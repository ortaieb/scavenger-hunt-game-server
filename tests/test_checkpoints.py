import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from game_server.app import create_app
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
UNKNOWN_SESSION = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
POSE = "Side profile, looking to your left, with the landmark behind you."
SCENE = "SECRET-SCENE a granite fountain in open lawn"


def sessions_repository() -> SessionRepository:
    checkpoint = {
        "name": "SECRET-NAME",
        "clue": "SECRET-CLUE",
        "location": {"lat": 51.504873, "long": -0.169872},
        "proximity": 40,
    }
    return parse_sessions(
        json.dumps(
            [
                {
                    "id": SESSION,
                    "name": "Hunt",
                    "location": "Here",
                    "start-time": "2026-10-03T10:00:00Z",
                    "end-time": "2026-10-03T12:00:00Z",
                    "checkpoints": [
                        {**checkpoint, "sequence": 1, "challenge": {"scene": SCENE, "pose": POSE}},
                        {**checkpoint, "sequence": 2},
                    ],
                }
            ]
        )
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    repository = sessions_repository()
    app.dependency_overrides[get_session_repository] = lambda: repository
    with TestClient(app) as test_client:
        yield test_client


def url(session: str = SESSION, sequence: object = 1) -> str:
    return f"/sessions/{session}/checkpoints/{sequence}/challenge"


def test_returns_the_pose(client: TestClient) -> None:
    response = client.get(url())

    assert response.status_code == 200
    assert response.json() == {"pose": POSE}


def test_pose_is_null_without_a_challenge(client: TestClient) -> None:
    response = client.get(url(sequence=2))

    assert response.status_code == 200
    assert response.json() == {"pose": None}


@pytest.mark.parametrize("sequence", [1, 2])
def test_pose_is_the_only_field(client: TestClient, sequence: int) -> None:
    response = client.get(url(sequence=sequence))

    assert set(response.json()) == {"pose"}
    for secret in ("SECRET-SCENE", "SECRET-NAME", "SECRET-CLUE", "51.504873", "10:00"):
        assert secret not in response.text


def test_schema_has_only_the_pose(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert set(schemas["PoseInstruction"]["properties"]) == {"pose"}


@pytest.mark.parametrize(
    ("session", "sequence", "detail"),
    [(UNKNOWN_SESSION, 1, "unknown session"), (SESSION, 3, "unknown checkpoint")],
)
def test_unknown_is_404(client: TestClient, session: str, sequence: int, detail: str) -> None:
    response = client.get(url(session, sequence))

    assert response.status_code == 404
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize(
    ("session", "sequence"),
    [("not-a-uuid", 1), (SESSION, "one"), (SESSION, 0), (SESSION, -1), (SESSION, 1.5)],
)
def test_malformed_path_is_422(client: TestClient, session: str, sequence: object) -> None:
    assert client.get(url(session, sequence)).status_code == 422
