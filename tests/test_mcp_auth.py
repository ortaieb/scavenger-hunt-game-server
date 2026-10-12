"""The organiser key in front of `/mcp`: the guard on its own, with a fixed clock."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from game_server.config import Settings
from game_server.mcp_server.auth import FAILED_ATTEMPT_INTERVAL, OrganiserGuard
from game_server.organiser import UNAUTHORISED_BODY

KEY = "SENTINEL-ORGANISER-KEY-7c1e9a"
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


async def behind(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("through")(scope, receive, send)


@pytest.fixture
def now() -> list[datetime]:
    return [T0]


@pytest.fixture
def client(now: list[datetime]) -> TestClient:
    return TestClient(OrganiserGuard(behind, Settings(organiser_key=KEY), lambda: now[0]))


def test_the_key_passes(client: TestClient) -> None:
    response = client.post("/", headers={"Authorization": f"Bearer {KEY}"})

    assert (response.status_code, response.text) == (200, "through")


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-the-key-0000000000000"}, {"Authorization": f"Basic {KEY}"}],
    ids=["none", "wrong", "other-scheme"],
)
def test_anything_else_is_the_designers_401(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/", headers=headers)

    assert (response.status_code, response.json()) == (401, UNAUTHORISED_BODY)
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_no_key_configured_is_401() -> None:
    client = TestClient(OrganiserGuard(behind, Settings(), lambda: T0))

    response = client.post("/", headers={"Authorization": f"Bearer {KEY}"})

    assert (response.status_code, response.json()) == (401, UNAUTHORISED_BODY)


def test_after_a_failure_the_client_waits_even_with_the_key(
    client: TestClient, now: list[datetime]
) -> None:
    client.post("/")
    now[0] = T0 + timedelta(milliseconds=400)

    response = client.post("/", headers={"Authorization": f"Bearer {KEY}"})

    assert response.status_code == 429
    assert response.json()["code"] == "organiser_rate_limited"
    assert response.headers["Retry-After"] == "1"


def test_after_the_interval_the_key_passes_again(client: TestClient, now: list[datetime]) -> None:
    client.post("/")
    now[0] = T0 + FAILED_ATTEMPT_INTERVAL

    response = client.post("/", headers={"Authorization": f"Bearer {KEY}"})

    assert response.status_code == 200


def test_successes_are_never_limited(client: TestClient) -> None:
    for _ in range(3):
        assert client.post("/", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_clients_are_limited_apart(now: list[datetime]) -> None:
    guard = OrganiserGuard(behind, Settings(organiser_key=KEY), lambda: now[0])
    TestClient(guard, client=("10.0.0.1", 5000)).post("/")

    response = TestClient(guard, client=("10.0.0.2", 5000)).post(
        "/", headers={"Authorization": f"Bearer {KEY}"}
    )

    assert response.status_code == 200
