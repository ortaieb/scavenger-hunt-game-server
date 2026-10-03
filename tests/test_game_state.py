import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.rows import DictRow

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.game_state import team_state
from game_server.models import VerdictStatus
from game_server.session_runs import SessionRun
from game_server.sessions import (
    GameSession,
    SessionRepository,
    Team,
    get_session_repository,
    parse_sessions,
)
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER_SESSION = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
DURING = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
WINDOW_OPENS = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)  # checkpoint 3's own window
WINDOW_CLOSES = datetime(2026, 10, 3, 11, 0, tzinfo=UTC)
FOX, HERON = "FOX-7Q2K", "HERON-4MXP"
RUNNING = SessionRun(started_at=START, stopped_at=None)
STOPPED = SessionRun(started_at=START, stopped_at=END)


def session_json(session_id: str) -> dict[str, Any]:
    def checkpoint(sequence: int, **extra: Any) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "name": f"Name {sequence}",
            "clue": f"Clue {sequence}",
            "location": {"lat": 51.5, "long": -0.1},
            "proximity": 40,
            **extra,
        }

    return {
        "id": session_id,
        "name": "Hunt",
        "location": "Here",
        "start-time": START.isoformat(),
        "end-time": END.isoformat(),
        "checkpoints": [
            checkpoint(1),
            checkpoint(2),
            checkpoint(
                3,
                window={
                    "opens-at": WINDOW_OPENS.isoformat(),
                    "closes-at": WINDOW_CLOSES.isoformat(),
                },
            ),
        ],
        "teams": [
            {
                "name": "Red Foxes",
                "join-code": FOX if session_id == SESSION else "OTHER-FOX1",
                "order": [1, 2, 3],
            },
            {
                "name": "Blue Herons",
                "join-code": HERON if session_id == SESSION else "OTHER-HRN1",
                "order": [3, 1, 2],
            },
        ],
    }


def repository() -> SessionRepository:
    return parse_sessions(json.dumps([session_json(SESSION), session_json(OTHER_SESSION)]))


def game_session() -> GameSession:
    found = repository().get_session(UUID(SESSION))
    assert found is not None
    return found


# --- the pure rules ------------------------------------------------------------------


def foxes() -> Team:
    return game_session().teams[0]


@pytest.mark.parametrize(
    ("run", "now"),
    [
        (None, DURING),
        (SessionRun(started_at=None, stopped_at=None), DURING),
        (None, END + timedelta(hours=1)),  # the planned end passes, but it never started
    ],
    ids=["no-run", "row-without-start", "after-planned-end"],
)
def test_before_the_start_is_not_started(run: SessionRun | None, now: datetime) -> None:
    state = team_state(game_session(), foxes(), frozenset(), now, run)

    assert (state.status, state.current, state.completed, state.total) == (
        "not_started",
        None,
        0,
        3,
    )


@pytest.mark.parametrize(
    "now",
    [START - timedelta(hours=1), DURING, END + timedelta(hours=1)],
    ids=["before-planned-start", "during", "after-planned-end"],
)
def test_a_running_session_plays_the_first_checkpoint_whatever_the_clock(now: datetime) -> None:
    state = team_state(game_session(), foxes(), frozenset(), now, RUNNING)

    assert state.status == "playing"
    assert state.current is not None
    assert (state.current.sequence, state.current.position, state.current.clue) == (1, 1, "Clue 1")


def test_after_a_stop_unfinished_is_ended() -> None:
    state = team_state(game_session(), foxes(), {1}, DURING, STOPPED)

    assert (state.status, state.current, state.completed) == ("ended", None, 1)


def test_completed_checkpoints_move_the_team_along_its_route() -> None:
    state = team_state(game_session(), foxes(), {1}, DURING, RUNNING)

    assert state.current is not None
    assert (state.current.sequence, state.current.position, state.completed) == (2, 2, 1)


