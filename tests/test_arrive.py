import json
import logging
import re
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import count
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server import arrive as arrive_module
from game_server.app import create_app
from game_server.arrive import draw_code
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.models import VerdictStatus
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
DURING = datetime(2026, 10, 3, 9, 41, 5, tzinfo=UTC)
WINDOW_OPENS = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)  # checkpoint 3's own window
TTL = 600
FOX = "FOX-7Q2K"
POSE = "Arms raised, facing the camera, with the landmark behind you."


def repository() -> SessionRepository:
    def checkpoint(sequence: int, **extra: Any) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "name": f"Name {sequence}",
            "clue": f"Clue {sequence}",
            "location": {"lat": 51.5, "long": -0.1},
            "proximity": 40,
            **extra,
        }

    return parse_sessions(
        json.dumps(
            [
                {
                    "id": SESSION,
                    "name": "Hunt",
                    "location": "Here",
                    "start-time": START.isoformat(),
                    "end-time": END.isoformat(),
                    "checkpoints": [
                        checkpoint(1, challenge={"scene": "A fountain", "pose": POSE}),
                        checkpoint(2),  # no challenge: pose null
                        checkpoint(
                            3,
                            window={
                                "opens-at": WINDOW_OPENS.isoformat(),
                                "closes-at": END.isoformat(),
                            },
                        ),
                    ],
                    "teams": [
                        {"name": "Red Foxes", "join-code": FOX, "order": [1, 2, 3]},
                        {"name": "Blue Herons", "join-code": "HERON-4MXP", "order": [3, 1, 2]},
                    ],
                }
            ]
        )
    )


@pytest.fixture
def now() -> list[datetime]:
    return [DURING]


@pytest.fixture
def client(now: list[datetime], store: SubmissionStore) -> Iterator[TestClient]:
    store.start_run(UUID(SESSION), START)
    app = create_app()
    settings = Settings(arrival_code_ttl_seconds=TTL)
    sessions = repository()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def codes(mocker: MockerFixture) -> Iterator[str]:
    """Deterministic distinct codes: 1001, 1002, ..."""
    numbers = count(1001)
    mocker.patch.object(arrive_module, "draw_code", side_effect=lambda: str(next(numbers)))
    yield "patched"


def join(client: TestClient, code: str = FOX) -> str:
    participant: str = client.post("/join", json={"code": code, "consent": True}).json()[
        "participant"
    ]
    return participant


def arrive(client: TestClient, participant: str, checkpoint: int | str = 1) -> Response:
    return client.post(
        f"/sessions/{SESSION}/participants/{participant}/arrive", json={"checkpoint": checkpoint}
    )


def submit(
    store: SubmissionStore, participant: str, checkpoint: int, verdict: VerdictStatus, at: datetime
) -> None:
    store.record(
        NewSubmission(
            session=UUID(SESSION),
            participant=UUID(participant),
            checkpoint=checkpoint,
            received_at=at,
            capture_time=at,
            lat=51.5,
            long=-0.1,
            image_id=uuid4(),
            verdict=verdict,
            checks=(),
            distance_m=1.0,
            phash=hash(at) & 0xFFFF,
            processing_ms=40,
        )
    )


# --- issuing ------------------------------------------------------------------------


@pytest.mark.usefixtures("codes")
def test_first_arrive_issues_a_code_and_the_pose(client: TestClient) -> None:
    response = arrive(client, join(client))

    assert response.status_code == 201
    assert response.json() == {
        "checkpoint": 1,
        "pose": POSE,
        "code": "1001",
        "issued-at": "2026-10-03T09:41:05Z",
        "expires-at": "2026-10-03T09:51:05Z",
    }


@pytest.mark.usefixtures("codes")
def test_arriving_again_within_the_ttl_returns_the_same_arrival(
    client: TestClient, now: list[datetime]
) -> None:
    participant = join(client)
    first = arrive(client, participant).json()
    now[0] += timedelta(seconds=TTL - 1)

    again = arrive(client, participant)

    assert again.status_code == 200
    assert again.json() == first


