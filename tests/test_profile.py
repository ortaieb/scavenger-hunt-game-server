"""The player's profile: `/me`, the upcoming-hunts consent, `/me/hunts` and a team's members."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from conftest import SignIn
from fastapi.testclient import TestClient
from psycopg.rows import DictRow
from test_join import FOX, HERON, OTHER_SESSION, SENTINEL_CODE, SESSION, sessions_text
from test_ruling import submit

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import database_config, open_database
from game_server.directory.consents import CURRENT_UPCOMING_HUNTS_WORDING
from game_server.directory.repository import DirectoryRepository
from game_server.sessions import SessionRepository, get_session_repository

T0 = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
ADA, BOB, CY = "user_ada", "user_bob", "user_cy"


@pytest.fixture
def now() -> list[datetime]:
    return [T0]


@pytest.fixture
def client(
    sign_in: SignIn, now: list[datetime], load_sessions: Callable[[str], SessionRepository]
) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(auth_mode="optional")  # after sign_in: it trusts the test key
    sessions = load_sessions(sessions_text())
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: now[0]
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def people(client: TestClient) -> DirectoryRepository:
    return DirectoryRepository(open_database(database_config(Settings())))


def pid_of(people: DirectoryRepository, user: str) -> UUID:
    found = people.find_pid(user)
    assert found is not None
    return found


def me(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.get("/me", headers=headers)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def consents(db: psycopg.Connection[DictRow]) -> list[DictRow]:
    return db.execute(
        "SELECT granted_at, withdrawn_at, wording_version, source"
        " FROM directory.contact_consents ORDER BY id"
    ).fetchall()


# --- GET /me -------------------------------------------------------------------------------------


def test_me_shows_the_profile_role_and_consent_but_never_the_email(
    client: TestClient, sign_in: SignIn, people: DirectoryRepository
) -> None:
    ada = sign_in("organiser", ADA)

    body = me(client, ada)

    assert body == {
        "pid": str(pid_of(people, ADA)),
        "displayName": None,
        "homeArea": None,
        "locale": "en-GB",
        "role": "organiser",
        "upcomingHunts": {"granted": False, "since": None, "wordingVersion": None},
    }
    assert "@" not in str(body)


@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_me_needs_a_token(client: TestClient, method: str) -> None:
    response = client.request(method, "/me", json={})

    assert response.status_code == 401


# --- PATCH /me -----------------------------------------------------------------------------------


def test_patch_changes_only_what_is_sent_and_trims_and_normalises(
    client: TestClient, sign_in: SignIn
) -> None:
    ada = sign_in("player", ADA)
    client.patch("/me", json={"displayName": "Ada", "homeArea": "sw1a"}, headers=ada)

    response = client.patch("/me", json={"displayName": "  Ada L  "}, headers=ada)

    assert response.status_code == 200
    body = response.json()
    assert (body["displayName"], body["homeArea"], body["locale"]) == ("Ada L", "SW1A", "en-GB")
    assert me(client, ada)["displayName"] == "Ada L"


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_clears_the_display_name_and_the_home_area(
    client: TestClient, sign_in: SignIn, empty: str | None
) -> None:
    ada = sign_in("player", ADA)
    client.patch("/me", json={"displayName": "Ada", "homeArea": "W4"}, headers=ada)

    response = client.patch("/me", json={"displayName": empty, "homeArea": empty}, headers=ada)

    assert response.status_code == 200
    assert (response.json()["displayName"], response.json()["homeArea"]) == (None, None)


@pytest.mark.parametrize("area", ["W4", "SW1A", "EC1A", "m1", "B33"])
def test_home_area_takes_a_postcode_district(
    client: TestClient, sign_in: SignIn, area: str
) -> None:
    response = client.patch("/me", json={"homeArea": area}, headers=sign_in("player", ADA))

    assert response.json()["homeArea"] == area.upper()


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"displayName": "x" * 41}, {"displayName": "invalid_display_name"}),
        ({"displayName": 7}, {"displayName": "invalid_display_name"}),
        ({"homeArea": "W4 1AA"}, {"homeArea": "invalid_home_area"}),
        ({"homeArea": "SW1A 1AA"}, {"homeArea": "invalid_home_area"}),
        ({"homeArea": "123"}, {"homeArea": "invalid_home_area"}),
        ({"homeArea": ["W4"]}, {"homeArea": "invalid_home_area"}),
        ({"locale": "fr-FR"}, {"locale": "unsupported_locale"}),
        ({"locale": None}, {"locale": "unsupported_locale"}),
        (
            {"displayName": "x" * 41, "homeArea": "nope", "locale": "de"},
            {
                "displayName": "invalid_display_name",
                "homeArea": "invalid_home_area",
                "locale": "unsupported_locale",
            },
        ),
    ],
)
def test_a_bad_field_is_422_with_a_code_per_field_and_nothing_changes(
    client: TestClient, sign_in: SignIn, body: dict[str, Any], fields: dict[str, str]
) -> None:
    ada = sign_in("player", ADA)
    client.patch("/me", json={"displayName": "Ada"}, headers=ada)

    response = client.patch("/me", json={"displayName": "Ignored", **body}, headers=ada)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "invalid profile",
        "code": "invalid_profile",
        "fields": fields,
    }
    assert me(client, ada)["displayName"] == "Ada"
    assert "xxxx" not in response.text


def test_patch_refuses_unknown_fields(client: TestClient, sign_in: SignIn) -> None:
    response = client.patch("/me", json={"email": "a@b.c"}, headers=sign_in("player", ADA))

    assert response.status_code == 422


# --- the upcoming-hunts consent ------------------------------------------------------------------


def grant(client: TestClient, headers: dict[str, str], version: str | None = None) -> Any:
    return client.put(
        "/me/consents/upcoming-hunts",
        json={"granted": True, "wordingVersion": version or CURRENT_UPCOMING_HUNTS_WORDING},
        headers=headers,
    )


def withdraw(client: TestClient, headers: dict[str, str]) -> Any:
    return client.put("/me/consents/upcoming-hunts", json={"granted": False}, headers=headers)


def test_granting_records_the_wording_and_when(
    client: TestClient, sign_in: SignIn, db: psycopg.Connection[DictRow]
) -> None:
    ada = sign_in("player", ADA)

    response = grant(client, ada)

    assert response.status_code == 200
    assert response.json() == {
        "granted": True,
        "since": "2026-10-03T09:30:00Z",
        "wordingVersion": CURRENT_UPCOMING_HUNTS_WORDING,
    }
    assert me(client, ada)["upcomingHunts"] == response.json()
    assert consents(db) == [
        {
            "granted_at": T0,
            "withdrawn_at": None,
            "wording_version": CURRENT_UPCOMING_HUNTS_WORDING,
            "source": "profile",
        }
    ]


def test_granting_withdrawing_and_granting_again_leaves_two_rows_the_first_withdrawn(
    client: TestClient, sign_in: SignIn, now: list[datetime], db: psycopg.Connection[DictRow]
) -> None:
    ada = sign_in("player", ADA)
    grant(client, ada)
    now[0] = T0 + timedelta(days=1)
    withdrawn = withdraw(client, ada)
    now[0] = T0 + timedelta(days=2)
    grant(client, ada)

    assert withdrawn.json()["granted"] is False
    first, second = consents(db)
    assert (first["granted_at"], first["withdrawn_at"]) == (T0, T0 + timedelta(days=1))
    assert (second["granted_at"], second["withdrawn_at"]) == (T0 + timedelta(days=2), None)
    assert me(client, ada)["upcomingHunts"]["granted"] is True


def test_a_withdrawal_says_since_when(
    client: TestClient, sign_in: SignIn, now: list[datetime]
) -> None:
    ada = sign_in("player", ADA)
    grant(client, ada)
    now[0] = T0 + timedelta(days=1)

    assert withdraw(client, ada).json()["since"] == "2026-10-04T09:30:00Z"


def test_repeating_a_grant_or_a_withdrawal_adds_nothing(
    client: TestClient, sign_in: SignIn, db: psycopg.Connection[DictRow]
) -> None:
    ada = sign_in("player", ADA)
    assert withdraw(client, ada).json()["granted"] is False
    assert consents(db) == []

    grant(client, ada)
    grant(client, ada)

    assert len(consents(db)) == 1


@pytest.mark.parametrize("version", [None, "", "1999-01-01"])
def test_a_grant_needs_a_known_wording_version(
    client: TestClient, sign_in: SignIn, db: psycopg.Connection[DictRow], version: str | None
) -> None:
    response = client.put(
        "/me/consents/upcoming-hunts",
        json={"granted": True, "wordingVersion": version},
        headers=sign_in("player", ADA),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "unknown_wording"
    assert consents(db) == []


def test_the_consent_needs_a_token(client: TestClient) -> None:
    assert client.put("/me/consents/upcoming-hunts", json={"granted": False}).status_code == 401


# --- joining with the box ticked -----------------------------------------------------------------


def join(client: TestClient, headers: dict[str, str] | None = None, **extra: Any) -> Any:
    return client.post("/join", json={"code": FOX, "consent": True, **extra}, headers=headers)


def test_a_ticked_box_at_join_is_recorded_with_source_join(
    client: TestClient, sign_in: SignIn, db: psycopg.Connection[DictRow]
) -> None:
    ada = sign_in("player", ADA)

    assert join(client, ada, upcomingHunts=True).status_code == 201

    assert consents(db) == [
        {
            "granted_at": T0,
            "withdrawn_at": None,
            "wording_version": CURRENT_UPCOMING_HUNTS_WORDING,
            "source": "join",
        }
    ]
    assert me(client, ada)["upcomingHunts"]["granted"] is True


@pytest.mark.parametrize("extra", [{}, {"upcomingHunts": False}])
def test_consent_is_never_assumed_at_join(
    client: TestClient, sign_in: SignIn, db: psycopg.Connection[DictRow], extra: dict[str, Any]
) -> None:
    assert join(client, sign_in("player", ADA), **extra).status_code == 201

    assert consents(db) == []


def test_joining_again_with_the_box_ticked_keeps_one_grant(
    client: TestClient, sign_in: SignIn, db: psycopg.Connection[DictRow]
) -> None:
    ada = sign_in("player", ADA)
    join(client, ada, upcomingHunts=True)

    assert join(client, ada, upcomingHunts=True).status_code == 200

    assert len(consents(db)) == 1


def test_a_ticked_box_without_a_token_records_nothing(
    client: TestClient, db: psycopg.Connection[DictRow]
) -> None:
    assert join(client, upcomingHunts=True).status_code == 201

    assert consents(db) == []


def test_the_box_must_be_a_boolean(client: TestClient, sign_in: SignIn) -> None:
    assert join(client, sign_in("player", ADA), upcomingHunts="yes").status_code == 422


# --- GET /me/hunts -------------------------------------------------------------------------------


def test_my_hunts_lists_the_teams_with_progress_and_points(
    client: TestClient, sign_in: SignIn, store: Any
) -> None:
    ada = sign_in("player", ADA)
    participant = join(client, ada).json()["participant"]
    herons = client.post("/join", json={"code": HERON, "consent": True}).json()["participant"]
    submit(store, participant, 1, "pass", T0, session=SESSION)
    submit(store, herons, 1, "pass", T0 + timedelta(minutes=1), session=SESSION)

    response = client.get("/me/hunts", headers=ada)

    assert response.status_code == 200
    [hunt] = response.json()["hunts"]
    assert hunt == {
        "session": SESSION,
        "participant": participant,
        "name": "Hyde Park Saturday Hunt",
        "team": "Red Foxes",
        "date": hunt["date"],
        "checkpoints": {"completed": 1, "total": 3},
        "points": 1 + 3 + 3,  # first at 1; unplaced (2 teams + 1) at the other two
    }


def test_my_hunts_lists_only_the_callers_hunts_newest_first(
    client: TestClient, sign_in: SignIn, now: list[datetime]
) -> None:
    ada, bob = sign_in("player", ADA), sign_in("player", BOB)
    join(client, ada)
    now[0] = T0 + timedelta(hours=1)
    client.post("/join", json={"code": SENTINEL_CODE, "consent": True}, headers=ada)

    hunts = client.get("/me/hunts", headers=ada).json()["hunts"]
    assert [h["team"] for h in hunts] == ["Sentinels", "Red Foxes"]
    assert client.get("/me/hunts", headers=bob).json() == {"hunts": []}


def test_my_hunts_needs_a_token(client: TestClient) -> None:
    assert client.get("/me/hunts").status_code == 401


# --- a team's members ----------------------------------------------------------------------------


def members_url(participant: str, session: str = SESSION) -> str:
    return f"/sessions/{session}/participants/{participant}/members"


def test_a_team_sees_its_members_names_and_never_emails_or_pids(
    client: TestClient, sign_in: SignIn, people: DirectoryRepository, now: list[datetime]
) -> None:
    ada, bob, cy = sign_in("player", ADA), sign_in("player", BOB), sign_in("player", CY)
    client.patch("/me", json={"displayName": "Ada"}, headers=ada)
    client.patch("/me", json={"displayName": "Bob"}, headers=bob)
    participant = join(client, ada).json()["participant"]
    now[0] += timedelta(minutes=1)
    join(client, bob)
    now[0] += timedelta(minutes=1)
    join(client, cy)  # no display name set

    response = client.get(members_url(participant), headers=bob)

    assert response.status_code == 200
    assert response.json() == {
        "members": [
            {"displayName": "Ada", "you": False},
            {"displayName": "Bob", "you": True},
            {"displayName": None, "you": False},
        ]
    }
    for secret in ("@", str(pid_of(people, ADA)), "user_"):
        assert secret not in response.text


def test_other_teams_and_outsiders_get_404(client: TestClient, sign_in: SignIn) -> None:
    ada, bob = sign_in("player", ADA), sign_in("player", BOB)
    participant = join(client, ada).json()["participant"]
    herons = client.post("/join", json={"code": HERON, "consent": True}, headers=bob).json()[
        "participant"
    ]

    assert client.get(members_url(participant), headers=bob).status_code == 404
    assert client.get(members_url(herons), headers=ada).status_code == 404
    assert client.get(members_url(participant, OTHER_SESSION), headers=ada).status_code == 404


def test_members_needs_a_token(client: TestClient, sign_in: SignIn) -> None:
    participant = join(client, sign_in("player", ADA)).json()["participant"]

    assert client.get(members_url(participant)).status_code == 401
