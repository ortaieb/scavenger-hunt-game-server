"""Roles from the directory: the role API, moderators by pid, and tokens alongside the codes."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import jwt
import psycopg
import pytest
from clerk_fakes import FakeClerk
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.rows import DictRow
from test_auth import ISSUER, ORIGIN, claims, private_pem, public_pem
from test_designer_review import AUTH as KEY_AUTH
from test_designer_review import HUNT, KEY, FixedCodes, ready_draft, review
from test_ruling import MODERATOR, SESSION, session_json, submit

from game_server.app import create_app
from game_server.auth import PERMISSIONS, can
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.designer.review import get_code_source
from game_server.directory import DirectoryRepository, get_directory
from game_server.directory.__main__ import grant_admin
from game_server.directory.clerk import get_clerk_users
from game_server.directory.outbox import ClerkOutbox, get_clerk_outbox
from game_server.directory.sync import send_due
from game_server.drafts import DraftRequest, DraftStore
from game_server.moderators import SessionModerators
from game_server.published_sessions import PublishedSessionRows
from game_server.sessions import SessionRepository, get_session_repository, parse_sessions
from game_server.submissions import SubmissionStore

NOW = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
CODE_AUTH = {"Authorization": f"Bearer {MODERATOR}"}


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(key: rsa.RSAPrivateKey) -> Settings:
    return Settings(
        auth_mode="optional",
        clerk_jwt_key=public_pem(key),
        clerk_issuer=ISSUER,
        clerk_authorized_parties=[ORIGIN],
        organiser_key=KEY,
    )


@pytest.fixture
def clerk() -> FakeClerk:
    return FakeClerk()


@pytest.fixture
def directory() -> DirectoryRepository:
    return get_directory(get_settings())


@pytest.fixture
def outbox() -> ClerkOutbox:
    return get_clerk_outbox(get_settings())


@pytest.fixture
def repository(database: Database) -> SessionRepository:
    sessions = parse_sessions(json.dumps([session_json(SESSION, MODERATOR)]))
    return sessions.beside(PublishedSessionRows(database))


@pytest.fixture
def client(
    settings: Settings, clerk: FakeClerk, repository: SessionRepository
) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clerk_users] = lambda: clerk
    app.dependency_overrides[get_session_repository] = lambda: repository
    app.dependency_overrides[get_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_code_source] = lambda: FixedCodes()
    with TestClient(app) as test_client:
        yield test_client


def person(directory: DirectoryRepository, name: str, role: str = "player") -> UUID:
    """A person in the directory with this role; their email is `<name>@example.com`."""
    pid = directory.get_or_create_person(f"user_{name}", f"{name}@example.com")
    directory.set_role(pid, role, None)
    return pid


def bearer(key: rsa.RSAPrivateKey, pid: UUID, role: str) -> dict[str, str]:
    """A session token for the person, carrying `role` (which may be out of date)."""
    token = jwt.encode(claims(pid=str(pid), role=role), private_pem(key), algorithm="RS256")
    return {"Authorization": f"Bearer {token}"}


def role_changes(db: psycopg.Connection[DictRow]) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT pid, from_role, to_role, changed_by FROM directory.role_changes ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


# --- the permission map --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("player", {"play"}),
        ("organiser", {"play", "design"}),
        ("admin", {"play", "design", "moderate_any_session", "manage_roles"}),
    ],
)
def test_the_permission_map(role: str, allowed: set[str]) -> None:
    assert {permission for permission in PERMISSIONS if can(role, permission)} == allowed


# --- the role API --------------------------------------------------------------------------------


def put_role(client: TestClient, pid: UUID, role: str, headers: dict[str, str]) -> Response:
    return client.put(f"/directory/people/{pid}/role", json={"role": role}, headers=headers)


def test_an_admin_grants_a_role_logged_with_who_did_it_and_written_to_clerk(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    clerk: FakeClerk,
    db: psycopg.Connection[DictRow],
) -> None:
    admin = person(directory, "ada", "admin")
    bob = person(directory, "bob")
    before = len(role_changes(db))

    response = put_role(client, bob, "organiser", bearer(key, admin, "admin"))

    assert response.status_code == 200
    assert response.json() == {"pid": str(bob), "role": "organiser", "changed": True}
    assert directory.role(bob) == "organiser"
    assert role_changes(db)[before:] == [
        {"pid": bob, "from_role": "player", "to_role": "organiser", "changed_by": admin}
    ]
    assert clerk.writes == [("user_bob", bob, "organiser")]
    assert clerk.revoked == []  # a promotion keeps the sessions


def test_setting_the_same_role_changes_and_logs_nothing(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    clerk: FakeClerk,
    db: psycopg.Connection[DictRow],
) -> None:
    admin = person(directory, "ada", "admin")
    bob = person(directory, "bob", "organiser")
    before = len(role_changes(db))

    response = put_role(client, bob, "organiser", bearer(key, admin, "admin"))

    assert response.json()["changed"] is False
    assert len(role_changes(db)) == before
    assert clerk.writes == []


def test_removing_a_role_deletes_the_grant_and_revokes_the_clerk_sessions(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    clerk: FakeClerk,
    db: psycopg.Connection[DictRow],
) -> None:
    admin = person(directory, "ada", "admin")
    bob = person(directory, "bob", "organiser")

    assert put_role(client, bob, "player", bearer(key, admin, "admin")).status_code == 200

    grants = db.execute("SELECT 1 FROM directory.role_grants WHERE pid = %s", (bob,)).fetchall()
    assert grants == []
    assert clerk.writes == [("user_bob", bob, "player")]
    assert clerk.revoked == ["user_bob"]


def test_when_clerk_fails_the_write_and_the_revocation_wait_in_the_outbox(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: FakeClerk,
) -> None:
    admin = person(directory, "ada", "admin")
    bob = person(directory, "bob", "admin")
    clerk.fail_writes = True

    assert put_role(client, bob, "organiser", bearer(key, admin, "admin")).status_code == 200
    assert outbox.pending(bob) == 2

    clerk.fail_writes = False
    assert send_due(outbox, clerk, lambda: NOW) == 2
    assert clerk.writes == [("user_bob", bob, "organiser")]
    assert clerk.revoked == ["user_bob"]


def test_an_unknown_person_is_404(
    client: TestClient, key: rsa.RSAPrivateKey, directory: DirectoryRepository
) -> None:
    admin = person(directory, "ada", "admin")

    response = put_role(client, UUID(int=9), "admin", bearer(key, admin, "admin"))

    assert response.status_code == 404
    assert response.json()["code"] == "unknown_person"


@pytest.mark.parametrize("role", ["player", "organiser"])
def test_only_an_admin_changes_roles(
    client: TestClient, key: rsa.RSAPrivateKey, directory: DirectoryRepository, role: str
) -> None:
    caller = person(directory, "ada", role)
    bob = person(directory, "bob")

    response = put_role(client, bob, "admin", bearer(key, caller, role))

    assert response.status_code == 403
    assert response.json() == {"detail": "not allowed", "code": "forbidden"}
    assert directory.role(bob) == "player"


def test_the_directory_not_the_token_decides_the_role(
    client: TestClient, key: rsa.RSAPrivateKey, directory: DirectoryRepository
) -> None:
    demoted = person(directory, "ada", "player")  # the token below still says admin

    response = put_role(client, person(directory, "bob"), "admin", bearer(key, demoted, "admin"))

    assert response.status_code == 403


def test_the_role_api_needs_a_token(client: TestClient, directory: DirectoryRepository) -> None:
    assert put_role(client, person(directory, "bob"), "admin", KEY_AUTH).status_code == 401
    assert client.get("/directory/people").status_code == 401


def test_admins_list_people_by_role(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    db: psycopg.Connection[DictRow],
) -> None:
    admin = person(directory, "ada", "admin")
    organiser = person(directory, "bob", "organiser")
    person(directory, "cy")
    db.execute("UPDATE directory.profiles SET display_name = 'Bob' WHERE pid = %s", (organiser,))
    headers = bearer(key, admin, "admin")

    default = client.get("/directory/people", headers=headers).json()
    organisers = client.get("/directory/people?role=organiser", headers=headers).json()

    assert default == {
        "people": [
            {"pid": str(admin), "display-name": None, "role": "admin"},
            {"pid": str(organiser), "display-name": "Bob", "role": "organiser"},
        ]
    }
    assert organisers == {
        "people": [{"pid": str(organiser), "display-name": "Bob", "role": "organiser"}]
    }


def test_listing_an_unknown_role_is_422(
    client: TestClient, key: rsa.RSAPrivateKey, directory: DirectoryRepository
) -> None:
    admin = person(directory, "ada", "admin")

    response = client.get("/directory/people?role=superuser", headers=bearer(key, admin, "admin"))

    assert response.status_code == 422


# --- moderating a session ------------------------------------------------------------------------

MODERATOR_ROUTES = [
    ("GET", f"/sessions/{SESSION}/overview"),
    ("GET", f"/sessions/{SESSION}/review"),
    ("GET", f"/sessions/{SESSION}/traces"),
    ("POST", f"/sessions/{SESSION}/start"),
    ("POST", f"/sessions/{SESSION}/submissions/1/ruling"),
]


def moderate(client: TestClient, route: tuple[str, str], headers: dict[str, str]) -> Response:
    method, url = route
    body = {"ruling": "approve"} if url.endswith("/ruling") else None
    return client.request(method, url, json=body, headers=headers)


def allowed(response: Response) -> bool:
    """Past the moderator check, whatever the route then said (e.g. no such submission)."""
    return response.status_code not in (401, 403)


@pytest.fixture(
    params=MODERATOR_ROUTES, ids=[f"{m} {u.rsplit('/', 1)[-1]}" for m, u in MODERATOR_ROUTES]
)
def route(request: pytest.FixtureRequest) -> tuple[str, str]:
    found: tuple[str, str] = request.param
    return found


def test_the_moderator_code_still_works(client: TestClient, route: tuple[str, str]) -> None:
    assert allowed(moderate(client, route, CODE_AUTH))


def test_an_admin_moderates_any_session(
    client: TestClient, key: rsa.RSAPrivateKey, directory: DirectoryRepository, route: Any
) -> None:
    admin = person(directory, "ada", "admin")

    assert allowed(moderate(client, route, bearer(key, admin, "admin")))


def test_an_organiser_moderates_the_sessions_they_were_added_to(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    database: Database,
    route: Any,
) -> None:
    organiser = person(directory, "bob", "organiser")
    headers = bearer(key, organiser, "organiser")

    assert moderate(client, route, headers).status_code == 403
    SessionModerators(database).add(UUID(SESSION), organiser, None, NOW)
    assert allowed(moderate(client, route, headers))


def test_a_player_never_moderates_even_with_a_moderator_row(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    database: Database,
    route: Any,
) -> None:
    player = person(directory, "cy")
    SessionModerators(database).add(UUID(SESSION), player, None, NOW)

    response = moderate(client, route, bearer(key, player, "player"))

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_bad_token_is_401_not_a_wrong_code(client: TestClient, route: Any) -> None:
    response = moderate(client, route, {"Authorization": "Bearer a.b.c"})

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorised"


def test_a_ruling_records_the_moderator_who_made_it(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    store: SubmissionStore,
    db: psycopg.Connection[DictRow],
) -> None:
    participant = str(store.join_team(UUID(SESSION), "Red Foxes", NOW).participant)
    by_token = submit(store, participant, 1, "pending")
    by_code = submit(store, participant, 2, "pending")
    admin = person(directory, "ada", "admin")
    url = f"/sessions/{SESSION}/submissions/{{}}/ruling"

    client.post(
        url.format(by_token), json={"ruling": "approve"}, headers=bearer(key, admin, "admin")
    )
    client.post(url.format(by_code), json={"ruling": "reject"}, headers=CODE_AUTH)

    rows = db.execute("SELECT submission_id, pid FROM rulings ORDER BY id").fetchall()
    assert [(row["submission_id"], row["pid"]) for row in rows] == [
        (by_token, admin),
        (by_code, None),
    ]


# --- demotion takes effect at once ---------------------------------------------------------------


def test_demoting_an_organiser_stops_their_access_at_once(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    database: Database,
    clerk: FakeClerk,
) -> None:
    admin = person(directory, "ada", "admin")
    organiser = person(directory, "bob", "organiser")
    SessionModerators(database).add(UUID(SESSION), organiser, None, NOW)
    old_token = bearer(key, organiser, "organiser")  # still valid, still says organiser
    overview = ("GET", f"/sessions/{SESSION}/overview")
    assert allowed(moderate(client, overview, old_token))
    assert client.get("/designer/drafts", headers=old_token).status_code == 200

    put_role(client, organiser, "player", bearer(key, admin, "admin"))

    assert moderate(client, overview, old_token).status_code == 403
    assert client.get("/designer/drafts", headers=old_token).status_code == 403
    assert clerk.revoked == ["user_bob"]


# --- the hunt designer ---------------------------------------------------------------------------


def test_the_organiser_key_still_works(client: TestClient) -> None:
    assert client.get("/designer/drafts", headers=KEY_AUTH).status_code == 200


@pytest.mark.parametrize(("role", "status"), [("organiser", 200), ("admin", 200), ("player", 403)])
def test_the_designer_takes_a_token_with_a_designing_role(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    role: str,
    status: int,
) -> None:
    pid = person(directory, "ada", role)

    assert client.get("/designer/drafts", headers=bearer(key, pid, role)).status_code == status


def test_publishing_as_an_organiser_makes_them_its_moderator(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    database: Database,
) -> None:
    organiser = person(directory, "bob", "organiser")
    draft = ready_draft(DraftStore(database))
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="rejected")
    headers = bearer(key, organiser, "organiser")

    response = client.post(f"/designer/drafts/{draft}/publish", json=HUNT, headers=headers)

    assert response.status_code == 201
    session = UUID(response.json()["session"])
    assert SessionModerators(database).is_moderator(session, organiser)
    assert allowed(moderate(client, ("GET", f"/sessions/{session}/overview"), headers))


def test_publishing_with_the_key_adds_no_moderator(
    client: TestClient, database: Database, db: psycopg.Connection[DictRow]
) -> None:
    draft = ready_draft(DraftStore(database))
    review(client, draft, p1="accepted", p2="accepted", p3="accepted", p4="rejected")

    assert client.post(f"/designer/drafts/{draft}/publish", json=HUNT, headers=KEY_AUTH).is_success
    assert db.execute("SELECT 1 FROM session_moderators").fetchall() == []


def test_a_draft_records_the_organiser_who_started_it(
    database: Database, directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    organiser = person(directory, "bob", "organiser")
    request = DraftRequest.model_validate({"area": "Chiswick", "theme": "Brewing"})
    DraftStore(database).create(UUID(int=5), request, "stub", NOW, organiser)

    assert db.execute("SELECT pid FROM hunt_drafts").fetchall() == [{"pid": organiser}]


# --- adding and removing moderators --------------------------------------------------------------


def moderators_url(session: str = SESSION) -> str:
    return f"/sessions/{session}/moderators"


def test_an_admin_adds_an_organiser_as_moderator_by_email(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    db: psycopg.Connection[DictRow],
) -> None:
    admin = person(directory, "ada", "admin")
    bob = person(directory, "bob", "organiser")
    headers = bearer(key, admin, "admin")

    first = client.post(moderators_url(), json={"email": " BOB@example.com "}, headers=headers)
    again = client.post(moderators_url(), json={"email": "bob@example.com"}, headers=headers)

    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json() == {"session": SESSION, "pid": str(bob)}
    rows = db.execute("SELECT pid, added_by FROM session_moderators").fetchall()
    assert rows == [{"pid": bob, "added_by": admin}]


def test_a_player_cant_be_added_as_moderator(
    client: TestClient, key: rsa.RSAPrivateKey, directory: DirectoryRepository
) -> None:
    admin = person(directory, "ada", "admin")
    person(directory, "cy")

    response = client.post(
        moderators_url(), json={"email": "cy@example.com"}, headers=bearer(key, admin, "admin")
    )

    assert response.status_code == 409
    assert response.json()["code"] == "not_an_organiser"


def test_an_unknown_email_is_404(
    client: TestClient, key: rsa.RSAPrivateKey, directory: DirectoryRepository
) -> None:
    admin = person(directory, "ada", "admin")

    response = client.post(
        moderators_url(), json={"email": "nobody@example.com"}, headers=bearer(key, admin, "admin")
    )

    assert response.status_code == 404
    assert response.json()["code"] == "no_account"


def test_an_organiser_manages_only_the_sessions_they_moderate(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    directory: DirectoryRepository,
    database: Database,
) -> None:
    bob = person(directory, "bob", "organiser")
    person(directory, "dee", "organiser")
    headers = bearer(key, bob, "organiser")
    body = {"email": "dee@example.com"}

    assert client.post(moderators_url(), json=body, headers=headers).status_code == 403
    SessionModerators(database).add(UUID(SESSION), bob, None, NOW)
    assert client.post(moderators_url(), json=body, headers=headers).status_code == 201


def test_the_organiser_key_manages_moderators_too(
    client: TestClient, directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    bob = person(directory, "bob", "organiser")

    response = client.post(moderators_url(), json={"email": "bob@example.com"}, headers=KEY_AUTH)

    assert response.status_code == 201
    assert db.execute("SELECT added_by FROM session_moderators").fetchall() == [{"added_by": None}]
    removed = client.delete(f"{moderators_url()}/{bob}", headers=KEY_AUTH)
    assert removed.status_code == 204
    assert client.delete(f"{moderators_url()}/{bob}", headers=KEY_AUTH).status_code == 404


def test_managing_moderators_needs_a_credential_and_a_known_session(
    client: TestClient,
) -> None:
    body = {"email": "bob@example.com"}

    assert client.post(moderators_url(), json=body).status_code == 401
    assert client.post(moderators_url(), json=body, headers=CODE_AUTH).status_code == 401
    unknown = moderators_url("00000000-0000-4000-8000-000000000000")
    assert client.post(unknown, json=body, headers=KEY_AUTH).status_code == 404


# --- the first admin -----------------------------------------------------------------------------


def test_grant_admin_makes_a_known_person_an_admin(
    directory: DirectoryRepository,
    db: psycopg.Connection[DictRow],
    capsys: pytest.CaptureFixture[str],
) -> None:
    pid = person(directory, "ada")

    assert grant_admin(get_settings(), "user_ada") == 0

    assert directory.role(pid) == "admin"
    assert role_changes(db)[-1] == {
        "pid": pid,
        "from_role": "player",
        "to_role": "admin",
        "changed_by": None,
    }
    out = capsys.readouterr().out
    assert out == f"Person {pid} is an admin\n"
    assert "user_ada" not in out


def test_grant_admin_needs_someone_who_signed_in(directory: DirectoryRepository) -> None:
    assert grant_admin(get_settings(), "user_nobody") == 1


def test_grant_admin_needs_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GAME_SERVER_DIRECTORY_DB_URL")

    assert grant_admin(Settings(), "user_ada") == 2