@pytest.mark.usefixtures("codes")
def test_after_expiry_a_fresh_code_is_issued(client: TestClient, now: list[datetime]) -> None:
    participant = join(client)
    arrive(client, participant)
    now[0] += timedelta(seconds=TTL)

    response = arrive(client, participant)

    assert response.status_code == 201
    assert response.json()["code"] == "1002"
    assert response.json()["issued-at"] == "2026-10-03T09:51:05Z"


@pytest.mark.usefixtures("codes")
def test_after_a_failed_photo_a_fresh_code_is_issued(
    client: TestClient, store: SubmissionStore, now: list[datetime]
) -> None:
    participant = join(client)
    arrive(client, participant)
    now[0] += timedelta(minutes=1)
    submit(store, participant, 1, "failed", now[0])

    response = arrive(client, participant)

    assert response.status_code == 201
    assert response.json()["code"] == "1002"


@pytest.mark.usefixtures("codes")
def test_a_photo_before_the_arrival_does_not_end_it(
    client: TestClient, store: SubmissionStore, now: list[datetime]
) -> None:
    participant = join(client)
    submit(store, participant, 1, "failed", now[0] - timedelta(minutes=5))
    first = arrive(client, participant).json()

    assert arrive(client, participant).json() == first


@pytest.mark.parametrize("verdict", ["pass", "pending"])
@pytest.mark.usefixtures("codes")
def test_after_an_accepted_photo_the_team_has_moved_on(
    client: TestClient, store: SubmissionStore, now: list[datetime], verdict: VerdictStatus
) -> None:
    participant = join(client)
    arrive(client, participant)
    now[0] += timedelta(minutes=1)
    submit(store, participant, 1, verdict, now[0])

    response = arrive(client, participant)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "not your current checkpoint",
        "code": "not_current_checkpoint",
    }
    assert arrive(client, participant, 2).status_code == 201  # its next checkpoint


@pytest.mark.usefixtures("codes")
def test_checkpoint_without_a_challenge_has_no_pose_but_a_code(
    client: TestClient, store: SubmissionStore
) -> None:
    participant = join(client)
    submit(store, participant, 1, "pass", DURING - timedelta(minutes=1))

    response = arrive(client, participant, 2)

    assert response.status_code == 201
    assert response.json()["pose"] is None
    assert response.json()["code"] == "1001"


def test_ttl_is_not_capped_at_the_window_close(client: TestClient, now: list[datetime]) -> None:
    now[0] = END - timedelta(minutes=1)  # the window closes in a minute

    body = arrive(client, join(client)).json()

    assert body["expires-at"] == "2026-10-03T12:09:00Z"


# --- refusals, in order ---------------------------------------------------------------


def test_unknown_session_participant_and_checkpoint(client: TestClient) -> None:
    participant = join(client)
    unknown_session = client.post(
        f"/sessions/{uuid4()}/participants/{participant}/arrive", json={"checkpoint": 1}
    )

    assert (unknown_session.status_code, unknown_session.json()) == (
        404,
        {"detail": "unknown session"},
    )
    assert arrive(client, str(uuid4())).json() == {"detail": "unknown participant"}
    assert arrive(client, participant, 9).json() == {"detail": "unknown checkpoint"}
    assert arrive(client, participant, 9).status_code == 404


def conflict(response: Response) -> tuple[str, str]:
    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"detail", "code"}
    return body["detail"], body["code"]