def test_out_of_order_completion_still_takes_the_first_gap() -> None:
    state = team_state(game_session(), foxes(), {2}, DURING, RUNNING)  # 2 done before 1

    assert state.current is not None
    assert (state.current.sequence, state.current.position, state.completed) == (1, 1, 1)


@pytest.mark.parametrize("run", [None, RUNNING, STOPPED], ids=["scheduled", "running", "stopped"])
def test_everything_completed_is_finished_in_any_phase(run: SessionRun | None) -> None:
    state = team_state(game_session(), foxes(), {1, 2, 3}, DURING, run)

    assert (state.status, state.current, state.completed, state.total) == ("finished", None, 3, 3)


def test_each_team_follows_its_own_route() -> None:
    session = game_session()
    herons = session.teams[1]

    state = team_state(session, herons, frozenset(), WINDOW_OPENS, RUNNING)

    assert state.current is not None
    assert (state.current.sequence, state.current.position, state.current.clue) == (3, 1, "Clue 3")


@pytest.mark.parametrize(
    ("now", "is_open"),
    [
        (WINDOW_OPENS - timedelta(seconds=1), False),
        (WINDOW_OPENS, True),
        (WINDOW_CLOSES, True),
        (WINDOW_CLOSES + timedelta(seconds=1), False),
    ],
    ids=["before-window", "opens", "closes", "after-window"],
)
def test_open_follows_the_effective_window(now: datetime, is_open: bool) -> None:
    herons = game_session().teams[1]  # starts at checkpoint 3, which has its own window

    state = team_state(game_session(), herons, frozenset(), now, RUNNING)

    assert state.current is not None
    assert state.current.open is is_open


def test_a_window_closing_before_a_late_start_is_never_open() -> None:
    herons = game_session().teams[1]
    late = SessionRun(started_at=WINDOW_CLOSES + timedelta(minutes=1), stopped_at=None)

    for now in (WINDOW_OPENS, WINDOW_CLOSES, WINDOW_CLOSES + timedelta(minutes=2)):
        state = team_state(game_session(), herons, frozenset(), now, late)
        assert state.current is not None
        assert state.current.open is False


def test_checkpoint_without_window_is_open_while_running() -> None:
    state = team_state(game_session(), foxes(), frozenset(), END + timedelta(hours=1), RUNNING)

    assert state.current is not None
    assert state.current.open is True


# --- the endpoint, with real submission rows -----------------------------------------


@pytest.fixture
def now() -> list[datetime]:
    return [DURING]


@pytest.fixture
def client(now: list[datetime], store: SubmissionStore) -> Iterator[TestClient]:
    store.start_run(UUID(SESSION), START)
    app = create_app()
    settings = Settings()
    sessions = repository()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


def join(client: TestClient, code: str = FOX) -> str:
    participant: str = client.post("/join", json={"code": code, "consent": True}).json()[
        "participant"
    ]
    return participant


def state(client: TestClient, participant: str, session: str = SESSION) -> Response:
    return client.get(f"/sessions/{session}/participants/{participant}/state")


def submit(
    store: SubmissionStore,
    participant: str,
    checkpoint: int,
    verdict: VerdictStatus,
    session: str = SESSION,
) -> None:
    store.record(
        NewSubmission(
            session=UUID(session),
            participant=UUID(participant),
            checkpoint=checkpoint,
            received_at=DURING,
            capture_time=DURING,
            lat=51.5,
            long=-0.1,
            image_id=uuid4(),
            verdict=verdict,
            checks=(),
            distance_m=1.0,
            phash=checkpoint,
        )
    )


def test_playing_response(client: TestClient) -> None:
    response = state(client, join(client))

    assert response.status_code == 200
    assert response.json() == {
        "status": "playing",
        "team": "Red Foxes",
        "progress": {"completed": 0, "total": 3},
        "current": {"sequence": 1, "position": 1, "clue": "Clue 1", "open": True},
    }


