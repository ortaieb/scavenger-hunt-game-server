"""The organiser's MCP server: `/mcp` over HTTP in the app, and its tools in-process."""

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from mcp import Client
from mcp_fakes import call_tool
from test_designer_review import FixedCodes
from test_published_sessions import FILE_CODE, FILE_SESSION, START, game_session, session_json

from game_server.app import create_app
from game_server.config import Settings
from game_server.database import Database
from game_server.mcp_server.server import SDK_LOGGERS, build_mcp_server
from game_server.mcp_server.services import McpServices
from game_server.organiser import UNAUTHORISED_BODY
from game_server.published_sessions import PublishedSessionRows
from game_server.sessions import SessionRepository, parse_sessions
from game_server.submissions import SubmissionStore

KEY = "SENTINEL-ORGANISER-KEY-7c1e9a"
AUTH = {"Authorization": f"Bearer {KEY}"}
SNAPSHOT = Path(__file__).parent / "fixtures" / "mcp_tools.json"
# The SDK wants both; with `json_response` it answers in JSON.
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
PROTOCOL = "2025-06-18"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


@pytest.fixture
def enable_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_MCP_ENABLED", "true")
    monkeypatch.setenv("GAME_SERVER_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("GAME_SERVER_ORGANISER_KEY", KEY)


@pytest.fixture
def client(enable_mcp: None) -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def rpc(client: TestClient, body: dict[str, Any], **headers: str) -> Response:
    headers = {**MCP_HEADERS, "MCP-Protocol-Version": PROTOCOL, **headers}
    # Not following redirects: `/mcp` must answer itself, not redirect to `/mcp/`.
    return client.post("/mcp", json=body, headers=headers, follow_redirects=False)


# --- over HTTP ---------------------------------------------------------------------------------


def test_initialize_at_exactly_mcp(client: TestClient) -> None:
    response = rpc(client, INITIALIZE, **AUTH)

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["serverInfo"]["name"] == "scavenger-hunt"
    assert "poll the draft until it is `ready`" in result["instructions"]


def test_tools_list(client: TestClient) -> None:
    response = rpc(client, TOOLS_LIST, **AUTH)

    assert response.status_code == 200
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == [
        "list_sessions",
        "start_hunt_design",
        "list_hunt_drafts",
        "get_hunt_draft",
        "edit_hunt_checkpoint",
        "publish_hunt",
        "list_submissions",
        "get_submission",
        "override_verdict",
        "start_session",
        "stop_session",
        "session_overview",
    ]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-the-key-0000000000000"}],
    ids=["no-key", "wrong-key"],
)
def test_without_the_key_it_is_401(client: TestClient, headers: dict[str, str]) -> None:
    response = rpc(client, INITIALIZE, **headers)

    assert (response.status_code, response.json()) == (401, UNAUTHORISED_BODY)
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_with_no_key_configured_it_is_401(
    enable_mcp: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GAME_SERVER_ORGANISER_KEY")
    with TestClient(create_app()) as client:
        response = rpc(client, INITIALIZE, **AUTH)

    assert (response.status_code, response.json()) == (401, UNAUTHORISED_BODY)


def test_not_mounted_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_ORGANISER_KEY", KEY)
    with TestClient(create_app()) as client:
        response = rpc(client, INITIALIZE, **AUTH)

    assert response.status_code == 404


def test_an_unknown_host_is_refused(client: TestClient) -> None:
    response = rpc(client, INITIALIZE, Host="attacker.example", **AUTH)

    assert response.status_code == 421


def test_no_key_or_code_is_logged(
    client: TestClient, database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    session = game_session()
    SessionRepository().beside(PublishedSessionRows(database)).publish_session(session)
    caplog.set_level(logging.DEBUG)

    rpc(client, INITIALIZE, **AUTH)
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_sessions"}}
    assert rpc(client, call, **AUTH).status_code == 200
    # Last: a failure makes the client wait before its next attempt.
    rpc(client, INITIALIZE, Authorization="Bearer not-the-key-0000000000000")

    codes = [session.moderator_code or "", *(team.join_code for team in session.teams)]
    for secret in [KEY, "not-the-key", *codes]:
        assert secret not in caplog.text


def test_the_sdks_loggers_stay_at_warning(client: TestClient) -> None:
    for name in SDK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


# --- in-process --------------------------------------------------------------------------------


def list_sessions(repository: SessionRepository, settings: Settings) -> Any:
    services = McpServices(
        sessions=lambda: repository,
        drafts=lambda: pytest.fail("list_sessions doesn't touch drafts"),
        submissions=lambda: pytest.fail("list_sessions doesn't touch submissions"),
        runner=lambda: pytest.fail("list_sessions doesn't touch the runner"),
        codes=FixedCodes(),
        clock=lambda: START,
    )
    result = call_tool(services, settings, "list_sessions")
    assert not result.is_error
    return result.structured_content


def test_list_sessions_returns_file_and_published_sessions_with_their_phase(
    database: Database,
) -> None:
    file_session = session_json(FILE_SESSION, codes=(FILE_CODE,), moderator=None)
    repository = parse_sessions(json.dumps([file_session])).beside(PublishedSessionRows(database))
    published = game_session()
    repository.publish_session(published)
    SubmissionStore(database).start_run(published.id, START)
    settings = Settings(web_app_url="https://play.example/")

    listed = list_sessions(repository, settings)

    assert listed == {
        "sessions": [
            {
                "id": FILE_SESSION,
                "name": "Published hunt",
                "source": "file",
                "phase": "scheduled",
                "planned_start": "2026-10-10T09:00:00Z",
                "planned_end": "2026-10-10T12:00:00Z",
                "teams": 1,
                "moderator_link": f"https://play.example/moderator?session={FILE_SESSION}",
            },
            {
                "id": str(published.id),
                "name": "Published hunt",
                "source": "published",
                "phase": "running",
                "planned_start": "2026-10-10T09:00:00Z",
                "planned_end": "2026-10-10T12:00:00Z",
                "teams": 2,
                "moderator_link": f"https://play.example/moderator?session={published.id}",
            },
        ]
    }


def test_without_the_web_app_url_there_are_no_links() -> None:
    file_session = session_json(FILE_SESSION, codes=(FILE_CODE,), moderator=None)

    listed = list_sessions(parse_sessions(json.dumps([file_session])), Settings())

    assert listed["sessions"][0]["moderator_link"] is None


def no_services() -> McpServices:
    """Services for listing tools, which calls none."""
    return McpServices(
        sessions=SessionRepository,
        drafts=lambda: pytest.fail("not called"),
        submissions=lambda: pytest.fail("not called"),
        runner=lambda: pytest.fail("not called"),
        codes=FixedCodes(),
        clock=lambda: START,
    )


def test_the_tools_match_the_snapshot() -> None:
    """Renaming a tool or an argument is a reviewed change. After changing a tool, regenerate
    the snapshot and commit it: `UPDATE_MCP_SNAPSHOT=1 uv run pytest -k snapshot`."""

    async def tools() -> list[dict[str, Any]]:
        async with Client(build_mcp_server(Settings(), no_services())) as client:
            listed = await client.list_tools()
        return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in listed.tools]

    listed = asyncio.run(tools())
    if os.environ.get("UPDATE_MCP_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(listed, indent=2) + "\n")
    assert listed == json.loads(SNAPSHOT.read_text())
