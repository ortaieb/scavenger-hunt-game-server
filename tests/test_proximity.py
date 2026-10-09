import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg import sql
from psycopg.rows import DictRow
from pytest_mock import MockerFixture

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
UNKNOWN_SESSION = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
PARTICIPANT = "7c860ccc-9adf-4e22-b54f-3ff158f5d600"
OTHER_PARTICIPANT = "5d0a8b8e-7f6c-4d4b-8f0e-2b1a9c3d4e5f"
CHECKPOINT_AT = {"lat": 51.5, "long": -0.1}
INTERVAL = 10

SESSION_START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
SESSION_END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
WINDOW_OPENS = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)  # checkpoint 2's own window
WINDOW_CLOSES = datetime(2026, 10, 3, 11, 0, tzinfo=UTC)
INSIDE_WINDOW = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)

IN_RANGE = {"lat": 51.5001, "long": -0.1}  # ~11 m
OUT_OF_RANGE = {"lat": 51.5004, "long": -0.1}  # ~44 m; proximity is 40 m

BODY: dict[str, Any] = {
    "session": SESSION,
    "participant": PARTICIPANT,
    "checkpoint": 1,
    "location": IN_RANGE,
}


def sessions_text() -> str:
    checkpoint = {"name": "Spot", "clue": "Find it", "location": CHECKPOINT_AT, "proximity": 40}
    return json.dumps(
        [
            {
                "id": SESSION,
                "name": "Test hunt",
                "location": "Somewhere",
                "start-time": SESSION_START.isoformat(),
                "end-time": SESSION_END.isoformat(),
                "checkpoints": [
                    {**checkpoint, "sequence": 1},
                    {
                        **checkpoint,
                        "sequence": 2,
                        "window": {
                            "opens-at": WINDOW_OPENS.isoformat(),
                            "closes-at": WINDOW_CLOSES.isoformat(),
                        },
                    },
                ],
            }
        ]
    )


def sessions_repository() -> SessionRepository:
    return parse_sessions(sessions_text())


@pytest.fixture
def now() -> list[datetime]:
    """The fixed clock's current time; tests replace element 0 to move time."""
    return [INSIDE_WINDOW]


@pytest.fixture
def client(
    now: list[datetime], store: SubmissionStore, load_sessions: Callable[[str], SessionRepository]
) -> Iterator[TestClient]:
    store.start_run(UUID(SESSION), SESSION_START)
    app = create_app()
    settings = Settings(proximity_hint_interval_seconds=INTERVAL)
    repository = load_sessions(sessions_text())
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


def ask(client: TestClient, body: dict[str, Any] | None = None, **changes: Any) -> Response:
    return client.post("/checkpoint/proximity", json={**(body or BODY), **changes})


# --- the answer --------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "in_range"),
    [
        pytest.param(CHECKPOINT_AT, True, id="at-checkpoint"),
        pytest.param(IN_RANGE, True, id="in-range"),
        pytest.param(OUT_OF_RANGE, False, id="out-of-range"),
        pytest.param({"lat": 48.8584, "long": 2.2945}, False, id="far-away"),
    ],
)
def test_in_range(client: TestClient, location: dict[str, float], in_range: bool) -> None:
    response = ask(client, location=location)

    assert response.status_code == 200
    assert response.json() == {"in_range": in_range}


@pytest.mark.parametrize(
    ("distance", "in_range"),
    [(40.0, True), (40.001, False)],
    ids=["exactly-on-boundary", "just-outside"],
)
def test_boundary_counts_as_in_range(
    client: TestClient, mocker: MockerFixture, distance: float, in_range: bool
) -> None:
    mocker.patch("game_server.proximity.distance_m", return_value=distance)

    assert ask(client).json() == {"in_range": in_range}


@pytest.mark.parametrize(
    ("at", "checkpoint", "in_range"),
    [
        pytest.param(SESSION_START, 1, True, id="at-start"),
        pytest.param(SESSION_END + timedelta(hours=1), 1, True, id="after-planned-end"),
        pytest.param(WINDOW_OPENS - timedelta(seconds=1), 2, False, id="before-window"),
        pytest.param(INSIDE_WINDOW, 2, True, id="inside-window"),
        pytest.param(WINDOW_CLOSES + timedelta(seconds=1), 2, False, id="after-window"),
    ],
)
def test_closed_window_is_not_in_range(
    client: TestClient, now: list[datetime], at: datetime, checkpoint: int, in_range: bool
) -> None:
    now[0] = at

    assert ask(client, checkpoint=checkpoint).json() == {"in_range": in_range}


