"""`POST /me/provision`: a signed-in Clerk user without a pid gets one, written to Clerk."""

import logging
from datetime import timedelta
from uuid import UUID

import jwt
import psycopg
import pytest
from clerk_fakes import FakeClerk, FakeClock
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import DictRow
from test_auth import ISSUER, ORIGIN, claims, private_pem, public_pem

from game_server import errors
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.directory import DirectoryRepository, get_directory
from game_server.directory.clerk import get_clerk_users
from game_server.directory.routes import router

URL = "/me/provision"
USER = "user_2abcDEF"  # test_auth's token subject


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(key: rsa.RSAPrivateKey) -> Settings:
    return Settings(
        auth_mode="required",
        clerk_jwt_key=public_pem(key),
        clerk_issuer=ISSUER,
        clerk_authorized_parties=[ORIGIN],
    )


@pytest.fixture
def clerk() -> FakeClerk:
    fake = FakeClerk()
    fake.emails[USER] = "a@example.com"
    return fake


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(settings: Settings, clerk: FakeClerk, clock: FakeClock) -> TestClient:
    app = FastAPI()
    errors.install(app)
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clerk_users] = lambda: clerk
    app.dependency_overrides[get_clock] = lambda: clock
    return TestClient(app)


@pytest.fixture
def directory() -> DirectoryRepository:
    return get_directory(get_settings())


@pytest.fixture
def headers(key: rsa.RSAPrivateKey) -> dict[str, str]:
    """A valid session token without a pid yet."""
    token = jwt.encode(claims(pid=None, role=None), private_pem(key), algorithm="RS256")
    return {"Authorization": f"Bearer {token}"}


def pending_writes(db: psycopg.Connection[DictRow]) -> int:
    row = db.execute(
        "SELECT count(*) AS n FROM directory.clerk_outbox WHERE done_at IS NULL"
    ).fetchone()
    assert row is not None
    return int(row["n"])


def test_a_new_person_is_created_and_written_to_clerk_at_once(
    client: TestClient,
    headers: dict[str, str],
    clerk: FakeClerk,
    directory: DirectoryRepository,
    db: psycopg.Connection[DictRow],
) -> None:
    response = client.post(URL, headers=headers)

    assert response.status_code == 200
    pid = UUID(response.json()["pid"])
    assert directory.find_pid(USER) == pid
    profile = directory.profile(pid)
    assert profile is not None
    assert profile.email == "a@example.com"
    assert clerk.writes == [(USER, pid, "player")]
    assert pending_writes(db) == 0


def test_a_known_person_keeps_their_pid_without_asking_clerk_for_the_email(
    client: TestClient, headers: dict[str, str], clerk: FakeClerk, directory: DirectoryRepository
) -> None:
    pid = directory.get_or_create_person(USER, "a@example.com")  # e.g. from user.created

    response = client.post(URL, headers=headers)

    assert response.json() == {"pid": str(pid)}
    assert clerk.email_reads == 0
    assert clerk.writes == [(USER, pid, "player")]


def test_the_role_written_is_the_directorys(
    client: TestClient,
    headers: dict[str, str],
    clerk: FakeClerk,
    directory: DirectoryRepository,
    db: psycopg.Connection[DictRow],
) -> None:
    pid = directory.get_or_create_person(USER, "a@example.com")
    db.execute("INSERT INTO directory.role_grants (pid, role) VALUES (%s, 'organiser')", (pid,))

    client.post(URL, headers=headers)

    assert clerk.writes == [(USER, pid, "organiser")]


def test_a_failed_write_falls_back_to_the_outbox(
    client: TestClient,
    headers: dict[str, str],
    clerk: FakeClerk,
    db: psycopg.Connection[DictRow],
    caplog: pytest.LogCaptureFixture,
) -> None:
    clerk.fail_writes = True

    with caplog.at_level(logging.INFO):
        response = client.post(URL, headers=headers)

    assert response.status_code == 200
    assert pending_writes(db) == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"Clerk write for person {response.json()['pid']} failed" in logged
    assert USER not in logged
    assert "a@example.com" not in logged


def test_a_new_person_needs_clerk_for_their_email(
    client: TestClient, headers: dict[str, str], clerk: FakeClerk, directory: DirectoryRepository
) -> None:
    clerk.fail_reads = True

    response = client.post(URL, headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "clerk_unavailable"
    assert directory.find_pid(USER) is None


def test_a_clerk_user_without_a_primary_email_is_refused(
    client: TestClient, headers: dict[str, str], clerk: FakeClerk, directory: DirectoryRepository
) -> None:
    clerk.emails.clear()

    response = client.post(URL, headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "no_primary_email"
    assert directory.find_pid(USER) is None


def test_a_person_being_erased_isnt_provisioned_again(
    client: TestClient, headers: dict[str, str], clerk: FakeClerk, directory: DirectoryRepository
) -> None:
    directory.mark_erasing(directory.get_or_create_person(USER, "a@example.com"))

    response = client.post(URL, headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "erasing"
    assert clerk.writes == []


def test_provisioning_needs_a_valid_token(client: TestClient, clerk: FakeClerk) -> None:
    response = client.post(URL)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorised"
    assert clerk.writes == []


def test_provisioning_is_rate_limited_per_clerk_user(
    client: TestClient, headers: dict[str, str], clock: FakeClock
) -> None:
    assert client.post(URL, headers=headers).status_code == 200

    again = client.post(URL, headers=headers)
    clock.now += timedelta(seconds=5)
    later = client.post(URL, headers=headers)

    assert again.status_code == 429
    assert again.headers["Retry-After"] == "5"
    assert again.json()["code"] == "too_many_requests"
    assert later.status_code == 200


def test_without_clerk_provisioning_is_unavailable(
    client: TestClient, headers: dict[str, str]
) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_clerk_users] = lambda: None

    response = client.post(URL, headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "clerk_unavailable"


def test_without_a_directory_provisioning_is_unavailable(
    client: TestClient, headers: dict[str, str], settings: Settings
) -> None:
    settings.directory_db_url = None

    response = client.post(URL, headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "directory_unavailable"


def test_the_event_and_provisioning_agree_on_one_pid(
    client: TestClient, headers: dict[str, str], directory: DirectoryRepository
) -> None:
    provisioned = UUID(client.post(URL, headers=headers).json()["pid"])

    assert directory.get_or_create_person(USER, "a@example.com") == provisioned  # user.created
