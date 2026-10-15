"""`POST /internal/clerk-events`: Clerk's signed user events applied to the directory."""

import json
import logging

import psycopg
import pytest
from clerk_fakes import OTHER_SECRET, WEBHOOK_SECRET, FakeClock, clerk_user, signed
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import DictRow

from game_server import errors
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.directory import DirectoryNotConfiguredError, DirectoryRepository, get_directory
from game_server.directory.outbox import get_clerk_outbox
from game_server.directory.routes import router

URL = "/internal/clerk-events"


@pytest.fixture
def settings() -> Settings:
    return Settings(clerk_webhook_secret=WEBHOOK_SECRET)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = FastAPI()
    errors.install(app)
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clock] = lambda: FakeClock()
    return TestClient(app)


@pytest.fixture
def directory() -> DirectoryRepository:
    return get_directory(get_settings())


def send(client: TestClient, event: object, **signing: str) -> int:
    body, headers = signed(event, **signing)
    return client.post(URL, content=body, headers=headers).status_code


def counts(db: psycopg.Connection[DictRow]) -> dict[str, int]:
    row = db.execute(
        "SELECT (SELECT count(*) FROM directory.people) AS people,"
        " (SELECT count(*) FROM directory.clerk_outbox WHERE done_at IS NULL) AS writes"
    ).fetchone()
    assert row is not None
    return dict(row)


# --- user.created --------------------------------------------------------------------------------


def test_a_created_user_becomes_a_person_with_a_write_queued(
    client: TestClient, directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    assert send(client, {"type": "user.created", "data": clerk_user()}) == 204

    pid = directory.find_pid("user_a")
    assert pid is not None
    profile = directory.profile(pid)
    assert profile is not None
    assert profile.email == "a@example.com"
    assert counts(db) == {"people": 1, "writes": 1}


def test_the_same_created_event_twice_gives_one_person(
    client: TestClient, directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    event = {"type": "user.created", "data": clerk_user()}

    assert send(client, event) == 204
    first = directory.find_pid("user_a")
    assert send(client, event) == 204

    assert directory.find_pid("user_a") == first
    assert counts(db)["people"] == 1


def test_a_created_user_without_a_primary_email_is_ignored(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    assert send(client, {"type": "user.created", "data": clerk_user(email=None)}) == 204

    assert counts(db) == {"people": 0, "writes": 0}


def test_a_created_event_for_an_erasing_person_changes_nothing(
    client: TestClient, directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")
    directory.mark_erasing(pid)

    assert send(client, {"type": "user.created", "data": clerk_user()}) == 204

    person = directory.find_person("user_a")
    assert person is not None
    assert person.status == "erasing"
    assert counts(db)["writes"] == 0


# --- signatures ----------------------------------------------------------------------------------


def test_an_unsigned_event_is_refused_and_changes_nothing(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    response = client.post(URL, json={"type": "user.created", "data": clerk_user()})

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid signature", "code": "invalid_signature"}
    assert counts(db) == {"people": 0, "writes": 0}


def test_an_event_signed_with_another_secret_is_refused(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    event = {"type": "user.created", "data": clerk_user()}

    assert send(client, event, secret=OTHER_SECRET) == 400
    assert counts(db) == {"people": 0, "writes": 0}


def test_a_tampered_event_is_refused(client: TestClient, db: psycopg.Connection[DictRow]) -> None:
    _, headers = signed({"type": "user.created", "data": clerk_user()})
    forged = json.dumps({"type": "user.created", "data": clerk_user("user_evil")})

    assert client.post(URL, content=forged, headers=headers).status_code == 400
    assert counts(db) == {"people": 0, "writes": 0}


def test_without_a_webhook_secret_events_are_refused_for_a_retry(
    settings: Settings, client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    settings.clerk_webhook_secret = None

    body, headers = signed({"type": "user.created", "data": clerk_user()})
    response = client.post(URL, content=body, headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "clerk_events_unavailable"
    assert counts(db)["people"] == 0


def test_without_a_directory_events_are_refused_for_a_retry(
    settings: Settings, client: TestClient
) -> None:
    settings.directory_db_url = None

    body, headers = signed({"type": "user.created", "data": clerk_user()})
    response = client.post(URL, content=body, headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "directory_unavailable"


# --- user.updated --------------------------------------------------------------------------------


def test_an_updated_primary_email_reaches_the_profile(
    client: TestClient, directory: DirectoryRepository
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")

    event = {"type": "user.updated", "data": clerk_user(email="new@example.com")}
    assert send(client, event) == 204

    profile = directory.profile(pid)
    assert profile is not None
    assert profile.email == "new@example.com"


@pytest.mark.parametrize("erasing", [False, True], ids=["unknown", "erasing"])
def test_an_update_for_an_unknown_or_erasing_person_is_ignored(
    client: TestClient, directory: DirectoryRepository, erasing: bool
) -> None:
    if erasing:
        directory.mark_erasing(directory.get_or_create_person("user_a", "a@example.com"))

    event = {"type": "user.updated", "data": clerk_user(email="new@example.com")}
    assert send(client, event) == 204

    pid = directory.find_pid("user_a")
    profile = None if pid is None else directory.profile(pid)
    assert profile is None or profile.email == "a@example.com"


# --- user.deleted --------------------------------------------------------------------------------


def test_a_deleted_user_is_marked_erasing_and_logged_by_pid(
    client: TestClient, directory: DirectoryRepository, caplog: pytest.LogCaptureFixture
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")

    with caplog.at_level(logging.INFO):
        for _ in range(2):
            deleted = {"type": "user.deleted", "data": {"id": "user_a", "deleted": True}}
            assert send(client, deleted) == 204

    person = directory.find_person("user_a")
    assert person is not None
    assert person.status == "erasing"
    logged = [r.getMessage() for r in caplog.records if "erasing" in r.getMessage()]
    assert logged == [f"Person {pid} deleted in Clerk: marked erasing"]


def test_a_deleted_unknown_user_changes_nothing(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    assert send(client, {"type": "user.deleted", "data": {"id": "user_a", "deleted": True}}) == 204

    assert counts(db)["people"] == 0


# --- anything else -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {"type": "session.created", "data": {"id": "sess_1"}},
        {"type": "user.created", "data": {"no": "id"}},
        {"type": "user.created"},
        ["not", "an", "object"],
    ],
    ids=["another type", "no id", "no data", "not an object"],
)
def test_other_and_malformed_events_are_acknowledged_and_ignored(
    client: TestClient, db: psycopg.Connection[DictRow], event: object
) -> None:
    assert send(client, event) == 204

    assert counts(db) == {"people": 0, "writes": 0}


def test_no_clerk_id_or_email_is_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        send(client, {"type": "user.created", "data": clerk_user()})
        send(client, {"type": "user.updated", "data": clerk_user(email="new@example.com")})
        send(client, {"type": "user.deleted", "data": {"id": "user_a", "deleted": True}})

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("user_a", "a@example.com", "new@example.com", "Ada"):
        assert secret not in logged


def test_the_outbox_dependency_needs_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GAME_SERVER_DIRECTORY_DB_URL")

    with pytest.raises(DirectoryNotConfiguredError):
        get_clerk_outbox(Settings())
