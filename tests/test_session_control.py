import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.session_runs import SessionRun, session_phase
from game_server.sessions import get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
CODE = "MOD-8H3T-QX"
OTHER_CODE = "MOD-2KV9-ZP"
PLANNED_START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
PLANNED_END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 3, 9, 3, 12, tzinfo=UTC)


def session(session_id: str, code: str) -> dict[str, Any]:
    return {
        "id": session_id,
        "name": "Hunt",
        "location": "Here",
        "start-time": PLANNED_START.isoformat(),
        "end-time": PLANNED_END.isoformat(),
        "moderator-code": code,
        "checkpoints": [
            {
                "sequence": 1,
                "name": "Spot",
                "clue": "Find it",
                "location": {"lat": 51.5, "long": -0.1},
                "proximity": 40,
            }
        ],
    }


@pytest.fixture
def now() -> list[datetime]:
    return [T0]


@pytest.fixture
def client(now: list[datetime]) -> Iterator[TestClient]:
    app = create_app()
    repository = parse_sessions(json.dumps([session(SESSION, CODE), session(OTHER, OTHER_CODE)]))
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


def post(
    client: TestClient, action: str, session_id: str = SESSION, code: str | None = CODE
) -> Response:
    headers = {"Authorization": f"Bearer {code}"} if code else {}
    return client.post(f"/sessions/{session_id}/{action}", headers=headers)


# --- the phase ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run", "phase"),
    [
        (None, "scheduled"),
        (SessionRun(started_at=None, stopped_at=None), "scheduled"),
        (SessionRun(started_at=T0, stopped_at=None), "running"),
        (SessionRun(started_at=T0, stopped_at=T0 + timedelta(hours=1)), "stopped"),
    ],
    ids=["no-run", "row-without-start", "started", "stopped"],
)
def test_session_phase(run: SessionRun | None, phase: str) -> None:
    assert session_phase(run) == phase


# --- starting ---------------------------------------------------------------------------


def test_start_returns_the_session_clock(client: TestClient) -> None:
    response = post(client, "start")

    assert response.status_code == 201
    assert response.json() == {
        "phase": "running",
        "planned-start": "2026-10-03T09:00:00Z",
        "planned-end": "2026-10-03T12:00:00Z",
        "started-at": "2026-10-03T09:03:12Z",
        "stopped-at": None,
        "server-time": "2026-10-03T09:03:12Z",
    }


@pytest.mark.parametrize(
    "at",
    [PLANNED_START - timedelta(hours=2), PLANNED_START + timedelta(minutes=30)],
    ids=["before-planned-start", "after-planned-start"],
)
def test_start_works_whatever_the_planned_time(
    client: TestClient, now: list[datetime], at: datetime
) -> None:
    now[0] = at

    response = post(client, "start")

    assert response.status_code == 201
    assert response.json()["phase"] == "running"


def test_session_keeps_running_past_its_planned_end(
    client: TestClient, now: list[datetime]
) -> None:
    post(client, "start")
    now[0] = PLANNED_END + timedelta(hours=3)

    again = post(client, "start")  # a repeat start reports the clock

    assert again.status_code == 200
    assert again.json()["phase"] == "running"
    assert again.json()["server-time"] == "2026-10-03T15:00:00Z"


def test_repeat_start_returns_the_same_clock(client: TestClient, now: list[datetime]) -> None:
    first = post(client, "start").json()

    repeat = post(client, "start")

    assert repeat.status_code == 200
    assert repeat.json() == first
    now[0] += timedelta(minutes=5)
    later = post(client, "start").json()
    assert later["started-at"] == first["started-at"]


def test_start_after_stop_is_409(client: TestClient) -> None:
    post(client, "start")
    post(client, "stop")

    response = post(client, "start")

    assert response.status_code == 409
    assert response.json() == {"detail": "session has ended", "code": "session_stopped"}


# --- stopping ---------------------------------------------------------------------------


def test_stop_finishes_a_running_session(client: TestClient, now: list[datetime]) -> None:
    post(client, "start")
    now[0] = T0 + timedelta(hours=2)

    response = post(client, "stop")

    assert response.status_code == 201
    assert response.json() == {
        "phase": "stopped",
        "planned-start": "2026-10-03T09:00:00Z",
        "planned-end": "2026-10-03T12:00:00Z",
        "started-at": "2026-10-03T09:03:12Z",
        "stopped-at": "2026-10-03T11:03:12Z",
        "server-time": "2026-10-03T11:03:12Z",
    }


def test_repeat_stop_returns_the_same_clock(client: TestClient) -> None:
    post(client, "start")
    first = post(client, "stop").json()

    repeat = post(client, "stop")

    assert repeat.status_code == 200
    assert repeat.json() == first


def test_stop_before_start_is_409(client: TestClient) -> None:
    response = post(client, "stop")

    assert response.status_code == 409
    assert response.json() == {"detail": "session hasn't started", "code": "session_not_started"}


def test_sessions_run_independently(client: TestClient) -> None:
    post(client, "start")

    assert post(client, "stop", OTHER, OTHER_CODE).status_code == 409
    assert post(client, "start", OTHER, OTHER_CODE).status_code == 201


# --- authorisation ----------------------------------------------------------------------


@pytest.mark.parametrize("action", ["start", "stop"])
@pytest.mark.parametrize(
    "code", [None, "MOD-WRONG-1", OTHER_CODE], ids=["no-code", "wrong", "other-session"]
)
def test_moderator_code_required(client: TestClient, action: str, code: str | None) -> None:
    response = post(client, action, code=code)

    assert response.status_code == 401
    assert response.json() == {
        "detail": "moderator code required",
        "code": "moderator_unauthorised",
    }
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("action", ["start", "stop"])
def test_unknown_session_is_404(client: TestClient, action: str) -> None:
    response = post(client, action, str(uuid4()))

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown session"}


def test_refused_calls_change_nothing(client: TestClient, store: SubmissionStore) -> None:
    post(client, "start", code=None)
    post(client, "stop")

    assert store.session_run(UUID(SESSION)) is None


# --- concurrency and logging ---------------------------------------------------------------


def test_concurrent_starts_stamp_one_time(store: SubmissionStore) -> None:
    barrier = Barrier(8)

    def start(offset: int) -> tuple[datetime | None, bool]:
        barrier.wait()
        change = store.start_run(UUID(SESSION), T0 + timedelta(seconds=offset))
        assert change.run is not None
        return change.run.started_at, change.changed

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(start, range(8)))

    assert len({started for started, _ in results}) == 1
    assert [changed for _, changed in results].count(True) == 1
    run = store.session_run(UUID(SESSION))
    assert run is not None
    assert run.started_at == results[0][0]


def test_each_change_is_logged_once_without_the_code(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    post(client, "start")
    post(client, "start")
    post(client, "stop")
    post(client, "stop")

    lines = [r.getMessage() for r in caplog.records if r.name == "game_server.session_control"]
    assert lines == [
        f"Session {SESSION} phase running started_at 2026-10-03T09:03:12Z",
        f"Session {SESSION} phase stopped stopped_at 2026-10-03T09:03:12Z",
    ]
    assert CODE not in caplog.text