def test_before_start_comes_first(
    client: TestClient, store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    participant = join(client)
    for checkpoint in (1, 2, 3):  # even a team with everything done
        submit(store, participant, checkpoint, "pass", DURING)
    db.execute("TRUNCATE session_runs")  # the moderator hasn't started the session

    assert conflict(arrive(client, participant, 2)) == (
        "session hasn't started",
        "session_not_started",
    )


def test_the_planned_start_does_not_start_a_session(
    client: TestClient, db: psycopg.Connection[DictRow], now: list[datetime]
) -> None:
    participant = join(client)
    db.execute("TRUNCATE session_runs")
    now[0] = START + timedelta(hours=1)

    assert conflict(arrive(client, participant)) == (
        "session hasn't started",
        "session_not_started",
    )


def test_finished_comes_before_ended(client: TestClient, store: SubmissionStore) -> None:
    participant = join(client)
    for checkpoint in (1, 2, 3):
        submit(store, participant, checkpoint, "pass", DURING)
    store.stop_run(UUID(SESSION), END)

    assert conflict(arrive(client, participant)) == ("hunt finished", "hunt_finished")


def test_ended_comes_before_not_current(client: TestClient, store: SubmissionStore) -> None:
    participant = join(client)
    store.stop_run(UUID(SESSION), DURING)

    assert conflict(arrive(client, participant, 2)) == ("session has ended", "session_stopped")


def test_the_planned_end_does_not_end_a_running_session(
    client: TestClient, now: list[datetime]
) -> None:
    participant = join(client)
    now[0] = END + timedelta(hours=1)

    assert arrive(client, participant).status_code == 201


def test_not_current_comes_before_not_open(client: TestClient) -> None:
    herons = join(client, "HERON-4MXP")  # starts at 3, whose window opens at 10:30

    assert conflict(arrive(client, herons, 1)) == (
        "not your current checkpoint",
        "not_current_checkpoint",
    )
    assert conflict(arrive(client, herons, 3)) == ("checkpoint isn't open", "checkpoint_closed")


def test_current_and_open_succeeds(client: TestClient, now: list[datetime]) -> None:
    herons = join(client, "HERON-4MXP")
    now[0] = WINDOW_OPENS

    assert arrive(client, herons, 3).status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"checkpoint": "1"},
        {"checkpoint": 1.0},
        {"checkpoint": 0},
        {"checkpoint": 1, "lat": 51.5},
    ],
    ids=["missing", "string", "float", "zero", "location-field"],
)
def test_invalid_body_is_422(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post(f"/sessions/{SESSION}/participants/{join(client)}/arrive", json=body)

    assert response.status_code == 422


# --- concurrency, codes and logging -------------------------------------------------------


def test_concurrent_arrives_create_one_arrival(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    barrier = Barrier(8)
    participant = uuid4()

    def tap(_: int) -> tuple[str, bool]:
        barrier.wait()
        outcome = store.arrive(
            UUID(SESSION),
            participant,
            1,
            pose=None,
            now=DURING,
            ttl=timedelta(seconds=TTL),
            new_code=draw_code,
        )
        return outcome.arrival.code, outcome.new

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(tap, range(8)))

    assert len({code for code, _ in results}) == 1
    assert [new for _, new in results].count(True) == 1
    assert db.execute("SELECT COUNT(*) AS n FROM arrivals").fetchone() == {"n": 1}


def test_codes_are_four_digits_over_many_draws() -> None:
    drawn = [draw_code() for _ in range(5000)]

    assert all(re.fullmatch(r"\d{4}", code) for code in drawn)
    assert any(code.startswith("0") for code in drawn)  # leading zeros kept
    assert len(set(drawn)) > 3000  # spread across the 10 000 values
    assert max(Counter(drawn).values()) < 10


def test_codes_come_from_secrets(mocker: MockerFixture) -> None:
    randbelow = mocker.patch("game_server.arrive.secrets.randbelow", return_value=42)

    assert draw_code() == "0042"
    randbelow.assert_called_once_with(10_000)


def test_the_code_is_never_logged(
    client: TestClient, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(arrive_module, "draw_code", return_value="SENTINEL-OTC")
    caplog.set_level(logging.DEBUG)
    participant = join(client)

    arrive(client, participant)
    arrive(client, participant)

    assert "SENTINEL-OTC" not in caplog.text
    assert f"participant {participant} checkpoint 1 new expires_at" in caplog.text
    assert f"participant {participant} checkpoint 1 existing expires_at" in caplog.text
