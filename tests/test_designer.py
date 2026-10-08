"""The hunt designer's API, with the stub runner."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.rows import DictRow

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.designer import route_of
from game_server.designer_runners import StubRunner, get_designer_runner, stub_checkpoints
from game_server.drafts import DraftRequest, DraftResult, DraftStore
from game_server.geo import distance_m

KEY = "SENTINEL-ORGANISER-KEY-7c1e9a"
AUTH = {"Authorization": f"Bearer {KEY}"}
T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
BODY = {"area": "Chiswick, London", "theme": "The Thames and brewing history"}


@pytest.fixture
def now() -> list[datetime]:
    return [T0]


@pytest.fixture
def runner(database: Database, now: list[datetime]) -> Iterator[StubRunner]:
    """The stub, waiting until a test wakes it (never longer than the test)."""
    stub = StubRunner(DraftStore(database), lambda: now[0], delay_seconds=60)
    yield stub
    stub.stop()


def designer_app(settings: Settings, now: list[datetime], runner: StubRunner | None) -> Any:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    if runner is not None:
        app.dependency_overrides[get_designer_runner] = lambda: runner
    return app


@pytest.fixture
def client(now: list[datetime], runner: StubRunner) -> Iterator[TestClient]:
    settings = Settings(organiser_key=KEY, designer_runner="stub")
    with TestClient(designer_app(settings, now, runner)) as test_client:
        yield test_client


def start(client: TestClient, body: dict[str, Any] | None = None) -> Response:
    return client.post("/designer/drafts", json=BODY if body is None else body, headers=AUTH)


def read(client: TestClient, draft: str) -> dict[str, Any]:
    response = client.get(f"/designer/drafts/{draft}", headers=AUTH)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def finish(runner: StubRunner) -> None:
    runner.wake()
    runner.join()


def drafts_in(db: psycopg.Connection[DictRow]) -> int:
    row = db.execute("SELECT COUNT(*) AS n FROM hunt_drafts").fetchone()
    assert row is not None
    return int(row["n"])


# --- starting and following a design -------------------------------------------------------


def test_start_returns_202_with_the_id_and_location(client: TestClient) -> None:
    response = start(client)

    assert response.status_code == 202
    body = response.json()
    assert body == {"id": body["id"], "status": "running"}
    assert UUID(body["id"])
    assert response.headers["location"] == f"/designer/drafts/{body['id']}"


def test_a_running_draft(client: TestClient) -> None:
    draft = start(client, {**BODY, "checkpoints": 4, "max-walk-km": 2.5}).json()["id"]

    assert read(client, draft) == {
        "id": draft,
        "status": "running",
        "request": {
            "area": "Chiswick, London",
            "theme": "The Thames and brewing history",
            "checkpoints": 4,
            "max-walk-km": 2.5,
        },
        "area": None,
        "progress": [],
        "checkpoints": [],
        "route": None,
        "problems": [],
        "run": {
            "runner": "stub",
            "model": None,
            "turns": 0,
            "cost-usd": 0.0,
            "duration-ms": None,
            "error": None,
        },
        "attribution": "© OpenStreetMap contributors",
        "published": None,
        "created-at": "2026-10-08T09:00:00Z",
        "finished-at": None,
    }


def test_the_stub_makes_a_ready_draft_with_three_checkpoints(
    client: TestClient, runner: StubRunner, now: list[datetime]
) -> None:
    draft = start(client).json()["id"]
    now[0] = T0 + timedelta(seconds=1)
    finish(runner)

    body = read(client, draft)

    assert body["status"] == "ready"
    assert body["finished-at"] == "2026-10-08T09:00:01Z"
    assert body["area"]["name"] == "Chiswick, London (stub)"
    assert set(body["area"]["bbox"]) == {"south", "west", "north", "east"}
    assert body["area"]["clipped"] is False
    assert [step["step"] for step in body["progress"]] == [
        "resolve_area",
        "find_places",
        "write_clues",
        "check_draft",
    ]
    assert set(body["progress"][0]) == {"at", "step", "summary"}
    assert [c["position"] for c in body["checkpoints"]] == [1, 2, 3]
    [first, *_] = body["checkpoints"]
    assert set(first) == {
        "position",
        "place",
        "clue",
        "challenge",
        "proximity",
        "rationale",
        "review",
        "edited",
    }
    assert set(first["place"]) == {"osm", "name", "kind", "location"}
    assert set(first["place"]["location"]) == {"lat", "long"}
    assert set(first["challenge"]) == {"scene", "pose"}
    assert (first["review"], first["edited"]) == ("pending", False)
    assert len(body["route"]["legs-m"]) == 3
    assert body["route"]["loop-m"] == sum(body["route"]["legs-m"])
    assert body["problems"] == []
    assert body["run"]["runner"] == "stub"
    assert isinstance(body["run"]["duration-ms"], int)
    assert body["run"]["error"] is None


def test_the_list_is_newest_first(
    client: TestClient, runner: StubRunner, now: list[datetime]
) -> None:
    ids = []
    for n, area in enumerate(["First area", "Second area", "Third area"]):
        now[0] = T0 + timedelta(minutes=n)
        ids.append(start(client, {**BODY, "area": area}).json()["id"])
        finish(runner)
    now[0] = T0 + timedelta(minutes=10)

    drafts = client.get("/designer/drafts", headers=AUTH).json()["drafts"]

    assert [d["id"] for d in drafts] == ids[::-1]
    assert drafts[0] == {
        "id": ids[2],
        "status": "ready",
        "area": "Third area",
        "theme": "The Thames and brewing history",
        "created-at": "2026-10-08T09:02:00Z",
        "finished-at": "2026-10-08T09:02:00Z",
        "checkpoints": 3,
        "cost-usd": 0.0,
    }


def test_a_running_draft_in_the_list(client: TestClient) -> None:
    draft = start(client).json()["id"]

    [listed] = client.get("/designer/drafts", headers=AUTH).json()["drafts"]

    assert (listed["id"], listed["status"], listed["checkpoints"]) == (draft, "running", 0)
    assert listed["finished-at"] is None


def test_the_list_holds_at_most_fifty(client: TestClient, database: Database) -> None:
    store = DraftStore(database)
    for n in range(52):
        draft = uuid4()
        store.create(draft, DraftRequest.model_validate(BODY), "stub", T0 + timedelta(seconds=n))
        store.finish(draft, failed(), T0 + timedelta(seconds=n))

    drafts = client.get("/designer/drafts", headers=AUTH).json()["drafts"]

    assert len(drafts) == 50
    assert drafts[0]["created-at"] == "2026-10-08T09:00:51Z"


def failed() -> DraftResult:
    return DraftResult(
        status="failed",
        area=None,
        checkpoints=(),
        problems=(),
        model=None,
        turns=0,
        cost_usd=Decimal(0),
        duration_ms=0,
        error_code="no_valid_draft",
    )


def test_a_failed_run_shows_its_error(client: TestClient, database: Database) -> None:
    draft = uuid4()
    store = DraftStore(database)
    store.create(draft, DraftRequest.model_validate(BODY), "agent", T0)
    store.finish(draft, failed(), T0)

    body = read(client, str(draft))

    assert body["status"] == "failed"
    assert body["run"]["error"] == {"code": "no_valid_draft"}


# --- refusals ----------------------------------------------------------------------------


def test_one_design_at_a_time(client: TestClient, db: psycopg.Connection[DictRow]) -> None:
    start(client)

    response = start(client, {**BODY, "area": "Somewhere else"})

    assert response.status_code == 409
    assert response.json() == {"detail": "a design is already running", "code": "designer_busy"}
    assert drafts_in(db) == 1


def test_another_design_can_start_once_the_first_is_ready(
    client: TestClient, runner: StubRunner
) -> None:
    start(client)
    finish(runner)

    assert start(client).status_code == 202


def test_the_agent_runner_is_unavailable(
    now: list[datetime], db: psycopg.Connection[DictRow]
) -> None:
    settings = Settings(organiser_key=KEY, designer_runner="agent")  # no Anthropic key either
    with TestClient(designer_app(settings, now, runner=None)) as test_client:
        response = start(test_client)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "the hunt designer is not available",
        "code": "designer_disabled",
    }
    assert drafts_in(db) == 0


SENTINEL_AREA = "SENTINEL-AREA-51d2"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**BODY, "area": SENTINEL_AREA, "checkpoints": 2}, id="two-checkpoints"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "checkpoints": 9}, id="nine-checkpoints"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "checkpoints": "3"}, id="string-count"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "checkpoints": 3.0}, id="float-count"),
        pytest.param({"area": SENTINEL_AREA, "theme": ""}, id="empty-theme"),
        pytest.param({"area": SENTINEL_AREA, "theme": "  ab  "}, id="theme-too-short"),
        pytest.param({**BODY, "area": SENTINEL_AREA + "x" * 200}, id="area-too-long"),
        pytest.param({"theme": SENTINEL_AREA}, id="no-area"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "max-walk-km": 0.4}, id="walk-too-short"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "max-walk-km": 11}, id="walk-too-long"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "max-walk-km": True}, id="walk-bool"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "max_walk_km": 3}, id="snake-case-key"),
        pytest.param({**BODY, "area": SENTINEL_AREA, "budget": 5}, id="unknown-field"),
    ],
)
def test_bad_bodies_are_422_without_echoing_them(
    client: TestClient, db: psycopg.Connection[DictRow], body: dict[str, Any]
) -> None:
    response = start(client, body)

    assert response.status_code == 422
    assert SENTINEL_AREA not in response.text
    assert drafts_in(db) == 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**BODY, "checkpoints": 3, "max-walk-km": 0.5}, id="smallest"),
        pytest.param({**BODY, "checkpoints": 8, "max-walk-km": 10}, id="largest"),
        pytest.param({"area": "abc", "theme": "x" * 200}, id="text-bounds"),
    ],
)
def test_bounds_are_inclusive(client: TestClient, body: dict[str, Any]) -> None:
    assert start(client, body).status_code == 202


def test_defaults_are_three_checkpoints_and_three_km(client: TestClient) -> None:
    draft = start(client).json()["id"]

    assert read(client, draft)["request"] == {**BODY, "checkpoints": 3, "max-walk-km": 3.0}


def test_unknown_draft_is_404(client: TestClient) -> None:
    response = client.get(f"/designer/drafts/{uuid4()}", headers=AUTH)

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown draft"}


def test_a_draft_id_that_isnt_a_uuid_is_422(client: TestClient) -> None:
    assert client.get("/designer/drafts/not-a-uuid", headers=AUTH).status_code == 422


# --- authorisation -----------------------------------------------------------------------

ROUTES = [
    ("POST", "/designer/drafts", BODY),
    ("GET", "/designer/drafts", None),
    ("GET", f"/designer/drafts/{uuid4()}", None),
]
UNAUTHORISED = {"detail": "organiser key required", "code": "organiser_unauthorised"}


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=["start", "list", "read"])
@pytest.mark.parametrize(
    "authorization",
    [None, f"Basic {KEY}", "Bearer wrong-key-but-long-enough-000"],
    ids=["no-header", "other-scheme", "wrong-key"],
)
def test_every_route_needs_the_key(
    client: TestClient,
    db: psycopg.Connection[DictRow],
    method: str,
    path: str,
    body: dict[str, Any] | None,
    authorization: str | None,
) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = client.request(method, path, json=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == UNAUTHORISED
    assert response.headers["www-authenticate"] == "Bearer"
    assert drafts_in(db) == 0


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=["start", "list", "read"])
def test_without_a_key_configured_every_route_is_401(
    now: list[datetime], runner: StubRunner, method: str, path: str, body: Any
) -> None:
    settings = Settings(designer_runner="stub")
    with TestClient(designer_app(settings, now, runner)) as test_client:
        response = test_client.request(method, path, json=body, headers=AUTH)

    assert response.status_code == 401
    assert response.json() == UNAUTHORISED


@pytest.mark.parametrize(
    ("path", "body"),
    [("/designer/drafts", {"checkpoints": 2}), ("/designer/drafts/not-a-uuid", None)],
    ids=["bad-body", "bad-id"],
)
def test_the_key_is_checked_before_the_request(
    client: TestClient, path: str, body: dict[str, Any] | None
) -> None:
    method = "POST" if body is not None else "GET"

    assert client.request(method, path, json=body).status_code == 401


# --- logging -----------------------------------------------------------------------------


def test_one_line_when_created_and_one_when_finished(
    client: TestClient, runner: StubRunner, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    draft = start(client).json()["id"]
    finish(runner)

    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name in ("game_server.designer", "game_server.designer_runners")
    ]
    assert lines == [
        f"Draft {draft} created status running runner stub",
        f"Draft {draft} finished status ready checkpoints 3 cost_usd 0",
    ]
    for checkpoint in read(client, draft)["checkpoints"]:
        for secret in (
            checkpoint["clue"],
            checkpoint["challenge"]["scene"],
            str(checkpoint["place"]["location"]["lat"]),
        ):
            assert secret not in caplog.text
    assert KEY not in caplog.text


# --- the route -----------------------------------------------------------------------------


def test_the_route_closes_the_loop() -> None:
    checkpoints = stub_checkpoints()
    a, b, c = (checkpoint.place.location for checkpoint in checkpoints)

    route = route_of(checkpoints)

    assert route is not None
    assert route.legs_m == [
        round(distance_m(a, b)),
        round(distance_m(b, c)),
        round(distance_m(c, a)),
    ]
    assert route.loop_m == sum(route.legs_m)


def test_the_route_follows_positions_not_storage_order() -> None:
    checkpoints = stub_checkpoints()

    assert route_of(checkpoints[::-1]) == route_of(checkpoints)


def test_no_route_without_checkpoints() -> None:
    assert route_of(()) is None
