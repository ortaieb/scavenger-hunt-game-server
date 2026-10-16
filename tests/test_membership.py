"""Only a team's members can play as that team: the four play routes, in each auth mode."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx2 import Response
from test_auth import ISSUER, ORIGIN, public_pem
from test_join import HERON, SESSION
from test_team_members import ADA, BOB, PHOTO, bearer, join, make_client

from game_server.config import AuthMode, Settings
from game_server.sessions import SessionRepository
from game_server.submissions import SubmissionStore

T0 = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
UNKNOWN_TEAM = {"detail": "unknown participant"}


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def settings_for(mode: AuthMode, key: rsa.RSAPrivateKey) -> Settings:
    if mode == "off":
        return Settings()
    return Settings(
        auth_mode=mode,
        clerk_jwt_key=public_pem(key),
        clerk_issuer=ISSUER,
        clerk_authorized_parties=[ORIGIN],
    )


@pytest.fixture(params=["off", "optional", "required"])
def mode(request: pytest.FixtureRequest) -> AuthMode:
    mode: AuthMode = request.param
    return mode


@pytest.fixture
def now() -> list[datetime]:
    return [T0]


@pytest.fixture
def client(
    mode: AuthMode,
    key: rsa.RSAPrivateKey,
    now: list[datetime],
    load_sessions: Callable[[str], SessionRepository],
    store: SubmissionStore,
) -> Iterator[TestClient]:
    for test_client in make_client(settings_for(mode, key), now, load_sessions):
        store.start_run(UUID(SESSION), T0)
        yield test_client


@pytest.fixture
def team(client: TestClient, mode: AuthMode, key: rsa.RSAPrivateKey) -> str:
    """The Foxes' participant id, joined by Ada (anonymously with accounts off)."""
    return str(join(client, None if mode == "off" else bearer(key, ADA)).json()["participant"])


# --- the four play routes ------------------------------------------------------------------------

Play = Callable[[TestClient, str, dict[str, str] | None], Response]


def state(client: TestClient, participant: str, headers: dict[str, str] | None) -> Response:
    return client.get(f"/sessions/{SESSION}/participants/{participant}/state", headers=headers)


def arrive(client: TestClient, participant: str, headers: dict[str, str] | None) -> Response:
    url = f"/sessions/{SESSION}/participants/{participant}/arrive"
    return client.post(url, json={"checkpoint": 1}, headers=headers)


def challenge(client: TestClient, participant: str, headers: dict[str, str] | None) -> Response:
    metadata = {
        "session": SESSION,
        "participant": participant,
        "checkpoint": 1,
        "location": {"lat": 51.5001, "long": -0.1},
        "capture-time": T0.isoformat(),
    }
    return client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "challenge-image": ("photo.jpeg", PHOTO, "image/jpeg"),
        },
        headers=headers,
    )


def proximity(client: TestClient, participant: str, headers: dict[str, str] | None) -> Response:
    body: dict[str, Any] = {
        "session": SESSION,
        "participant": participant,
        "checkpoint": 1,
        "location": {"lat": 51.5001, "long": -0.1},
    }
    return client.post("/checkpoint/proximity", json=body, headers=headers)


ACCOUNTS_ON = pytest.mark.parametrize("mode", ["optional", "required"])

ROUTES = [state, arrive, challenge, proximity]


@pytest.fixture(params=ROUTES, ids=[route.__name__ for route in ROUTES])
def play(request: pytest.FixtureRequest) -> Play:
    route: Play = request.param
    return route


def played(response: Response) -> bool:
    """The request got past the membership check (whatever the game then said)."""
    return response.status_code not in (401, 404)


def test_a_member_plays_in_every_mode(
    client: TestClient, mode: AuthMode, key: rsa.RSAPrivateKey, team: str, play: Play
) -> None:
    headers = None if mode == "off" else bearer(key, ADA)

    assert played(play(client, team, headers))


def test_a_person_outside_the_team_is_a_404_when_a_token_is_sent(
    client: TestClient, mode: AuthMode, key: rsa.RSAPrivateKey, team: str, play: Play
) -> None:
    response = play(client, team, bearer(key, BOB))

    if mode == "off":
        assert played(response)  # the token isn't even read
    else:
        assert response.status_code == 404
        assert response.json() == UNKNOWN_TEAM


@ACCOUNTS_ON
def test_an_outsider_cant_tell_a_real_team_from_an_unknown_one(
    client: TestClient, mode: AuthMode, key: rsa.RSAPrivateKey, team: str, play: Play
) -> None:
    headers = bearer(key, BOB)

    real = play(client, team, headers)
    unknown = play(client, str(UUID(int=7)), headers)

    assert (real.status_code, real.json()) == (unknown.status_code, unknown.json())


def test_without_a_token_it_is_a_401_only_when_required(
    client: TestClient, mode: AuthMode, team: str, play: Play
) -> None:
    response = play(client, team, None)

    if mode == "required":
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorised"
    else:
        assert played(response)  # as today: the participant id alone is enough


def test_a_bad_token_is_a_401_when_accounts_are_on(
    client: TestClient, mode: AuthMode, team: str, play: Play
) -> None:
    response = play(client, team, {"Authorization": "Bearer not-a-token"})

    if mode == "off":
        assert played(response)
    else:
        assert response.status_code == 401


@ACCOUNTS_ON
def test_a_teammate_who_joined_later_plays_too(
    client: TestClient, mode: AuthMode, key: rsa.RSAPrivateKey, team: str, play: Play
) -> None:
    assert join(client, bearer(key, BOB)).json()["participant"] == team

    assert played(play(client, team, bearer(key, BOB)))


@ACCOUNTS_ON
def test_a_member_of_another_team_cant_play_as_this_one(
    client: TestClient, mode: AuthMode, key: rsa.RSAPrivateKey, team: str, play: Play
) -> None:
    join(client, bearer(key, BOB), code=HERON)

    response = play(client, team, bearer(key, BOB))

    assert response.status_code == 404


@ACCOUNTS_ON
def test_the_check_comes_before_the_photo_is_read(
    client: TestClient, mode: AuthMode, key: rsa.RSAPrivateKey, team: str
) -> None:
    metadata = json.dumps(
        {
            "session": SESSION,
            "participant": team,
            "checkpoint": 1,
            "location": {"lat": 51.5, "long": -0.1},
            "capture-time": T0.isoformat(),
        }
    )

    response = client.post(
        "/challenge",
        files={
            "metadata": (None, metadata, "application/json"),
            "challenge-image": ("photo.jpeg", b"not a jpeg", "image/png"),  # would be a 415
        },
        headers=bearer(key, BOB),
    )

    assert response.status_code == 404
