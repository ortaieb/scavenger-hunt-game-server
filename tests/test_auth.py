"""Checking Clerk session tokens: `verify_session_token`, `current_person`, `current_clerk_user`."""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, cast
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from game_server import errors
from game_server.auth import (
    UNAUTHORISED_BODY,
    Principal,
    current_clerk_user,
    current_person,
)
from game_server.config import Settings, get_settings

ISSUER = "https://example.clerk.accounts.dev"
ORIGIN = "https://play.example.com"
PID = "5b1f5d3e-8f0e-4f64-9d57-2f6d4a3e7c10"
CLERK_USER = "user_2abcDEF"
SESSION = "sess_2xyz"


def public_pem(key: rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def private_pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(private_key: rsa.RSAPrivateKey) -> Settings:
    return Settings(
        auth_mode="required",
        clerk_jwt_key=public_pem(private_key),
        clerk_issuer=ISSUER,
        clerk_authorized_parties=[ORIGIN],
    )


def claims(**overrides: Any) -> dict[str, Any]:
    """A valid token's claims, with `overrides` applied; a `None` value removes the claim."""
    now = int(time.time())
    base: dict[str, Any] = {
        "exp": now + 60,
        "nbf": now - 10,
        "iat": now - 10,
        "iss": ISSUER,
        "sub": CLERK_USER,
        "sid": SESSION,
        "azp": ORIGIN,
        "sts": "active",
        "pid": PID,
        "role": "player",
    }
    base.update(overrides)
    return {name: value for name, value in base.items() if value is not None}


@pytest.fixture
def mint(private_key: rsa.RSAPrivateKey) -> Any:
    def make(key: rsa.RSAPrivateKey | None = None, **overrides: Any) -> str:
        return jwt.encode(claims(**overrides), private_pem(key or private_key), algorithm="RS256")

    return make


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forged(settings: Settings, header: dict[str, str], secret: bytes | None) -> str:
    """A token PyJWT won't encode: HS256 keyed with the public key, or `alg: none`."""
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims()).encode())}"
    signature = b""
    if secret is not None:
        signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64(signature)}"


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = FastAPI()
    errors.install(app)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/person")
    def person(who: Principal = Depends(current_person)) -> dict[str, str]:  # noqa: B008
        return {"pid": str(who.pid), "role": who.role, "session": who.session_id}

    @app.get("/clerk")
    def clerk(sub: str = Depends(current_clerk_user)) -> dict[str, str]:
        return {"sub": sub}

    return TestClient(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- a valid token -------------------------------------------------------------------------------


def test_a_valid_token_gives_the_principal(client: TestClient, mint: Any) -> None:
    response = client.get("/person", headers=bearer(mint()))

    assert response.status_code == 200
    assert response.json() == {"pid": PID, "role": "player", "session": SESSION}


@pytest.mark.parametrize("role", ["player", "organiser", "admin"])
def test_a_known_role_is_kept(client: TestClient, mint: Any, role: str) -> None:
    response = client.get("/person", headers=bearer(mint(role=role)))

    assert response.json()["role"] == role


@pytest.mark.parametrize("role", ["", "superuser", "Admin", 7, None])
def test_an_empty_missing_or_unknown_role_is_player(
    client: TestClient, mint: Any, role: object
) -> None:
    response = client.get("/person", headers=bearer(mint(role=role)))

    assert response.json()["role"] == "player"


def test_the_scheme_is_case_insensitive(client: TestClient, mint: Any) -> None:
    response = client.get("/person", headers={"Authorization": f"bearer {mint()}"})

    assert response.status_code == 200


def test_a_token_within_the_leeway_is_accepted(client: TestClient, mint: Any) -> None:
    now = int(time.time())

    response = client.get("/person", headers=bearer(mint(exp=now - 2, nbf=now + 2)))

    assert response.status_code == 200


# --- every refusal is the same 401 ---------------------------------------------------------------


def refusals(settings: Settings, mint: Any, other_private_key: rsa.RSAPrivateKey) -> dict[str, str]:
    now = int(time.time())
    public_key = settings.clerk_jwt_key or ""
    return {
        "expired": mint(exp=now - 60),
        "not yet valid": mint(nbf=now + 60),
        "issued in the future": mint(iat=now + 60),
        "wrong issuer": mint(iss="https://evil.clerk.accounts.dev"),
        "azp not allowed": mint(azp="https://evil.example.com"),
        "bad signature": mint(key=other_private_key),
        "no sid": mint(sid=None),
        "no azp": mint(azp=None),
        "no exp": mint(exp=None),
        "no nbf": mint(nbf=None),
        "no iat": mint(iat=None),
        "no iss": mint(iss=None),
        "no sub": mint(sub=None),
        "pending": mint(sts="pending"),
        "HS256 keyed with the public key": forged(
            settings, {"alg": "HS256", "typ": "JWT"}, public_key.encode()
        ),
        "alg none": forged(settings, {"alg": "none", "typ": "JWT"}, None),
        "not a token": "not-a-token",
    }


REFUSAL_NAMES = list(
    refusals(Settings(), lambda **_: "", cast(rsa.RSAPrivateKey, None))
)  # names only


@pytest.mark.parametrize("path", ["/person", "/clerk"])
@pytest.mark.parametrize("case", REFUSAL_NAMES)
def test_a_bad_token_is_refused_with_the_one_body(
    client: TestClient,
    settings: Settings,
    mint: Any,
    other_private_key: rsa.RSAPrivateKey,
    case: str,
    path: str,
) -> None:
    token = refusals(settings, mint, other_private_key)[case]

    response = client.get(path, headers=bearer(token))

    assert response.status_code == 401
    assert response.json() == UNAUTHORISED_BODY
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ["/person", "/clerk"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer"}],
    ids=["no header", "another scheme", "no token"],
)
def test_a_missing_or_malformed_header_is_refused(
    client: TestClient, headers: dict[str, str], path: str
) -> None:
    response = client.get(path, headers=headers)

    assert response.status_code == 401
    assert response.json() == UNAUTHORISED_BODY


def test_without_a_configured_key_nothing_verifies(mint: Any) -> None:
    app = FastAPI()
    errors.install(app)

    @app.get("/person")
    def person(who: Principal = Depends(current_person)) -> str:  # noqa: B008
        return str(who.pid)

    response = TestClient(app).get("/person", headers=bearer(mint()))

    assert response.status_code == 401


# --- no pid --------------------------------------------------------------------------------------


@pytest.mark.parametrize("pid", [None, "", "not-a-uuid", 12])
def test_a_valid_token_without_a_usable_pid_needs_provisioning(
    client: TestClient, mint: Any, pid: object
) -> None:
    response = client.get("/person", headers=bearer(mint(pid=pid)))

    assert response.status_code == 401
    assert response.json()["code"] == "provisioning_required"


def test_the_clerk_user_needs_no_pid(client: TestClient, mint: Any) -> None:
    response = client.get("/clerk", headers=bearer(mint(pid=None)))

    assert response.status_code == 200
    assert response.json() == {"sub": CLERK_USER}


def test_the_principal_holds_a_uuid(client: TestClient, mint: Any) -> None:
    assert UUID(client.get("/person", headers=bearer(mint())).json()["pid"]) == UUID(PID)


# --- logs ----------------------------------------------------------------------------------------


def test_nothing_from_the_token_is_logged(
    client: TestClient, mint: Any, caplog: pytest.LogCaptureFixture
) -> None:
    good, bad = mint(), mint(sid=None)

    with caplog.at_level(logging.DEBUG):
        client.get("/person", headers=bearer(good))
        client.get("/person", headers=bearer(bad))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (good, bad, CLERK_USER, SESSION, PID):
        assert secret not in logged
