"""The MCP hunt tools: design, review and publish a hunt, in-process with the stub runner."""

import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from mcp_fakes import NOW, call_tool, error_of, stub_runner, stub_services
from test_designer_review import HUNT, FixedCodes, ready_draft
from test_organiser_services import accept_all

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.designer.review import get_code_source
from game_server.designer_runners import StubRunner
from game_server.drafts import DraftStore
from game_server.mcp_server.services import McpServices
from game_server.organiser_services import DesignerDisabledError
from game_server.sessions import SessionRepository, get_session_repository

KEY = "SENTINEL-ORGANISER-KEY-7c1e9a"
DESIGN = {"area": "Chiswick, London", "theme": "Brewing"}
WEB = "https://play.example/"


@pytest.fixture
def runner(database: Database) -> Iterator[StubRunner]:
    with stub_runner(database) as stub:
        yield stub


@pytest.fixture
def settings() -> Settings:
    return Settings(organiser_key=KEY, web_app_url=WEB)


@pytest.fixture
def services(database: Database, runner: StubRunner) -> McpServices:
    return stub_services(database, runner)


@pytest.fixture
def store(database: Database) -> DraftStore:
    return DraftStore(database)


def ok(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    body: dict[str, Any] = result.structured_content
    return body


# --- start_hunt_design ---------------------------------------------------------------------------


def test_starting_a_design_returns_the_draft_and_its_link(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    started = ok(call_tool(services, settings, "start_hunt_design", DESIGN))

    draft = UUID(started["id"])
    assert started["status"] == "running"
    assert started["designer_link"] == f"https://play.example/designer?draft={draft}"
    found = store.get(draft)
    assert found is not None
    assert (found.status, found.request.checkpoints, found.request.max_walk_km) == ("running", 3, 3)


def test_the_request_is_taken_as_given(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    args = {**DESIGN, "checkpoints": 5, "max_walk_km": 1.5}

    draft = UUID(ok(call_tool(services, settings, "start_hunt_design", args))["id"])

    found = store.get(draft)
    assert found is not None
    assert (found.request.checkpoints, found.request.max_walk_km) == (5, 1.5)


@pytest.mark.parametrize(
    "bad",
    [
        {"checkpoints": 2},
        {"checkpoints": 9},
        {"max_walk_km": 0.4},
        {"max_walk_km": 11},
        {"area": "x"},
    ],
    ids=["few", "many", "short-walk", "long-walk", "short-area"],
)
def test_a_request_out_of_range_is_refused(
    services: McpServices, settings: Settings, store: DraftStore, bad: dict[str, Any]
) -> None:
    result = call_tool(services, settings, "start_hunt_design", {**DESIGN, **bad})

    assert result.is_error
    assert store.list(10) == []


def test_without_a_web_app_url_there_is_no_link(services: McpServices) -> None:
    started = ok(call_tool(services, Settings(), "start_hunt_design", DESIGN))

    assert started["designer_link"] is None


def test_designer_busy(services: McpServices, settings: Settings) -> None:
    ok(call_tool(services, settings, "start_hunt_design", DESIGN))

    error = error_of(call_tool(services, settings, "start_hunt_design", DESIGN))

    assert error == {"code": "designer_busy", "detail": "a design is already running"}


def test_designer_disabled(
    services: McpServices, settings: Settings, mocker: Any, store: DraftStore
) -> None:
    mocker.patch(
        "game_server.organiser_services.start_design",
        side_effect=DesignerDisabledError("the hunt designer is not available"),
    )

    error = error_of(call_tool(services, settings, "start_hunt_design", DESIGN))

    assert error["code"] == "designer_disabled"


# --- list_hunt_drafts and get_hunt_draft ---------------------------------------------------------


def test_drafts_are_listed_newest_first(
    services: McpServices, settings: Settings, store: DraftStore, database: Database
) -> None:
    older = ready_draft(store)
    newer = ready_draft(store)
    with database.transaction() as conn:  # both were created at the fixed time
        conn.execute(
            "UPDATE hunt_drafts SET created_at = created_at - interval '1 hour' WHERE id = %s",
            (older,),
        )

    listed = ok(call_tool(services, settings, "list_hunt_drafts"))

    assert [d["id"] for d in listed["drafts"]] == [str(newer), str(older)]
    assert listed["drafts"][0]["status"] == "ready"


def test_a_draft_is_returned_in_full(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)

    body = ok(call_tool(services, settings, "get_hunt_draft", {"draft_id": str(draft)}))

    assert body["id"] == str(draft)
    assert body["status"] == "ready"
    assert len(body["checkpoints"]) == 4
    assert body["route"]["loop-m"] > 0


def test_an_unknown_draft(services: McpServices, settings: Settings) -> None:
    error = error_of(call_tool(services, settings, "get_hunt_draft", {"draft_id": str(uuid4())}))

    assert error == {"code": "unknown_draft", "detail": "unknown draft"}


def test_waiting_returns_when_the_status_changes(
    services: McpServices, settings: Settings, runner: StubRunner
) -> None:
    draft = ok(call_tool(services, settings, "start_hunt_design", DESIGN))["id"]
    timer = threading.Timer(0.3, runner.wake)
    timer.start()

    body = ok(
        call_tool(services, settings, "get_hunt_draft", {"draft_id": draft, "wait_seconds": 15})
    )
    timer.join()

    assert body["status"] == "ready"


def test_waiting_gives_up_at_the_timeout(services: McpServices, settings: Settings) -> None:
    draft = ok(call_tool(services, settings, "start_hunt_design", DESIGN))["id"]

    body = ok(
        call_tool(services, settings, "get_hunt_draft", {"draft_id": draft, "wait_seconds": 0.6})
    )

    assert body["status"] == "running"


def test_a_finished_draft_is_returned_without_waiting(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)
    started = datetime.now(UTC)

    ok(
        call_tool(
            services, settings, "get_hunt_draft", {"draft_id": str(draft), "wait_seconds": 20}
        )
    )

    assert (datetime.now(UTC) - started).total_seconds() < 5


# --- edit_hunt_checkpoint ------------------------------------------------------------------------


def edit_args(draft: UUID, position: int = 1, **edit: Any) -> dict[str, Any]:
    return {"draft_id": str(draft), "position": position, "edit": edit}


def test_a_checkpoint_is_accepted(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)

    checkpoint = ok(
        call_tool(services, settings, "edit_hunt_checkpoint", edit_args(draft, review="accepted"))
    )

    assert (checkpoint["position"], checkpoint["review"]) == (1, "accepted")


def test_a_clue_is_edited_and_the_original_kept(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)
    original = store.get(draft).checkpoints[0].clue  # type: ignore[union-attr]  # just created

    checkpoint = ok(
        call_tool(
            services, settings, "edit_hunt_checkpoint", edit_args(draft, clue="By the water.")
        )
    )

    assert checkpoint["clue"] == "By the water."
    assert checkpoint["original"]["clue"] == original


def test_a_clue_naming_its_place_is_draft_problems(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)
    name = store.get(draft).checkpoints[0].place.name  # type: ignore[union-attr]

    error = error_of(
        call_tool(
            services, settings, "edit_hunt_checkpoint", edit_args(draft, clue=f"It is {name}.")
        )
    )

    assert error["code"] == "draft_problems"
    assert error["problems"]


def test_editing_an_unknown_draft_or_checkpoint(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)

    unknown_draft = call_tool(
        services, settings, "edit_hunt_checkpoint", edit_args(uuid4(), review="accepted")
    )
    unknown_checkpoint = call_tool(
        services, settings, "edit_hunt_checkpoint", edit_args(draft, 99, review="accepted")
    )

    assert error_of(unknown_draft)["code"] == "unknown_draft"
    assert error_of(unknown_checkpoint)["code"] == "unknown_checkpoint"


def test_only_a_ready_draft_is_edited(services: McpServices, settings: Settings) -> None:
    draft = ok(call_tool(services, settings, "start_hunt_design", DESIGN))["id"]

    result = call_tool(
        services,
        settings,
        "edit_hunt_checkpoint",
        {"draft_id": draft, "position": 1, "edit": {"review": "accepted"}},
    )

    assert error_of(result) == {"code": "draft_not_editable", "detail": "draft can't be edited"}


def test_an_empty_edit_is_refused(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)

    assert call_tool(services, settings, "edit_hunt_checkpoint", edit_args(draft)).is_error


# --- publish_hunt --------------------------------------------------------------------------------


def publish_args(draft: UUID, **changes: Any) -> dict[str, Any]:
    return {"draft_id": str(draft), "request": {**HUNT, **changes}}


def test_publishing_returns_the_session_and_its_codes(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)

    publication = ok(call_tool(services, settings, "publish_hunt", publish_args(draft)))

    assert publication["name"] == HUNT["name"]
    assert [team["name"] for team in publication["teams"]] == HUNT["teams"]
    assert all(team["join-code"] for team in publication["teams"])
    assert publication["moderator-code"]
    found = store.get(draft)
    assert found is not None
    assert (found.status, str(found.published_session)) == ("published", publication["session"])


def test_publishing_an_unknown_draft(services: McpServices, settings: Settings) -> None:
    result = call_tool(services, settings, "publish_hunt", publish_args(uuid4()))

    assert error_of(result)["code"] == "unknown_draft"


def test_publishing_with_checkpoints_pending_review(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)

    error = error_of(call_tool(services, settings, "publish_hunt", publish_args(draft)))

    assert error["code"] == "draft_not_ready"
    assert "4 still pending review" in error["detail"]


def test_publishing_twice(services: McpServices, settings: Settings, store: DraftStore) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)
    ok(call_tool(services, settings, "publish_hunt", publish_args(draft)))

    error = error_of(call_tool(services, settings, "publish_hunt", publish_args(draft)))

    assert error == {"code": "draft_not_ready", "detail": "draft is already published"}


def test_publishing_checkpoints_that_break_the_rules(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    from test_designer_review import close_to_first

    from game_server.designer_runners import stub_checkpoints

    draft = ready_draft(store, (*stub_checkpoints(), close_to_first()))
    accept_all(store, draft)

    error = error_of(call_tool(services, settings, "publish_hunt", publish_args(draft)))

    assert error["code"] == "draft_problems"
    assert error["problems"]


def test_an_invalid_hunt_is_refused(
    services: McpServices, settings: Settings, store: DraftStore
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)

    result = call_tool(services, settings, "publish_hunt", publish_args(draft, teams=[]))

    assert result.is_error
    assert store.get(draft).status == "ready"  # type: ignore[union-attr]  # just created


def test_the_codes_are_never_logged(
    services: McpServices,
    settings: Settings,
    store: DraftStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    draft = ready_draft(store)
    accept_all(store, draft)
    caplog.set_level(logging.DEBUG)

    publication = ok(call_tool(services, settings, "publish_hunt", publish_args(draft)))

    codes = [publication["moderator-code"], *(team["join-code"] for team in publication["teams"])]
    assert "published as session" in caplog.text
    for code in codes:
        assert code not in caplog.text


# --- the whole flow, and parity with the routes -----------------------------------------------


def test_design_review_and_publish_a_playable_session(
    database: Database, settings: Settings, runner: StubRunner
) -> None:
    repository = SessionRepository().beside(
        __import__("game_server.published_sessions", fromlist=["x"]).PublishedSessionRows(database)
    )
    services = stub_services(database, runner, repository)

    started = ok(call_tool(services, settings, "start_hunt_design", DESIGN))
    draft = started["id"]
    assert (
        ok(call_tool(services, settings, "get_hunt_draft", {"draft_id": draft}))["status"]
        == "running"
    )
    runner.wake()
    runner.join()
    ready = ok(
        call_tool(services, settings, "get_hunt_draft", {"draft_id": draft, "wait_seconds": 5})
    )
    assert ready["status"] == "ready"
    for checkpoint in ready["checkpoints"]:
        edit = {
            "draft_id": draft,
            "position": checkpoint["position"],
            "edit": {"review": "accepted"},
        }
        ok(call_tool(services, settings, "edit_hunt_checkpoint", edit))
    publication = ok(call_tool(services, settings, "publish_hunt", publish_args(UUID(draft))))

    session_id = UUID(publication["session"])
    found = repository.find_team(publication["teams"][0]["join-code"])
    assert found is not None
    assert (found[0].id, found[1].name) == (session_id, "Red Foxes")
    assert repository.moderator_code(session_id) is not None
    listed = ok(call_tool(services, settings, "list_sessions"))
    assert [(s["id"], s["source"], s["phase"]) for s in listed["sessions"]] == [
        (str(session_id), "published", "scheduled")
    ]


def test_the_route_and_the_tool_publish_equal_sessions(
    database: Database, settings: Settings, runner: StubRunner, store: DraftStore
) -> None:
    repository = SessionRepository().beside(
        __import__("game_server.published_sessions", fromlist=["x"]).PublishedSessionRows(database)
    )
    via_route, via_tool = ready_draft(store), ready_draft(store)
    accept_all(store, via_route)
    accept_all(store, via_tool)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_code_source] = lambda: FixedCodes()
    services = stub_services(database, runner, repository)
    # Inside the `with`: leaving it closes the app's database pool, which the tool shares.
    with TestClient(app) as client:
        response = client.post(
            f"/designer/drafts/{via_route}/publish",
            json=HUNT,
            headers={"Authorization": f"Bearer {KEY}"},
        )
        tool = ok(call_tool(services, settings, "publish_hunt", publish_args(via_tool)))
    assert response.status_code == 201

    def comparable(publication: dict[str, Any]) -> dict[str, Any]:
        session = repository.get_session(UUID(publication["session"]))
        assert session is not None
        document = session.model_dump(mode="json", by_alias=True)
        for credential in ("id", "moderator-code"):
            document.pop(credential)
        for team in document["teams"]:
            team.pop("join-code")
        return document

    assert comparable(tool) == comparable(response.json())
