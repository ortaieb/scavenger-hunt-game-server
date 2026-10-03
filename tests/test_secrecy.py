"""No endpoint may reveal a checkpoint's scene description (the answer to its clue)."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

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
JOIN_CODE = "SENTINEL-CODE-9X"  # a credential: no response may echo it
NAME = "SENTINEL-NAME-c3"  # checkpoint names are never shown
LATER_CLUE = "SENTINEL-LATER-CLUE-7e"  # the second clue on the route: not while on the first
PHOTO_NAME = "SENTINEL-PHOTO-fountain-north"  # a reference photo's file name shows the place
MODERATOR_CODE = "SENTINEL-MOD-4Q"  # a credential: never in a response or a log line
SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
UNKNOWN = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
NOW = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)
PHOTO = jpeg(scene(3))

SESSIONS_JSON = json.dumps(
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
                    "name": NAME,
                    "clue": "Find it",
                    "location": {"lat": 51.5, "long": -0.1},
                    "proximity": 40,
                    "challenge": {"scene": f"{SENTINEL} a fountain", "pose": "Wave."},
                    "reference-photos": [f"reference/{PHOTO_NAME}.jpg"],
                },
                {
                    "sequence": 2,
                    "name": "Second spot",
                    "clue": LATER_CLUE,
                    "location": {"lat": 51.6, "long": -0.2},
                    "proximity": 40,
                },
            ],
            "teams": [{"name": "Testers", "join-code": JOIN_CODE, "order": [1, 2]}],
            "moderator-code": MODERATOR_CODE,
        }
    ]
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=tmp_path / "images")
    app.dependency_overrides[get_settings] = lambda: settings
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / f"{PHOTO_NAME}.jpg").write_bytes(jpeg(scene(4, (64, 48))))
    repository = parse_sessions(SESSIONS_JSON, reference_dir=tmp_path)
    assert repository.reference_photos(UUID(SESSION), 1)  # really loaded
    app.dependency_overrides[get_session_repository] = lambda: repository
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
    state = "/sessions/{session}/participants/{participant}/state"
    arrive = "/sessions/{session}/participants/{participant}/arrive"
    joined = client.post("/join", json={"code": JOIN_CODE, "consent": True})  # 201
    participant = joined.json()["participant"]
    return {
        ("GET", "/"): [client.get("/")],
        ("POST", "/challenge"): [
            submit(client, metadata()),  # pending
            submit(client, metadata(location={"lat": 51.51, "long": -0.1})),  # failed checks
            submit(client, metadata(), image=PHOTO),  # duplicate
            submit(client, metadata(checkpoint=9)),  # 404
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
            client.get(f"/sessions/{SESSION}/checkpoints/9/challenge"),  # 404
            client.get(f"/sessions/{SESSION}/checkpoints/0/challenge"),  # 422
        ],
        ("GET", "/health"): [client.get("/health")],
        ("POST", "/join"): [
            joined,
            client.post("/join", json={"code": JOIN_CODE.lower(), "consent": True}),  # 200
            client.post("/join", json={"code": "NO-SUCH-CODE", "consent": True}),  # 404
            client.post("/join", json={"code": JOIN_CODE}),  # 422: consent missing
            client.post("/join", json={"code": JOIN_CODE, "consent": "true"}),  # 422
            client.post("/join", json={"code": JOIN_CODE, "consent": True, "x": 1}),  # 422
        ],
        ("GET", state): [
            client.get(f"/sessions/{SESSION}/participants/{participant}/state"),  # on its 1st
            client.get(f"/sessions/{SESSION}/participants/{UNKNOWN}/state"),  # 404
            client.get(f"/sessions/{UNKNOWN}/participants/{participant}/state"),  # 404
            client.get(f"/sessions/{SESSION}/participants/not-a-uuid/state"),  # 422
        ],
        ("POST", arrive): [
            client.post(f"/sessions/{SESSION}/participants/{participant}/arrive", json=body)
            for body in (
                {"checkpoint": 1},  # 201
                {"checkpoint": 1},  # 200: the active arrival
                {"checkpoint": 9},  # 404 unknown checkpoint
                {"checkpoint": 2},  # 409 not the team's current checkpoint
                {"checkpoint": "1"},  # 422
            )
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


def test_no_route_ever_returns_the_scene(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    responses = every_route_response(client)

    assert set(responses) == app_routes(client), "a route is missing from this test"
    for route, route_responses in responses.items():
        for response in route_responses:
            assert SENTINEL not in response.text, f"{route} leaked the scene"
            assert JOIN_CODE not in response.text, f"{route} leaked a join code"
            assert NAME not in response.text, f"{route} leaked a checkpoint name"
            assert LATER_CLUE not in response.text, f"{route} leaked a later clue"
            assert PHOTO_NAME not in response.text, f"{route} leaked a reference photo"
            assert "reference" not in response.text.lower(), f"{route} mentions reference photos"
            assert MODERATOR_CODE not in response.text, f"{route} leaked the moderator code"
    for secret in (MODERATOR_CODE, JOIN_CODE):
        assert secret not in caplog.text.upper(), "a credential reached the logs"


def test_the_calls_cover_success_and_error_paths(client: TestClient) -> None:
    statuses = {
        route: sorted({r.status_code for r in rs})
        for route, rs in every_route_response(client).items()
    }

    assert statuses[("POST", "/challenge")] == [200, 202, 404, 422]
    assert statuses[("POST", "/checkpoint/proximity")] == [200, 404, 422, 429]
    assert statuses[("POST", "/join")] == [200, 201, 404, 422]
    assert statuses[("POST", "/sessions/{session}/participants/{participant}/arrive")] == [
        200,
        201,
        404,
        409,
        422,
    ]
    assert statuses[("GET", "/sessions/{session}/participants/{participant}/state")] == [
        200,
        404,
        422,
    ]
    assert statuses[("GET", "/sessions/{session}/checkpoints/{sequence}/challenge")] == [
        200,
        404,
        422,
    ]


def test_arrive_reveals_no_place(client: TestClient) -> None:
    arrive_responses = every_route_response(client)[
        ("POST", "/sessions/{session}/participants/{participant}/arrive")
    ]

    for response in arrive_responses:
        for leak in ("51.5", "-0.1", "proximity", NAME, SENTINEL, LATER_CLUE):
            assert leak not in response.text
