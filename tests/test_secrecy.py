"""No endpoint may reveal a checkpoint's scene description (the answer to its clue)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from starlette.routing import Route

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.sessions import get_session_repository, parse_sessions

SENTINEL = "SENTINEL-SCENE-4b1d"
SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
UNKNOWN = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
NOW = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)
PHOTO = jpeg(scene(3))

REPOSITORY = parse_sessions(
    json.dumps(
        [
            {
                "id": SESSION,
                "name": "Hunt",
                "location": "Here",
                "start-time": "2026-10-03T10:00:00Z",
                "end-time": "2026-10-03T12:00:00Z",
                "checkpoints": [
                    {
                        "sequence": 1,
                        "name": "Spot",
                        "clue": "Find it",
                        "location": {"lat": 51.5, "long": -0.1},
                        "proximity": 40,
                        "challenge": {"scene": f"{SENTINEL} a fountain", "pose": "Wave."},
                    }
                ],
            }
        ]
    )
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=tmp_path / "images", db_path=tmp_path / "game.sqlite3")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: REPOSITORY
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    with TestClient(app) as test_client:
        yield test_client


def metadata(**changes: Any) -> str:
    return json.dumps(
        {
            "session": SESSION,
            "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
            "checkpoint": 1,
            "location": {"lat": 51.5001, "long": -0.1},
            "capture-time": "2026-10-03T10:29:00Z",
            **changes,
        }
    )


def submit(client: TestClient, raw_metadata: str, image: bytes = PHOTO) -> Response:
    return client.post(
        "/challenge",
        files={
            "metadata": (None, raw_metadata, "application/json"),
            "challenge-image": ("photo.jpeg", image, "image/jpeg"),
        },
    )


def hint(client: TestClient, **changes: Any) -> Response:
    body = {
        "session": SESSION,
        "participant": "5d0a8b8e-7f6c-4d4b-8f0e-2b1a9c3d4e5f",
        "checkpoint": 1,
        "location": {"lat": 51.5001, "long": -0.1},
        **changes,
    }
    return client.post("/checkpoint/proximity", json=body)


def every_route_response(client: TestClient) -> dict[tuple[str, str], list[Response]]:
    """Successful and failing calls to every route, keyed by (method, route path)."""
    pose = "/sessions/{session}/checkpoints/{sequence}/challenge"
    return {
        ("GET", "/"): [client.get("/")],
        ("POST", "/challenge"): [
            submit(client, metadata()),  # pending
            submit(client, metadata(location={"lat": 51.51, "long": -0.1})),  # failed checks
            submit(client, metadata(), image=PHOTO),  # duplicate
            submit(client, metadata(checkpoint=2)),  # 404
            submit(client, metadata(extra=True)),  # 422
            submit(client, metadata(), image=b"\xff\xd8\xffjunk"),  # 422 undecodable
        ],
        ("POST", "/checkpoint/proximity"): [
            hint(client),  # 200
            hint(client),  # 429
            hint(client, session=UNKNOWN),  # 404
            hint(client, checkpoint="1"),  # 422
        ],
        ("GET", pose): [
            client.get(f"/sessions/{SESSION}/checkpoints/1/challenge"),
            client.get(f"/sessions/{SESSION}/checkpoints/2/challenge"),  # 404
            client.get(f"/sessions/{SESSION}/checkpoints/0/challenge"),  # 422
        ],
        ("GET", "/openapi.json"): [client.get("/openapi.json")],
        ("GET", "/docs"): [client.get("/docs")],
        ("GET", "/docs/oauth2-redirect"): [client.get("/docs/oauth2-redirect")],
        ("GET", "/redoc"): [client.get("/redoc")],
    }


def app_routes(client: TestClient) -> set[tuple[str, str]]:
    """Every (method, path) the app serves.

    API routes come from the OpenAPI schema (public, and it sees into included routers);
    the docs routes are excluded from the schema, so they come from the top-level routes.
    """
    app: FastAPI = client.app  # type: ignore[assignment]  # TestClient wraps our FastAPI app
    api = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    top_level = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in (route.methods or set()) - {"HEAD"}
    }
    return api | top_level


def test_no_route_ever_returns_the_scene(client: TestClient) -> None:
    responses = every_route_response(client)

    assert set(responses) == app_routes(client), "a route is missing from this test"
    for route, route_responses in responses.items():
        for response in route_responses:
            assert SENTINEL not in response.text, f"{route} leaked the scene"


def test_the_calls_cover_success_and_error_paths(client: TestClient) -> None:
    statuses = {
        route: sorted({r.status_code for r in rs})
        for route, rs in every_route_response(client).items()
    }

    assert statuses[("POST", "/challenge")] == [200, 202, 404, 422]
    assert statuses[("POST", "/checkpoint/proximity")] == [200, 404, 422, 429]
    assert statuses[("GET", "/sessions/{session}/checkpoints/{sequence}/challenge")] == [
        200,
        404,
        422,
    ]
