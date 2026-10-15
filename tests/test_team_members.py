"""Team membership by pid: each signed-in player's own row and consent, and the pid recorded."""

import json
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx2 import Response
from images import jpeg, scene
from psycopg.rows import DictRow
from test_auth import ISSUER, ORIGIN, claims, private_pem, public_pem
from test_join import FOX, HERON, OTHER_SESSION, SENTINEL_CODE, SESSION, sessions_text

from game_server.app import create_app
from game_server.checks import get_checks
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.sessions import SessionRepository, get_session_repository
from game_server.submissions import SubmissionStore

ADA = "5b1f5d3e-8f0e-4f64-9d57-2f6d4a3e7c10"
BOB = "8c2a6e4f-9a1f-4a75-8e68-3a7e5b4f8d21"
T0 = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
PHOTO = jpeg(scene(0, (16, 16)), quality=50)


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key: rsa.RSAPrivateKey, pid: str | None = ADA) -> str:
    return jwt.encode(claims(pid=pid), private_pem(key), algorithm="RS256")


@pytest.fixture
def now() -> list[datetime]:
    return [T0]


def make_client(
    settings: Settings,
    now: list[datetime],
    load_sessions: Callable[[str], SessionRepository],
) -> Iterator[TestClient]:
    app = create_app()
    sessions = load_sessions(sessions_text())
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    app.dependency_overrides[get_checks] = lambda: []
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def accounts(key: rsa.RSAPrivateKey) -> Settings:
    return Settings(
        auth_mode="optional",
        clerk_jwt_key=public_pem(key),
        clerk_issuer=ISSUER,
        clerk_authorized_parties=[ORIGIN],
    )


@pytest.fixture
def client(
    accounts: Settings, now: list[datetime], load_sessions: Callable[[str], SessionRepository]
) -> Iterator[TestClient]:
    yield from make_client(accounts, now, load_sessions)


def bearer(key: rsa.RSAPrivateKey, pid: str | None = ADA) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(key, pid)}"}


def join(client: TestClient, headers: dict[str, str] | None = None, code: str = FOX) -> Response:
    return client.post("/join", json={"code": code, "consent": True}, headers=headers)


def members(db: psycopg.Connection[DictRow]) -> list[DictRow]:
    return db.execute("SELECT * FROM team_members ORDER BY pid").fetchall()


# --- joining -------------------------------------------------------------------------------------


def test_a_signed_in_player_becomes_a_member_with_their_own_consent(
    client: TestClient, key: rsa.RSAPrivateKey, db: psycopg.Connection[DictRow]
) -> None:
    response = join(client, bearer(key))

    assert response.status_code == 201
    (member,) = members(db)
    assert member == {
        "session": UUID(SESSION),
        "participant": UUID(response.json()["participant"]),
        "pid": UUID(ADA),
        "joined_at": T0,
        "photo_consent_at": T0,
    }


def test_two_people_joining_with_one_code_share_a_participant_and_keep_their_own_consent(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    now: list[datetime],
    db: psycopg.Connection[DictRow],
) -> None:
    first = join(client, bearer(key, ADA))
    now[0] = T0 + timedelta(minutes=5)
    second = join(client, bearer(key, BOB))

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()
    ada, bob = members(db)
    assert {ada["participant"], bob["participant"]} == {UUID(first.json()["participant"])}
    assert (ada["pid"], ada["photo_consent_at"]) == (UUID(ADA), T0)
    assert (bob["pid"], bob["photo_consent_at"]) == (UUID(BOB), T0 + timedelta(minutes=5))
    assert len(db.execute("SELECT 1 FROM participants").fetchall()) == 1


def test_joining_again_renews_only_that_persons_consent(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    now: list[datetime],
    db: psycopg.Connection[DictRow],
) -> None:
    join(client, bearer(key, ADA))
    join(client, bearer(key, BOB))
    now[0] = T0 + timedelta(hours=1)

    assert join(client, bearer(key, ADA)).status_code == 200

    ada, bob = members(db)
    assert (ada["photo_consent_at"], ada["joined_at"]) == (T0 + timedelta(hours=1), T0)
    assert bob["photo_consent_at"] == T0