def test_before_the_start_there_is_no_current_checkpoint(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    participant = join(client)
    db.execute("TRUNCATE session_runs")  # joined, but the moderator hasn't started yet

    body = state(client, participant).json()

    assert (body["status"], body["current"]) == ("not_started", None)


def test_after_a_stop_there_is_no_current_checkpoint(
    client: TestClient, store: SubmissionStore
) -> None:
    participant = join(client)
    store.stop_run(UUID(SESSION), DURING)

    body = state(client, participant).json()

    assert (body["status"], body["current"]) == ("ended", None)


def test_the_planned_end_does_not_end_a_running_session(
    client: TestClient, now: list[datetime]
) -> None:
    participant = join(client)
    now[0] = END + timedelta(hours=1)

    body = state(client, participant).json()

    assert (body["status"], body["current"]["open"]) == ("playing", True)


def test_two_teams_get_their_own_first_clue(client: TestClient, now: list[datetime]) -> None:
    now[0] = WINDOW_OPENS
    fox, heron = join(client), join(client, HERON)

    assert state(client, fox).json()["current"]["clue"] == "Clue 1"
    assert state(client, heron).json()["current"]["clue"] == "Clue 3"


def test_failed_submission_does_not_complete(client: TestClient, store: SubmissionStore) -> None:
    participant = join(client)
    submit(store, participant, 1, "failed")

    body = state(client, participant).json()

    assert body["progress"]["completed"] == 0
    assert body["current"]["sequence"] == 1


@pytest.mark.parametrize("verdict", ["pass", "pending"])
def test_accepted_submission_completes_and_shows_the_next_clue(
    client: TestClient, store: SubmissionStore, verdict: VerdictStatus
) -> None:
    participant = join(client)
    submit(store, participant, 1, "failed")
    submit(store, participant, 1, verdict)

    body = state(client, participant).json()

    assert body["progress"] == {"completed": 1, "total": 3}
    assert body["current"] == {"sequence": 2, "position": 2, "clue": "Clue 2", "open": True}


def test_everything_completed_is_finished_even_after_a_stop(
    client: TestClient, store: SubmissionStore
) -> None:
    participant = join(client)
    for checkpoint in (1, 2, 3):
        submit(store, participant, checkpoint, "pending")
    store.stop_run(UUID(SESSION), END)

    body = state(client, participant).json()

    assert body == {
        "status": "finished",
        "team": "Red Foxes",
        "progress": {"completed": 3, "total": 3},
        "current": None,
    }


def test_other_participants_and_sessions_do_not_count(
    client: TestClient, store: SubmissionStore
) -> None:
    participant = join(client)
    heron = join(client, HERON)
    submit(store, str(uuid4()), 1, "pass")  # never joined
    submit(store, participant, 1, "pass", session=OTHER_SESSION)  # another session
    submit(store, heron, 1, "pass")  # another team

    body = state(client, participant).json()

    assert body["progress"]["completed"] == 0
    assert body["current"]["sequence"] == 1


def test_response_reveals_only_the_current_clue(client: TestClient) -> None:
    text = state(client, join(client)).text

    for leak in ("Clue 2", "Clue 3", "Name", "51.5", "proximity", "order", FOX, "Blue Herons"):
        assert leak not in text
    for times in ("10:30", "11:00", "09:00", "12:00"):
        assert times not in text


# --- errors ---------------------------------------------------------------------------


def test_unknown_session_is_404(client: TestClient) -> None:
    response = state(client, join(client), session=str(uuid4()))

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown session"}


def test_participant_that_never_joined_is_404(client: TestClient) -> None:
    response = state(client, str(uuid4()))

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown participant"}


def test_participant_of_another_session_is_404(client: TestClient) -> None:
    other = client.post("/join", json={"code": "OTHER-FOX1", "consent": True}).json()

    response = state(client, other["participant"])  # asks about SESSION

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown participant"}


@pytest.mark.parametrize(
    "path",
    [
        f"/sessions/not-a-uuid/participants/{uuid4()}/state",
        f"/sessions/{SESSION}/participants/not-a-uuid/state",
    ],
)
def test_malformed_ids_are_422(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422
