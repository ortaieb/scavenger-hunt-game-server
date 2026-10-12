"""The MCP `override_verdict` tool: rule on a submission from Claude, with the route's service."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from mcp_fakes import NOW, call_tool, error_of, stub_services
from psycopg.rows import DictRow
from test_mcp_server import AUTH, rpc
from test_ruling import FOX, MODERATOR, OTHER, OTHER_MODERATOR, SESSION, session_json, submit

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.mcp_server.services import McpServices
from game_server.ruling import NOTE_MAX_LENGTH
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

NOTE = "SENTINEL-NOTE-a3 the arm is cropped"
UNKNOWN = "00000000-0000-4000-8000-000000000000"


def sessions_json() -> str:
    return json.dumps(
        [session_json(SESSION, MODERATOR), session_json(OTHER, OTHER_MODERATOR, "-OTHER")]
    )


@pytest.fixture
def repository() -> SessionRepository:
    return parse_sessions(sessions_json())


@pytest.fixture
def services(database: Database, repository: SessionRepository) -> McpServices:
    return stub_services(database, repository=repository)


@pytest.fixture
def participant(store: SubmissionStore) -> str:
    return str(store.join_team(UUID(SESSION), "Red Foxes", NOW).participant)


def override(
    services: McpServices, submission: int, ruling: str = "approve", **arguments: Any
) -> Any:
    return call_tool(
        services,
        Settings(),
        "override_verdict",
        {"session_id": SESSION, "submission_id": submission, "ruling": ruling, **arguments},
    )


def ok(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    body: dict[str, Any] = result.structured_content
    return body


def rulings_of(db: psycopg.Connection[DictRow]) -> list[dict[str, Any]]:
    return db.execute("SELECT * FROM rulings ORDER BY id").fetchall()


# --- the outcomes --------------------------------------------------------------------------------


def test_approving_a_failed_photo(
    services: McpServices, store: SubmissionStore, participant: str
) -> None:
    submission = submit(store, participant, 1, "failed")

    body = ok(override(services, submission, "approve", note=NOTE))

    assert body == {
        "submission": submission,
        "verdict": "failed",
        "ruling": {"ruling": "approve", "note": NOTE, "ruled-at": "2026-10-10T09:00:00Z"},
        "effective-verdict": "pass",
        "replaced": False,
    }


def test_rejecting_a_passed_photo(
    services: McpServices, store: SubmissionStore, participant: str
) -> None:
    submission = submit(store, participant, 1, "pass")

    body = ok(override(services, submission, "reject"))

    assert (body["verdict"], body["effective-verdict"], body["ruling"]["note"]) == (
        "pass",
        "failed",
        None,
    )


def test_ruling_on_a_pending_photo(
    services: McpServices, store: SubmissionStore, participant: str
) -> None:
    submission = submit(store, participant, 1, "pending")

    body = ok(override(services, submission, "approve"))

    assert (body["verdict"], body["effective-verdict"]) == ("pending", "pass")


def test_ruling_again_replaces_the_ruling_and_keeps_the_earlier_one(
    services: McpServices,
    store: SubmissionStore,
    participant: str,
    db: psycopg.Connection[DictRow],
) -> None:
    submission = submit(store, participant, 1, "failed")
    ok(override(services, submission, "approve", note="first"))

    body = ok(override(services, submission, "reject", note="second"))

    assert (body["replaced"], body["effective-verdict"]) == (True, "failed")
    assert [(r["ruling"], r["note"]) for r in rulings_of(db)] == [
        ("approve", "first"),
        ("reject", "second"),
    ]


def test_the_referees_verdict_is_never_changed(
    services: McpServices,
    store: SubmissionStore,
    participant: str,
    db: psycopg.Connection[DictRow],
) -> None:
    submission = submit(store, participant, 1, "failed")

    ok(override(services, submission, "approve"))

    row = db.execute("SELECT verdict FROM submissions WHERE id = %s", (submission,)).fetchone()
    assert row == {"verdict": "failed"}


def test_it_works_after_the_session_stops(
    services: McpServices, store: SubmissionStore, participant: str
) -> None:
    submission = submit(store, participant, 1, "pending")
    store.start_run(UUID(SESSION), NOW)
    store.stop_run(UUID(SESSION), NOW)

    assert ok(override(services, submission))["effective-verdict"] == "pass"


# --- errors --------------------------------------------------------------------------------------


def test_an_unknown_submission(services: McpServices) -> None:
    error = error_of(override(services, 99999))

    assert error == {"code": "unknown_submission", "detail": "unknown submission"}


def test_another_sessions_submission_is_unknown(
    services: McpServices, store: SubmissionStore
) -> None:
    other = str(store.join_team(UUID(OTHER), "Red Foxes", NOW).participant)
    submission = submit(store, other, 1, "pending", session=OTHER)

    assert error_of(override(services, submission))["code"] == "unknown_submission"


def test_an_unknown_session(
    services: McpServices, store: SubmissionStore, participant: str
) -> None:
    submission = submit(store, participant, 1, "pending")

    result = call_tool(
        services,
        Settings(),
        "override_verdict",
        {"session_id": UNKNOWN, "submission_id": submission, "ruling": "approve"},
    )

    assert error_of(result)["code"] == "unknown_session"


@pytest.mark.parametrize(
    "bad",
    [{"ruling": "maybe"}, {"note": "x" * (NOTE_MAX_LENGTH + 1)}, {"submission_id": 0}],
    ids=["ruling", "long-note", "submission"],
)
def test_bad_arguments_are_refused_and_nothing_is_recorded(
    services: McpServices,
    store: SubmissionStore,
    participant: str,
    db: psycopg.Connection[DictRow],
    bad: dict[str, Any],
) -> None:
    submission = submit(store, participant, 1, "pending")
    arguments = {"session_id": SESSION, "submission_id": submission, "ruling": "approve", **bad}

    result = call_tool(services, Settings(), "override_verdict", arguments)

    assert result.is_error
    assert rulings_of(db) == []


def test_a_note_of_the_longest_length_is_kept(
    services: McpServices, store: SubmissionStore, participant: str
) -> None:
    submission = submit(store, participant, 1, "pending")

    body = ok(override(services, submission, note="x" * NOTE_MAX_LENGTH))

    assert len(body["ruling"]["note"]) == NOTE_MAX_LENGTH


# --- parity with the route, and the log ----------------------------------------------------


def test_the_route_and_the_tool_store_the_same_row(
    database: Database,
    repository: SessionRepository,
    store: SubmissionStore,
    participant: str,
    db: psycopg.Connection[DictRow],
) -> None:
    via_route = submit(store, participant, 1, "failed")
    via_tool = submit(store, participant, 2, "failed")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings()
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    services = stub_services(database, repository=repository)
    # Inside the `with`: leaving it closes the app's database pool, which the tool shares.
    with TestClient(app) as client:
        response = client.post(
            f"/sessions/{SESSION}/submissions/{via_route}/ruling",
            json={"ruling": "reject", "note": NOTE},
            headers={"Authorization": f"Bearer {MODERATOR}"},
        )
        tool = ok(override(services, via_tool, "reject", note=NOTE))
    assert response.status_code == 201

    route_row, tool_row = rulings_of(db)
    assert (route_row["submission_id"], tool_row["submission_id"]) == (via_route, via_tool)
    for row in (route_row, tool_row):
        del row["id"], row["submission_id"]
    assert route_row == tool_row
    assert tool["ruling"] == {
        k: response.json()["ruling"][k] for k in ("ruling", "note", "ruled-at")
    }
    assert tool["effective-verdict"] == response.json()["effective-verdict"]


@pytest.fixture
def mcp_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    sessions = tmp_path / "sessions.json"
    sessions.write_text(sessions_json())
    monkeypatch.setenv("GAME_SERVER_SESSIONS_FILE", str(sessions))
    monkeypatch.setenv("GAME_SERVER_MCP_ENABLED", "true")
    monkeypatch.setenv("GAME_SERVER_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("GAME_SERVER_ORGANISER_KEY", AUTH["Authorization"].removeprefix("Bearer "))
    with TestClient(create_app()) as client:
        yield client


def test_the_note_is_never_logged(
    mcp_app: TestClient,
    store: SubmissionStore,
    participant: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    submission = submit(store, participant, 1, "failed")
    caplog.set_level(logging.DEBUG)
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "override_verdict",
            "arguments": {
                "session_id": SESSION,
                "submission_id": submission,
                "ruling": "approve",
                "note": NOTE,
            },
        },
    }

    response = rpc(mcp_app, call, **AUTH)

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert f"submission {submission} approve verdict failed -> pass first=yes" in caplog.text
    assert "SENTINEL-NOTE-a3" not in caplog.text
    assert FOX not in caplog.text
