"""require_moderator, exercised through a small test-only app (no real route uses it yet)."""

import json
import logging
import secrets
from collections.abc import Iterator
from typing import Annotated, Any
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pytest_mock import MockerFixture

from game_server import moderation
from game_server.moderation import UNAUTHORISED_BODY, require_moderator
from game_server.sessions import GameSession, get_session_repository, parse_sessions

SESSION_A = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
SESSION_B = "0b5e9c1e-2f7a-4d8e-9a57-3c1f6f0d2b44"
SESSION_NO_CODE = "5d0a8b8e-7f6c-4d4b-8f0e-2b1a9c3d4e5f"
CODE_A = "MOD-8H3T-QX"
CODE_B = "MOD-2KV9-ZP"


def session(session_id: str, moderator_code: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": session_id,
        "name": "Hunt",
        "location": "Here",
        "start-time": "2026-10-03T09:00:00Z",
        "end-time": "2026-10-03T12:00:00Z",
        "checkpoints": [
            {
                "sequence": 1,
                "name": "Spot",
                "clue": "Find it",
                "location": {"lat": 51.5, "long": -0.1},
                "proximity": 40,
            }
        ],
    }
    if moderator_code is not None:
        payload["moderator-code"] = moderator_code
    return payload


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    moderation.install(app)
    repository = parse_sessions(
        json.dumps(
            [session(SESSION_A, CODE_A), session(SESSION_B, CODE_B), session(SESSION_NO_CODE, None)]
        )
    )
    app.dependency_overrides[get_session_repository] = lambda: repository

    @app.get("/sessions/{session}/moderate")
    def moderate(found: Annotated[GameSession, Depends(require_moderator)]) -> dict[str, str]:
        return {"moderating": str(found.id)}

    with TestClient(app) as test_client:
        yield test_client


def call(
    client: TestClient, session_id: str = SESSION_A, authorization: str | None = None
) -> Response:
    headers = {} if authorization is None else {"Authorization": authorization}
    return client.get(f"/sessions/{session_id}/moderate", headers=headers)


def assert_unauthorised(response: Response) -> None:
    assert response.status_code == 401
    assert response.json() == UNAUTHORISED_BODY
    assert response.json() == {
        "detail": "moderator code required",
        "code": "moderator_unauthorised",
    }
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(f"Bearer {CODE_A}", id="exact"),
        pytest.param(f"Bearer {CODE_A.lower()}", id="lower-case-code"),
        pytest.param(f"bearer {CODE_A}", id="lower-case-scheme"),
        pytest.param(f"BEARER   {CODE_A}  ", id="surrounding-spaces"),
    ],
)
def test_the_right_code_is_accepted(client: TestClient, authorization: str) -> None:
    response = call(client, authorization=authorization)

    assert response.status_code == 200
    assert response.json() == {"moderating": SESSION_A}


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(None, id="no-header"),
        pytest.param("", id="empty-header"),
        pytest.param(f"Basic {CODE_A}", id="basic-scheme"),
        pytest.param(f"Token {CODE_A}", id="other-scheme"),
        pytest.param(CODE_A, id="no-scheme"),
        pytest.param("Bearer", id="bearer-without-code"),
        pytest.param("Bearer   ", id="bearer-blank-code"),
        pytest.param("Bearer MOD-WRONG-1", id="wrong-code"),
        pytest.param(f"Bearer {CODE_A}X", id="code-with-extra"),
    ],
)
def test_anything_else_is_401(client: TestClient, authorization: str | None) -> None:
    assert_unauthorised(call(client, authorization=authorization))


def test_a_code_works_for_its_own_session_only(client: TestClient) -> None:
    assert_unauthorised(call(client, SESSION_B, f"Bearer {CODE_A}"))
    assert call(client, SESSION_B, f"Bearer {CODE_B}").status_code == 200


@pytest.mark.parametrize("authorization", [None, f"Bearer {CODE_A}", f"Bearer {CODE_B}"])
def test_a_session_without_a_code_cannot_be_moderated(
    client: TestClient, authorization: str | None
) -> None:
    assert_unauthorised(call(client, SESSION_NO_CODE, authorization))


def test_unknown_session_is_404(client: TestClient) -> None:
    response = call(client, str(uuid4()), f"Bearer {CODE_A}")

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown session"}


def test_malformed_session_is_422(client: TestClient) -> None:
    assert call(client, "not-a-uuid", f"Bearer {CODE_A}").status_code == 422


def test_codes_are_compared_in_constant_time(client: TestClient, mocker: MockerFixture) -> None:
    compare = mocker.spy(secrets, "compare_digest")

    call(client, authorization=f"Bearer {CODE_A}")

    compare.assert_called_once_with(CODE_A.encode(), CODE_A.encode())


def test_the_code_is_never_in_a_response_or_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    responses = [
        call(client, authorization=f"Bearer {CODE_A}"),
        call(client, authorization=f"Bearer {CODE_A}-WRONG"),
        call(client, SESSION_B, f"Bearer {CODE_A}"),
        call(client, str(uuid4()), f"Bearer {CODE_A}"),
    ]

    for response in responses:
        assert CODE_A not in response.text.upper()
    assert CODE_A not in caplog.text.upper()