def test_nobody_is_in_two_teams_of_one_session(
    client: TestClient, key: rsa.RSAPrivateKey, db: psycopg.Connection[DictRow]
) -> None:
    join(client, bearer(key))

    response = join(client, bearer(key), code=HERON)

    assert response.status_code == 409
    assert response.json()["code"] == "already_in_another_team"
    assert len(members(db)) == 1
    assert len(db.execute("SELECT 1 FROM participants").fetchall()) == 1  # no Herons row made


def test_the_same_person_can_join_a_team_of_another_session(
    client: TestClient, key: rsa.RSAPrivateKey, db: psycopg.Connection[DictRow]
) -> None:
    join(client, bearer(key))

    assert join(client, bearer(key), code=SENTINEL_CODE).status_code == 201

    assert {str(m["session"]) for m in members(db)} == {SESSION, OTHER_SESSION}


def test_concurrent_joins_by_one_person_make_one_row(
    client: TestClient, key: rsa.RSAPrivateKey, db: psycopg.Connection[DictRow]
) -> None:
    headers = bearer(key)

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = [r.status_code for r in pool.map(lambda _: join(client, headers), range(12))]

    assert set(codes) <= {200, 201}
    assert len(members(db)) == 1


def test_a_token_without_a_pid_asks_for_provisioning(
    client: TestClient, key: rsa.RSAPrivateKey, db: psycopg.Connection[DictRow]
) -> None:
    response = join(client, bearer(key, pid=None))

    assert response.status_code == 401
    assert response.json()["code"] == "provisioning_required"
    assert members(db) == []


def test_a_bad_token_is_refused(client: TestClient, db: psycopg.Connection[DictRow]) -> None:
    response = join(client, {"Authorization": "Bearer not-a-token"})

    assert response.status_code == 401
    assert members(db) == []


# --- without a person ----------------------------------------------------------------------------


def test_without_a_token_joining_is_as_before(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    assert join(client).status_code == 201
    assert members(db) == []


@pytest.fixture
def off(
    now: list[datetime], load_sessions: Callable[[str], SessionRepository]
) -> Iterator[TestClient]:
    yield from make_client(Settings(), now, load_sessions)


def test_with_accounts_off_a_token_isnt_even_read(
    off: TestClient, key: rsa.RSAPrivateKey, db: psycopg.Connection[DictRow]
) -> None:
    assert join(off, bearer(key)).status_code == 201
    assert join(off, {"Authorization": "Bearer junk"}, code=HERON).status_code == 201
    assert members(db) == []


def test_the_join_response_is_the_same_with_or_without_a_person(
    client: TestClient, key: rsa.RSAPrivateKey
) -> None:
    anonymous = join(client)
    signed_in = join(client, bearer(key))

    assert anonymous.json() == signed_in.json()


# --- recording the pid ---------------------------------------------------------------------------


def test_arrivals_and_photos_record_the_callers_pid(
    client: TestClient,
    key: rsa.RSAPrivateKey,
    store: SubmissionStore,
    db: psycopg.Connection[DictRow],
) -> None:
    headers = bearer(key)
    participant = join(client, headers).json()["participant"]
    store.start_run(UUID(SESSION), T0)

    arrived = client.post(
        f"/sessions/{SESSION}/participants/{participant}/arrive",
        json={"checkpoint": 1},
        headers=headers,
    )
    metadata = {
        "session": SESSION,
        "participant": participant,
        "checkpoint": 1,
        "location": {"lat": 51.5001, "long": -0.1},
        "capture-time": T0.isoformat(),
    }
    submitted = client.post(
        "/challenge",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "challenge-image": ("photo.jpeg", PHOTO, "image/jpeg"),
        },
        headers=headers,
    )

    assert arrived.status_code == 201
    assert submitted.status_code in (200, 202)
    for table in ("arrivals", "submissions"):
        rows = db.execute(f"SELECT pid FROM {table}").fetchall()  # noqa: S608 - fixed names
        assert rows == [{"pid": UUID(ADA)}]


def test_without_a_person_the_pid_is_left_empty(
    client: TestClient, store: SubmissionStore, db: psycopg.Connection[DictRow]
) -> None:
    participant = join(client).json()["participant"]
    store.start_run(UUID(SESSION), T0)

    client.post(f"/sessions/{SESSION}/participants/{participant}/arrive", json={"checkpoint": 1})

    assert db.execute("SELECT pid FROM arrivals").fetchall() == [{"pid": None}]
