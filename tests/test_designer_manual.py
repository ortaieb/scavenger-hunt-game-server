"""Manual hunt drafts: `POST /designer/drafts/manual`, and the same through the MCP tool."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from conftest import SignIn
from fastapi.testclient import TestClient
from mcp_fakes import call_tool, error_of, stub_services
from test_designer_review import AUTH, HUNT, NOW, FixedCodes, file_sessions

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.designer.review import get_code_source
from game_server.drafts import DraftRequest, DraftStore
from game_server.mcp_server.services import McpServices
from game_server.published_sessions import PublishedSessionRows
from game_server.sessions import get_session_repository


@pytest.fixture
def store(database: Database) -> DraftStore:
    return DraftStore(database)


@pytest.fixture
def client(database: Database) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings()
    repository = file_sessions().beside(PublishedSessionRows(database))
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_code_source] = lambda: FixedCodes()
    with TestClient(app) as test_client:
        yield test_client


def pin(name: str, lat: float, long: float, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "location": {"lat": lat, "long": long},
        "clue": "Where oars were kept by the water.",
        "scene": "A wooden shed with wide doors.",
        "pose": "Row an imaginary boat",
        **extra,
    }


def hunt(**extra: Any) -> dict[str, Any]:
    return {
        "area": "Chiswick, London",
        "theme": "Riverside pubs",
        "checkpoints": [
            pin("The old boathouse", 51.4880, -0.2600, proximity=45),
            pin("The brewery gate", 51.4910, -0.2600),
            pin("The river steps", 51.4910, -0.2540),
        ],
        **extra,
    }


@pytest.fixture(autouse=True)
def organiser(sign_in: SignIn) -> None:
    AUTH.clear()
    AUTH.update(sign_in("organiser"))


def post(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/designer/drafts/manual", json=body, headers=AUTH)


def test_a_manual_draft_is_stored_ready_and_returned(
    client: TestClient, database: Database
) -> None:
    response = post(client, hunt(**{"max-walk-km": 2}))

    assert response.status_code == 201
    body = response.json()
    assert response.headers["Location"] == f"/designer/drafts/{body['id']}"
    assert client.get(response.headers["Location"], headers=AUTH).json() == body
    assert body["status"] == "ready"
    assert body["request"] == {
        "area": "Chiswick, London",
        "theme": "Riverside pubs",
        "checkpoints": 3,
        "max-walk-km": 2,
    }
    assert body["area"]["name"] == "Chiswick, London"
    assert body["area"]["clipped"] is False
    assert body["area"]["bbox"]["south"] < 51.4880
    assert body["area"]["bbox"]["east"] > -0.2540
    assert body["progress"] == body["problems"] == []
    assert body["run"]["runner"] == "manual"
    assert (body["run"]["turns"], body["run"]["cost-usd"]) == (0, 0)
    assert body["finished-at"] == body["created-at"]
    first = body["checkpoints"][0]
    assert first["position"] == 1
    assert first["place"] == {
        "osm": "manual/1",
        "name": "The old boathouse",
        "kind": "manual",
        "location": {"lat": 51.4880, "long": -0.2600},
    }
    assert (first["rationale"], first["review"], first["proximity"]) == ("", "pending", 45)
    with database.connection() as conn:
        pid = conn.execute("SELECT pid FROM hunt_drafts WHERE id = %s", (body["id"],)).fetchone()
    assert pid is not None and pid[0] is not None


def test_proximity_and_walk_default(client: TestClient) -> None:
    body = post(client, hunt()).json()

    assert body["request"]["max-walk-km"] == 10
    assert body["checkpoints"][1]["proximity"] == 30


def test_a_draft_that_breaks_a_rule_is_422_and_nothing_is_stored(
    client: TestClient, store: DraftStore
) -> None:
    body = hunt()
    body["checkpoints"][0]["clue"] = "Find the old boathouse."

    response = post(client, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "draft problems"
    assert [p["code"] for p in response.json()["problems"]] == ["names_place"]
    assert store.list(10) == []


def test_a_loop_longer_than_the_walk_is_refused(client: TestClient) -> None:
    response = post(client, hunt(**{"max-walk-km": 0.5}))

    assert [p["code"] for p in response.json()["problems"]] == ["route_too_long"]


@pytest.mark.parametrize(
    "change",
    [
        {"checkpoints": hunt()["checkpoints"][:2]},
        {"checkpoints": hunt()["checkpoints"] * 3},
        {"max-walk-km": 11},
        {"unknown": 1},
    ],
)
def test_bad_bodies_are_422(client: TestClient, change: dict[str, Any]) -> None:
    assert post(client, {**hunt(), **change}).status_code == 422


def test_it_works_beside_a_running_design(client: TestClient, store: DraftStore) -> None:
    store.create(uuid4(), DraftRequest(area="Kew", theme="Gardens"), "stub", NOW)

    assert post(client, hunt()).status_code == 201


def test_the_list_tells_manual_drafts_apart(client: TestClient) -> None:
    post(client, hunt())

    listed = client.get("/designer/drafts", headers=AUTH).json()["drafts"]

    assert [(d["runner"], d["checkpoints"]) for d in listed] == [("manual", 3)]


def test_a_manual_draft_is_reviewed_and_published_like_any_other(client: TestClient) -> None:
    draft = post(client, hunt()).json()["id"]
    for position in (1, 2, 3):
        patched = client.patch(
            f"/designer/drafts/{draft}/checkpoints/{position}",
            json={"review": "accepted"},
            headers=AUTH,
        )
        assert patched.status_code == 200

    published = client.post(f"/designer/drafts/{draft}/publish", json=HUNT, headers=AUTH)

    assert published.status_code == 201
    assert [t["name"] for t in published.json()["teams"]] == HUNT["teams"]


# --- the MCP tool ---------------------------------------------------------------------------


@pytest.fixture
def services(database: Database) -> McpServices:
    return stub_services(database, codes=FixedCodes())


def test_the_tool_stores_a_ready_manual_draft(services: McpServices, store: DraftStore) -> None:
    result = call_tool(services, Settings(), "create_manual_hunt_draft", hunt())

    assert not result.is_error, result.content
    assert result.structured_content is not None
    stored = store.get(result.structured_content["id"])
    assert stored is not None
    assert (stored.status, stored.runner, len(stored.checkpoints)) == ("ready", "manual", 3)


def test_the_tool_reports_draft_problems(services: McpServices, store: DraftStore) -> None:
    body = hunt()
    body["checkpoints"][0]["clue"] = "Find the old boathouse."

    error = error_of(call_tool(services, Settings(), "create_manual_hunt_draft", body))

    assert error["code"] == "draft_problems"
    assert store.list(10) == []
