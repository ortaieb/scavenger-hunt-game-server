"""The MCP game-day tools: start a session, finish it and read its overview."""

import json
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from mcp_fakes import NOW, call_tool, error_of, stub_services
from test_ruling import (
    FOX,
    HERON,
    LAT,
    LONG,
    MODERATOR,
    OTHER,
    OTHER_MODERATOR,
    SESSION,
    session_json,
    submit,
)

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.mcp_server.services import McpServices
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

UNKNOWN = "00000000-0000-4000-8000-000000000000"
HEADERS = {"Authorization": f"Bearer {MODERATOR}"}


@pytest.fixture
def repository() -> SessionRepository:
    return parse_sessions(
        json.dumps(
            [session_json(SESSION, MODERATOR), session_json(OTHER, OTHER_MODERATOR, "-OTHER")]
        )
    )


@pytest.fixture
def services(database: Database, repository: SessionRepository) -> McpServices:
    return stub_services(database, repository=repository)


def call(services: McpServices, tool: str, session: str = SESSION) -> Any:
    return call_tool(services, Settings(), tool, {"session_id": session})


def ok(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    body: dict[str, Any] = result.structured_content
    return body


# --- start_session -------------------------------------------------------------------------------


def test_starting_a_scheduled_session_returns_the_running_clock(services: McpServices) -> None:
    clock = ok(call(services, "start_session"))

    assert clock == {
        "phase": "running",
        "planned-start": "2026-10-03T09:00:00Z",
        "planned-end": "2026-10-03T12:00:00Z",
        "started-at": "2026-10-10T09:00:00Z",
        "stopped-at": None,
        "server-time": "2026-10-10T09:00:00Z",
        "changed": True,
    }


def test_starting_a_running_session_changes_nothing(services: McpServices) -> None:
    first = ok(call(services, "start_session"))

    again = ok(call(services, "start_session"))

    assert again == {**first, "changed": False}


def test_a_stopped_session_cant_start_again(services: McpServices) -> None:
    ok(call(services, "start_session"))
    ok(call(services, "stop_session"))

    error = error_of(call(services, "start_session"))

    assert error == {"code": "session_stopped", "detail": "session has ended"}


# --- stop_session --------------------------------------------------------------------------------


def test_stopping_a_running_session_is_final(services: McpServices) -> None:
    ok(call(services, "start_session"))

    clock = ok(call(services, "stop_session"))

    assert (clock["phase"], clock["stopped-at"], clock["changed"]) == (
        "stopped",
        "2026-10-10T09:00:00Z",
        True,
    )


def test_stopping_twice_answers_with_the_same_clock(services: McpServices) -> None:
    ok(call(services, "start_session"))
    first = ok(call(services, "stop_session"))

    again = ok(call(services, "stop_session"))

    assert again == {**first, "changed": False}


def test_a_session_that_never_started_cant_stop(services: McpServices) -> None:
    error = error_of(call(services, "stop_session"))

    assert error == {"code": "session_not_started", "detail": "session hasn't started"}


def test_stopping_is_marked_destructive_and_says_to_confirm(services: McpServices) -> None:
    import asyncio

    from mcp import Client

    from game_server.mcp_server.server import build_mcp_server

    async def tools() -> dict[str, Any]:
        async with Client(build_mcp_server(Settings(), services)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    tool = asyncio.run(tools())["stop_session"]

    assert tool.annotations is not None
    assert tool.annotations.destructive_hint is True
    assert "confirm with the organiser first" in (tool.description or "").lower()


@pytest.mark.parametrize("tool", ["start_session", "stop_session", "session_overview"])
def test_an_unknown_session(services: McpServices, tool: str) -> None:
    error = error_of(call(services, tool, UNKNOWN))

    assert error == {"code": "unknown_session", "detail": "unknown session"}


def test_sessions_run_independently(services: McpServices) -> None:
    ok(call(services, "start_session"))

    assert ok(call(services, "start_session", OTHER))["changed"] is True
    ok(call(services, "stop_session"))
    assert ok(call(services, "session_overview", OTHER))["session"]["phase"] == "running"


# --- session_overview ----------------------------------------------------------------------------


def seed(store: SubmissionStore) -> None:
    fox = str(store.join_team(UUID(SESSION), "Red Foxes", NOW).participant)
    heron = str(store.join_team(UUID(SESSION), "Blue Herons", NOW).participant)
    submit(store, fox, 1, "pass")
    submit(store, fox, 2, "pending")
    submit(store, heron, 3, "failed")


def test_the_overview_shows_the_clock_the_queue_and_the_standings(
    services: McpServices, store: SubmissionStore
) -> None:
    seed(store)
    ok(call(services, "start_session"))

    overview = ok(call(services, "session_overview"))

    assert overview["session"]["phase"] == "running"
    assert overview["to-review"] == 1
    # Fewest points first: a failed photo costs a team more than a passed one.
    assert [(t["team"], t["points"], t["in-review"]) for t in overview["teams"]] == [
        ("Red Foxes", 7, 1),
        ("Blue Herons", 9, 0),
        ("Green Owls", None, None),
    ]
    assert overview["teams"][0]["current"] == {"sequence": 3, "name": "Spot 3"}


def test_the_overview_holds_no_coordinate_clue_scene_or_code(
    services: McpServices, store: SubmissionStore
) -> None:
    seed(store)

    text = json.dumps(ok(call(services, "session_overview")))

    for hidden in (str(LAT), str(LONG), "Clue 1", "Scene 1", FOX, HERON, MODERATOR):
        assert hidden not in text


# --- parity with the routes ----------------------------------------------------------------------


def test_the_routes_and_the_tools_answer_alike(
    database: Database,
    repository: SessionRepository,
    services: McpServices,
    store: SubmissionStore,
) -> None:
    seed(store)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings()
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    # Inside the `with`: leaving it closes the app's database pool, which the tool shares.
    with TestClient(app) as client:
        started = client.post(f"/sessions/{SESSION}/start", headers=HEADERS)
        route_overview = client.get(f"/sessions/{SESSION}/overview", headers=HEADERS)
        tool_overview = ok(call(services, "session_overview"))
        tool_again = ok(call(services, "start_session"))
        stopped = client.post(f"/sessions/{SESSION}/stop", headers=HEADERS)
        tool_stopped = ok(call(services, "stop_session"))

    assert started.status_code == 201
    assert tool_again == {**started.json(), "changed": False}
    assert tool_overview == route_overview.json()
    assert stopped.status_code == 201
    assert tool_stopped == {**stopped.json(), "changed": False}
