import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.rows import DictRow

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.join import find_participant
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
OTHER_SESSION = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
FOX = "FOX-7Q2K"
HERON = "HERON-4MXP"
SENTINEL_CODE = "SENTINEL-JOIN-55"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def session(session_id: str, teams: list[dict[str, Any]]) -> dict[str, Any]:
    checkpoint = {"name": "Spot", "clue": "Find it", "location": {"lat": 51.5, "long": -0.1}}
    return {
        "id": session_id,
        "name": "Hyde Park Saturday Hunt",
        "location": "Hyde Park, London",
        "start-time": START.isoformat(),
        "end-time": END.isoformat(),
        "checkpoints": [{**checkpoint, "sequence": n, "proximity": 40} for n in (1, 2, 3)],
        "teams": teams,
    }


def repository(*, with_heron: bool = True) -> SessionRepository:
    teams = [{"name": "Red Foxes", "join-code": FOX, "order": [1, 2, 3]}]
    if with_heron:
        teams.append({"name": "Blue Herons", "join-code": HERON, "order": [2, 3, 1]})
    other = [{"name": "Sentinels", "join-code": SENTINEL_CODE, "order": [3, 1, 2]}]
    return parse_sessions(json.dumps([session(SESSION, teams), session(OTHER_SESSION, other)]))


@pytest.fixture
def now() -> list[datetime]:
    return [datetime(2026, 10, 3, 9, 30, tzinfo=UTC)]


@pytest.fixture
def client(now: list[datetime]) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings()
    sessions = repository()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


def join(client: TestClient, code: str = FOX, **body: Any) -> Response:
    return client.post("/join", json={"code": code, "consent": True, **body})


def participants(db: psycopg.Connection[DictRow]) -> list[DictRow]:
    return db.execute("SELECT * FROM participants ORDER BY joined_at").fetchall()


# --- joining ------------------------------------------------------------------------


def test_first_join_creates_the_participant(client: TestClient) -> None:
    response = join(client)

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "participant": body["participant"],
        "team": "Red Foxes",
        "session": {
            "id": SESSION,
            "name": "Hyde Park Saturday Hunt",
            "location": "Hyde Park, London",
            "start-time": "2026-10-03T09:00:00Z",
            "end-time": "2026-10-03T12:00:00Z",
        },
        "checkpoints": 3,
    }
    assert UUID(body["participant"]).version == 4


@pytest.mark.parametrize("again", ["FOX-7Q2K", "fox-7q2k", "  Fox-7Q2k \n"])
def test_joining_again_returns_the_same_participant(
    client: TestClient, db: psycopg.Connection[DictRow], now: list[datetime], again: str
) -> None:
    first = join(client).json()["participant"]
    now[0] += timedelta(minutes=5)

    response = join(client, again)

    assert response.status_code == 200
    assert response.json()["participant"] == first
    [row] = participants(db)
    assert row["joined_at"] == datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
    assert row["consented_at"] == datetime(2026, 10, 3, 9, 35, tzinfo=UTC)  # consent again


def test_another_team_gets_another_participant(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    fox = join(client).json()["participant"]
    heron = join(client, HERON).json()["participant"]

    assert fox != heron
    assert {(row["team"], row["session"]) for row in participants(db)} == {
        ("Red Foxes", UUID(SESSION)),
        ("Blue Herons", UUID(SESSION)),
    }


def test_response_reveals_no_code_order_or_checkpoints(client: TestClient) -> None:
    text = join(client).text

    assert FOX not in text
    assert "order" not in text
    assert "Blue Herons" not in text
    assert "51.5" not in text
    assert "clue" not in text


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"code": FOX, "consent": False}, id="false"),
        pytest.param({"code": FOX}, id="missing"),
        pytest.param({"code": FOX, "consent": "true"}, id="string-true"),
        pytest.param({"code": FOX, "consent": 1}, id="one"),
        pytest.param({"code": FOX, "consent": None}, id="null"),
        pytest.param({"code": "", "consent": True}, id="empty-code"),
        pytest.param({"code": "X" * 65, "consent": True}, id="code-too-long"),
        pytest.param({"code": FOX, "consent": True, "name": "x"}, id="unknown-field"),
    ],
)
def test_invalid_body_is_422_and_stores_nothing(
    client: TestClient, db: psycopg.Connection[DictRow], body: dict[str, Any]
) -> None:
    response = client.post("/join", json=body)

    assert response.status_code == 422
    assert participants(db) == []
    assert all("input" not in error for error in response.json()["detail"])
    assert FOX not in response.text


def test_unknown_code_is_404(client: TestClient, db: psycopg.Connection[DictRow]) -> None:
    response = join(client, "NOPE-1234")

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown code"}
    assert participants(db) == []


@pytest.mark.parametrize(
    ("at", "status"),
    [
        pytest.param(START - timedelta(days=1), 201, id="day-before-start"),
        pytest.param(START, 201, id="at-start"),
        pytest.param(END, 201, id="at-end"),
        pytest.param(END + timedelta(seconds=1), 409, id="after-end"),
    ],
)
def test_joining_is_open_until_the_session_ends(
    client: TestClient,
    now: list[datetime],
    db: psycopg.Connection[DictRow],
    at: datetime,
    status: int,
) -> None:
    now[0] = at

    response = join(client)

    assert response.status_code == status
    if status == 409:
        assert response.json() == {"detail": "session has ended"}
        assert participants(db) == []


# --- concurrency and logging ---------------------------------------------------------


def test_concurrent_first_joins_create_one_participant(
    store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    barrier = Barrier(8)
    at = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)

    def join_now(_: int) -> tuple[UUID, bool]:
        barrier.wait()
        outcome = store.join_team(UUID(SESSION), "Red Foxes", at)
        return outcome.participant, outcome.first

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(join_now, range(8)))

    assert len({participant for participant, _ in results}) == 1
    assert [first for _, first in results].count(True) == 1
    assert len(participants(db)) == 1


def test_join_is_logged_without_the_code(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    participant = join(client, f" {SENTINEL_CODE.lower()} ").json()["participant"]
    join(client, SENTINEL_CODE)

    assert SENTINEL_CODE not in caplog.text.upper()
    assert (
        f"Team joined session {OTHER_SESSION} team Sentinels participant {participant} first=yes"
        in caplog.text
    )
    assert f"participant {participant} first=no" in caplog.text


# --- participant lookup (for #38 and #39) --------------------------------------------


def test_find_participant(store: SubmissionStore) -> None:
    sessions = repository()
    joined = store.join_team(UUID(SESSION), "Red Foxes", START).participant

    found = find_participant(store, sessions, UUID(SESSION), joined)

    assert found is not None
    assert (found.record.id, found.team.name, found.session.id) == (
        joined,
        "Red Foxes",
        UUID(SESSION),
    )
    assert found.team.order == (1, 2, 3)


def test_find_participant_none_cases(store: SubmissionStore) -> None:
    joined = store.join_team(UUID(SESSION), "Blue Herons", START).participant

    assert find_participant(store, repository(), UUID(SESSION), UUID(int=7)) is None  # never joined
    assert find_participant(store, repository(), UUID(OTHER_SESSION), joined) is None  # elsewhere
    # The moderator removed the team from the file.
    assert find_participant(store, repository(with_heron=False), UUID(SESSION), joined) is None