def test_a_session_not_yet_started_is_not_in_range(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    db.execute("TRUNCATE session_runs")

    assert ask(client).json() == {"in_range": False}


def test_a_stopped_session_is_not_in_range(client: TestClient, store: SubmissionStore) -> None:
    store.stop_run(UUID(SESSION), WINDOW_OPENS)

    assert ask(client, checkpoint=1).json() == {"in_range": False}


def test_closed_and_out_of_range_look_the_same(client: TestClient, now: list[datetime]) -> None:
    out_of_range = ask(client, location=OUT_OF_RANGE)
    now[0] = WINDOW_CLOSES + timedelta(minutes=1)
    closed = ask(client, checkpoint=2)

    assert out_of_range.status_code == closed.status_code == 200
    assert out_of_range.content == closed.content


# --- nothing leaks ------------------------------------------------------------


@pytest.mark.parametrize("location", [IN_RANGE, OUT_OF_RANGE], ids=["in", "out"])
def test_body_has_exactly_one_field(client: TestClient, location: dict[str, float]) -> None:
    body = ask(client, location=location).json()

    assert set(body) == {"in_range"}
    assert isinstance(body["in_range"], bool)


def test_openapi_schema_has_exactly_one_field(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()["components"]["schemas"]["ProximityHint"]

    assert set(schema["properties"]) == {"in_range"}


def test_does_not_log_coordinates(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    ask(client, location={"lat": 51.500123, "long": -0.100456})

    assert "51.500123" not in caplog.text
    assert "0.100456" not in caplog.text


def test_stores_nothing(
    client: TestClient, now: list[datetime], db: psycopg.Connection[DictRow]
) -> None:
    for step in range(3):
        now[0] = INSIDE_WINDOW + timedelta(seconds=step * INTERVAL)
        ask(client)

    for table in ("submissions", "participants", "arrivals"):
        query = sql.SQL("SELECT COUNT(*) AS n FROM {}").format(sql.Identifier(table))
        assert db.execute(query).fetchone() == {"n": 0}


# --- rate limit ---------------------------------------------------------------


def test_second_call_within_interval_is_429(client: TestClient, now: list[datetime]) -> None:
    ask(client)
    now[0] += timedelta(seconds=INTERVAL - 1)

    response = ask(client)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert set(response.json()) == {"detail"}


def test_call_after_interval_is_allowed(client: TestClient, now: list[datetime]) -> None:
    ask(client)
    now[0] += timedelta(seconds=INTERVAL)

    assert ask(client).status_code == 200


def test_limits_are_per_participant(client: TestClient) -> None:
    ask(client)

    assert ask(client, participant=OTHER_PARTICIPANT).status_code == 200
    assert ask(client).status_code == 429


def test_limit_applies_across_checkpoints(client: TestClient) -> None:
    ask(client)

    assert ask(client, checkpoint=2).status_code == 429


def test_rate_limited_answer_reveals_nothing(client: TestClient) -> None:
    ask(client)

    assert "in_range" not in ask(client, location=OUT_OF_RANGE).json()


# --- errors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"session": UNKNOWN_SESSION}, "unknown session"),
        ({"checkpoint": 3}, "unknown checkpoint"),
    ],
)
def test_unknown_target_is_404(client: TestClient, changes: dict[str, Any], detail: str) -> None:
    response = ask(client, **changes)

    assert response.status_code == 404
    assert response.json() == {"detail": detail}


def test_404s_do_not_use_up_the_rate_limit(client: TestClient) -> None:
    ask(client, checkpoint=3)

    assert ask(client).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {k: v for k, v in BODY.items() if k != "location"},
        {k: v for k, v in BODY.items() if k != "participant"},
        {**BODY, "checkpoint": "1"},
        {**BODY, "checkpoint": 0},
        {**BODY, "session": "not-a-uuid"},
        {**BODY, "location": {"lat": 91, "long": 0}},
        {**BODY, "location": {**IN_RANGE, "accuracy": 5}},
        {**BODY, "proximity": 1000},
    ],
)
def test_invalid_body_is_422(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/checkpoint/proximity", json=body)

    assert response.status_code == 422


def test_location_is_not_accepted_in_the_url(client: TestClient) -> None:
    response = client.get("/checkpoint/proximity", params={"lat": 51.5, "long": -0.1})

    assert response.status_code == 405
