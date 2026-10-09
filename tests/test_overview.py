"""The moderator overview, and the blocked attempts it shows (recorded by join, arrive, photo)."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.models import VerdictStatus
from game_server.rulings import Ruling
from game_server.sessions import SessionRepository, get_session_repository
from game_server.submissions import BLOCKED_KEPT, NewSubmission, SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
MODERATOR = "MOD-8H3T-QX"
OTHER_MODERATOR = "MOD-2KV9-ZP"
FOX, HERON, OWL = "FOX-7Q2K", "HERON-4MXP", "OWL-9KD3"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
DURING = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
CLUE = "SENTINEL-CLUE-3f"
SCENE = "SENTINEL-SCENE-8a"
LAT, LONG = 51.5034, -0.1276
PHOTOS = count(300)  # distinct seeds: no photo is a duplicate of another


def session_json(session_id: str, moderator: str, suffix: str = "") -> dict[str, Any]:
    return {
        "id": session_id,
        "name": "Hunt",
        "location": "Here",
        "start-time": START.isoformat(),
        "end-time": (START + timedelta(hours=3)).isoformat(),
        "moderator-code": moderator,
        "checkpoints": [
            {
                "sequence": n,
                "name": f"Spot {n}",
                "clue": f"{CLUE} {n}",
                "location": {"lat": LAT, "long": LONG},
                "proximity": 40,
                "challenge": {"scene": f"{SCENE} {n}", "pose": "Wave"},
            }
            for n in (1, 2, 3)
        ],
        "teams": [
            {"name": "Red Foxes", "join-code": f"{FOX}{suffix}", "order": [1, 2, 3]},
            {"name": "Blue Herons", "join-code": f"{HERON}{suffix}", "order": [3, 1, 2]},
            {"name": "Green Owls", "join-code": f"{OWL}{suffix}", "order": [2, 3, 1]},
        ],
    }


@pytest.fixture
def now() -> list[datetime]:
    return [DURING]


@pytest.fixture
def client(
    now: list[datetime], tmp_path: Path, load_sessions: Callable[[str], SessionRepository]
) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=tmp_path / "images")
    sessions = load_sessions(
        json.dumps(
            [session_json(SESSION, MODERATOR), session_json(OTHER, OTHER_MODERATOR, "-OTHER")]
        )
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


def moderate(client: TestClient, action: str, session: str = SESSION) -> Response:
    code = MODERATOR if session == SESSION else OTHER_MODERATOR
    headers = {"Authorization": f"Bearer {code}"}
    if action == "overview":
        return client.get(f"/sessions/{session}/overview", headers=headers)
    return client.post(f"/sessions/{session}/{action}", headers=headers)


def overview(client: TestClient) -> dict[str, Any]:
    response = moderate(client, "overview")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def join(client: TestClient, code: str = FOX) -> str:
    participant: str = client.post("/join", json={"code": code, "consent": True}).json()[
        "participant"
    ]
    return participant


def arrive(client: TestClient, participant: str, checkpoint: int = 1) -> Response:
    return client.post(
        f"/sessions/{SESSION}/participants/{participant}/arrive", json={"checkpoint": checkpoint}
    )


def photograph(client: TestClient, participant: str, at: datetime, **changes: Any) -> Response:
    metadata = {
        "session": SESSION,
        "participant": participant,
        "checkpoint": 1,
        "location": {"lat": LAT, "long": LONG},
        "capture-time": at.isoformat(),
        **changes,
    }
    return client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "challenge-image": ("p.jpeg", jpeg(scene(next(PHOTOS))), "image/jpeg"),
        },
    )


def submit(
    store: SubmissionStore, participant: str, checkpoint: int, verdict: VerdictStatus, at: datetime
) -> int:
    return store.record(
        NewSubmission(
            session=UUID(SESSION),
            participant=UUID(participant),
            checkpoint=checkpoint,
            received_at=at,
            capture_time=at,
            lat=LAT,
            long=LONG,
            image_id=uuid4(),
            verdict=verdict,
            checks=(),
            distance_m=1.0,
            phash=checkpoint,
            processing_ms=40,
        )
    ).id


def blocked_rows(db: psycopg.Connection[DictRow]) -> list[tuple[str, str, str]]:
    rows = db.execute("SELECT team, action, code FROM blocked_attempts ORDER BY id").fetchall()
    return [(row["team"], row["action"], row["code"]) for row in rows]


def minutes(n: int) -> datetime:
    return DURING + timedelta(minutes=n)


# --- blocked attempts are recorded ----------------------------------------------------------


def test_join_after_the_stop_is_recorded_with_the_team_not_the_code(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    moderate(client, "start")
    moderate(client, "stop")

    assert client.post("/join", json={"code": FOX, "consent": True}).status_code == 409

    assert blocked_rows(db) == [("Red Foxes", "join", "session_stopped")]
    assert FOX not in json.dumps(
        db.execute("SELECT * FROM blocked_attempts").fetchall(), default=str
    )


@pytest.mark.parametrize(
    "body",
    [{"code": "NO-SUCH-CODE", "consent": True}, {"code": FOX}],
    ids=["unknown-code", "invalid-body"],
)
def test_other_refused_joins_are_not_recorded(
    client: TestClient, db: psycopg.Connection[DictRow], body: dict[str, Any]
) -> None:
    moderate(client, "start")
    moderate(client, "stop")

    assert client.post("/join", json=body).status_code in (404, 422)

    assert blocked_rows(db) == []


def test_arrive_before_the_start_is_recorded(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    fox = join(client)

    assert arrive(client, fox).status_code == 409

    assert blocked_rows(db) == [("Red Foxes", "arrive", "session_not_started")]


def test_arrive_after_the_stop_is_recorded(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    fox = join(client)
    moderate(client, "start")
    moderate(client, "stop")

    assert arrive(client, fox).status_code == 409

    assert blocked_rows(db) == [("Red Foxes", "arrive", "session_stopped")]


def test_other_arrive_conflicts_are_not_recorded(
    client: TestClient, db: psycopg.Connection[DictRow], store: SubmissionStore
) -> None:
    fox, heron = join(client), join(client, HERON)
    moderate(client, "start")
    for checkpoint in (1, 2, 3):
        submit(store, heron, checkpoint, "pass", DURING)

    assert arrive(client, fox, 2).json()["code"] == "not_current_checkpoint"
    assert arrive(client, heron).json()["code"] == "hunt_finished"
    assert arrive(client, fox, 0).status_code == 422
    moderate(client, "stop")
    assert arrive(client, heron).json()["code"] == "hunt_finished"  # finished comes first

    assert blocked_rows(db) == []


@pytest.mark.parametrize(
    ("phase", "code"),
    [("scheduled", "session_not_started"), ("stopped", "session_stopped")],
)
def test_a_photo_outside_the_session_is_recorded(
    client: TestClient, db: psycopg.Connection[DictRow], phase: str, code: str
) -> None:
    fox = join(client)
    if phase == "stopped":
        moderate(client, "start")
        moderate(client, "stop")

    assert photograph(client, fox, DURING).json()["verdict"]["checkpoint"]["verdict"] == "failed"

    assert blocked_rows(db) == [("Red Foxes", "photo", code)]


def test_other_failed_photos_are_not_recorded(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    fox = join(client)
    moderate(client, "start")
    arrive(client, fox)

    far = photograph(client, fox, DURING, location={"lat": LAT + 0.1, "long": LONG})

    assert far.json()["verdict"]["checkpoint"]["rejections"][0]["code"] == "out_of_range"
    assert blocked_rows(db) == []


def test_a_photo_from_a_participant_that_never_joined_is_not_recorded(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    response = photograph(client, str(uuid4()), DURING)

    assert (response.status_code, response.json()) == (404, {"detail": "unknown participant"})
    assert blocked_rows(db) == []
    assert db.execute("SELECT * FROM submissions").fetchall() == []


def test_only_the_newest_blocked_attempts_are_kept(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    for n in range(BLOCKED_KEPT + 5):
        store.record_blocked(UUID(SESSION), f"T{n}", "join", "session_stopped", minutes(n))
    store.record_blocked(UUID(OTHER), "Other", "join", "session_stopped", minutes(0))

    rows = db.execute("SELECT team FROM blocked_attempts WHERE session = %s", (SESSION,))
    teams = {row["team"] for row in rows.fetchall()}
    assert len(teams) == BLOCKED_KEPT
    assert teams == {f"T{n}" for n in range(5, BLOCKED_KEPT + 5)}
    assert [a.team for a in store.blocked_attempts(UUID(OTHER), 50)] == ["Other"]


# --- the overview ------------------------------------------------------------------------


def test_the_overview_response(client: TestClient, store: SubmissionStore) -> None:
    fox, heron = join(client), join(client, HERON)
    moderate(client, "start")
    submit(store, fox, 1, "pass", minutes(1))
    submit(store, heron, 3, "pending", minutes(2))
    store.record_blocked(UUID(SESSION), "Green Owls", "join", "session_stopped", minutes(3))

    body = overview(client)

    assert body == {
        "session": {
            "phase": "running",
            "planned-start": "2026-10-03T09:00:00Z",
            "planned-end": "2026-10-03T12:00:00Z",
            "started-at": "2026-10-03T10:00:00Z",
            "stopped-at": None,
            "server-time": "2026-10-03T10:00:00Z",
        },
        "to-review": 1,
        "teams": [
            {
                "team": "Red Foxes",
                "joined": True,
                "completed": 1,
                "total": 3,
                "points": 1 + 3 + 3,
                "in-review": 0,
                "place": None,
                "last-completed": {
                    "sequence": 1,
                    "name": "Spot 1",
                    "verdict": "pass",
                    "at": "2026-10-03T10:01:00Z",
                },
                "current": {"sequence": 2, "name": "Spot 2"},
            },
            {
                "team": "Blue Herons",
                "joined": True,
                "completed": 1,
                "total": 3,
                "points": 3 + 3 + 3,
                "in-review": 1,
                "place": None,
                "last-completed": {
                    "sequence": 3,
                    "name": "Spot 3",
                    "verdict": "pending",
                    "at": "2026-10-03T10:02:00Z",
                },
                "current": {"sequence": 1, "name": "Spot 1"},
            },
            {
                "team": "Green Owls",
                "joined": False,
                "completed": 0,
                "total": 3,
                "points": None,
                "in-review": None,
                "place": None,
                "last-completed": None,
                "current": None,
            },
        ],
        "blocked": [
            {
                "at": "2026-10-03T10:03:00Z",
                "team": "Green Owls",
                "action": "join",
                "code": "session_stopped",
            }
        ],
    }


def test_standings_order_joined_teams_by_points_then_name(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, owl = join(client), join(client, OWL)
    join(client, HERON)
    moderate(client, "start")
    submit(store, owl, 2, "pass", minutes(1))
    submit(store, fox, 1, "pass", minutes(2))  # Foxes and Owls tie on points

    teams = overview(client)["teams"]

    assert [(t["team"], t["points"]) for t in teams] == [
        ("Green Owls", 9),
        ("Red Foxes", 9),
        ("Blue Herons", 12),
    ]


def test_standings_match_what_each_team_sees(client: TestClient, store: SubmissionStore) -> None:
    fox, heron, owl = join(client), join(client, HERON), join(client, OWL)
    moderate(client, "start")
    submit(store, heron, 1, "pass", minutes(1))
    submit(store, fox, 1, "pass", minutes(2))
    submit(store, owl, 2, "pending", minutes(3))
    submit(store, fox, 2, "pass", minutes(4))
    participants = {"Red Foxes": fox, "Blue Herons": heron, "Green Owls": owl}

    for _ in ("running", "stopped"):
        for row in overview(client)["teams"]:
            state = client.get(
                f"/sessions/{SESSION}/participants/{participants[row['team']]}/state"
            ).json()
            own = state["score"]
            assert (row["points"], row["in-review"], row["place"]) == (
                own["points"],
                own["in-review"],
                own["place"],
            )
            assert row["completed"] == state["progress"]["completed"]
        moderate(client, "stop")


def test_places_are_set_once_the_session_stops(client: TestClient, store: SubmissionStore) -> None:
    fox, heron = join(client), join(client, HERON)
    join(client, OWL)
    moderate(client, "start")
    submit(store, fox, 1, "pass", minutes(1))
    submit(store, heron, 3, "pass", minutes(1))
    assert all(t["place"] is None for t in overview(client)["teams"])
    moderate(client, "stop")

    body = overview(client)

    assert body["session"]["phase"] == "stopped"
    assert [(t["team"], t["place"]) for t in body["teams"]] == [
        ("Blue Herons", 1),
        ("Red Foxes", 1),
        ("Green Owls", 3),
    ]


def team_row(client: TestClient, name: str) -> dict[str, Any]:
    row: dict[str, Any] = next(t for t in overview(client)["teams"] if t["team"] == name)
    return row


def test_a_team_that_completed_nothing(client: TestClient, store: SubmissionStore) -> None:
    fox = join(client)
    moderate(client, "start")
    submit(store, fox, 1, "failed", minutes(1))

    row = team_row(client, "Red Foxes")

    assert (row["completed"], row["last-completed"]) == (0, None)
    assert row["current"] == {"sequence": 1, "name": "Spot 1"}


def test_a_team_mid_route_shows_its_latest_accepted_photo(
    client: TestClient, store: SubmissionStore
) -> None:
    heron = join(client, HERON)  # route 3, 1, 2
    moderate(client, "start")
    submit(store, heron, 3, "pass", minutes(1))
    submit(store, heron, 1, "pending", minutes(5))
    submit(store, heron, 2, "failed", minutes(7))

    row = team_row(client, "Blue Herons")

    assert row["completed"] == 2
    assert row["last-completed"] == {
        "sequence": 1,
        "name": "Spot 1",
        "verdict": "pending",
        "at": "2026-10-03T10:05:00Z",
    }
    assert row["current"] == {"sequence": 2, "name": "Spot 2"}


def test_to_review_counts_the_unruled_pending_photos(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, heron = join(client), join(client, HERON)
    moderate(client, "start")
    ruled = submit(store, fox, 1, "pending", minutes(1))
    submit(store, heron, 3, "pending", minutes(2))
    submit(store, fox, 2, "failed", minutes(3))
    submit(store, heron, 1, "pass", minutes(4))
    assert overview(client)["to-review"] == 2

    store.rule(UUID(SESSION), ruled, "approve", None, minutes(5))

    assert overview(client)["to-review"] == 1


@pytest.mark.parametrize(
    ("verdict", "ruling", "shown"),
    [
        ("pending", "approve", "pass"),
        ("pending", "reject", "failed"),
        ("failed", "approve", "pass"),
    ],
)
def test_last_completed_shows_the_effective_verdict(
    client: TestClient,
    store: SubmissionStore,
    verdict: VerdictStatus,
    ruling: Ruling,
    shown: VerdictStatus,
) -> None:
    fox = join(client)
    moderate(client, "start")
    submit(store, fox, 1, "pass", minutes(1))
    photo = submit(store, fox, 2, verdict, minutes(2))

    store.rule(UUID(SESSION), photo, ruling, None, minutes(3))

    row = team_row(client, "Red Foxes")
    assert row["last-completed"] == {
        "sequence": 2,
        "name": "Spot 2",
        "verdict": shown,
        "at": "2026-10-03T10:02:00Z",
    }
    assert (row["completed"], row["current"]) == (2, {"sequence": 3, "name": "Spot 3"})


def test_a_finished_team_has_no_current_checkpoint(
    client: TestClient, store: SubmissionStore
) -> None:
    owl = join(client, OWL)  # route 2, 3, 1
    moderate(client, "start")
    for n, checkpoint in enumerate((2, 3, 1)):
        submit(store, owl, checkpoint, "pass", minutes(n))

    row = team_row(client, "Green Owls")

    assert (row["completed"], row["current"]) == (3, None)
    assert row["last-completed"]["sequence"] == 1


def test_no_current_checkpoint_before_the_start(client: TestClient) -> None:
    join(client)

    assert team_row(client, "Red Foxes")["current"] is None


def test_blocked_shows_the_newest_fifty_newest_first(
    client: TestClient, store: SubmissionStore
) -> None:
    for n in range(60):
        store.record_blocked(UUID(SESSION), f"T{n}", "arrive", "session_not_started", minutes(n))
    store.record_blocked(UUID(OTHER), "Elsewhere", "join", "session_stopped", minutes(99))

    blocked = overview(client)["blocked"]

    assert [b["team"] for b in blocked] == [f"T{n}" for n in range(59, 9, -1)]


def test_the_overview_shows_blocked_attempts_as_they_happen(client: TestClient) -> None:
    fox = join(client)
    arrive(client, fox)  # before the start
    moderate(client, "start")
    moderate(client, "stop")
    photograph(client, fox, DURING)
    client.post("/join", json={"code": HERON, "consent": True})

    blocked = overview(client)["blocked"]

    assert [(b["team"], b["action"], b["code"]) for b in blocked] == [
        ("Blue Herons", "join", "session_stopped"),
        ("Red Foxes", "photo", "session_stopped"),
        ("Red Foxes", "arrive", "session_not_started"),
    ]


# --- authorisation and secrecy -------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer MOD-WRONG-1", f"Bearer {OTHER_MODERATOR}", f"Bearer {FOX}"],
    ids=["no-code", "wrong", "other-session", "join-code"],
)
def test_moderator_code_required(client: TestClient, authorization: str | None) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = client.get(f"/sessions/{SESSION}/overview", headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "detail": "moderator code required",
        "code": "moderator_unauthorised",
    }


def test_unknown_session_is_404(client: TestClient) -> None:
    response = client.get(
        f"/sessions/{uuid4()}/overview", headers={"Authorization": f"Bearer {MODERATOR}"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown session"}


def test_the_overview_reveals_no_coordinates_clues_scenes_or_codes(
    client: TestClient, store: SubmissionStore
) -> None:
    fox, heron = join(client), join(client, HERON)
    moderate(client, "start")
    submit(store, fox, 1, "pass", minutes(1))
    submit(store, heron, 3, "pending", minutes(2))
    moderate(client, "stop")
    client.post("/join", json={"code": OWL, "consent": True})

    text = moderate(client, "overview").text

    for leak in (CLUE, SCENE, str(LAT), str(LONG), "proximity", "Wave", FOX, HERON, OWL, MODERATOR):
        assert leak not in text
    for participant in (fox, heron):
        assert participant not in text
