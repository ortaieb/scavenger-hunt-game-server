"""The users directory: its repository, and the database logins that keep it apart from the game."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID

import psycopg
import pytest
from migration_files import MIGRATIONS
from psycopg.rows import DictRow

from game_server.config import Settings, get_settings
from game_server.directory import DirectoryNotConfiguredError, DirectoryRepository, get_directory

DIRECTORY_TABLES = [
    "people",
    "profiles",
    "contact_consents",
    "role_grants",
    "role_changes",
    "clerk_outbox",
    "erasures",
    "suppressions",
]
NEW_PERSON = "INSERT INTO directory.people (pid, clerk_user_id) VALUES (gen_random_uuid(), 'x')"
GAME_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


@pytest.fixture
def directory() -> DirectoryRepository:
    return get_directory(get_settings())


# --- the repository ------------------------------------------------------------------------------


def test_a_new_clerk_user_gets_a_person_and_a_profile(directory: DirectoryRepository) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")

    profile = directory.profile(pid)

    assert profile is not None
    assert (profile.pid, profile.email, profile.display_name, profile.home_area) == (
        pid,
        "a@example.com",
        None,
        None,
    )
    assert profile.locale == "en-GB"
    assert directory.find_pid("user_a") == pid


def test_a_repeat_call_gives_the_same_pid_and_keeps_the_profile(
    directory: DirectoryRepository,
) -> None:
    first = directory.get_or_create_person("user_a", "a@example.com")

    again = directory.get_or_create_person("user_a", "changed@example.com")

    assert again == first
    profile = directory.profile(first)
    assert profile is not None
    assert profile.email == "a@example.com"


def test_a_repeat_call_marks_the_person_seen(
    directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")
    db.execute("UPDATE directory.people SET last_seen_at = '2020-01-01' WHERE pid = %s", (pid,))

    directory.get_or_create_person("user_a", "a@example.com")

    row = db.execute(
        "SELECT last_seen_at > '2021-01-01' AS seen FROM directory.people WHERE pid = %s", (pid,)
    ).fetchone()
    assert row is not None
    assert row["seen"]


def test_concurrent_calls_for_one_clerk_user_give_one_pid(
    directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        pids = list(
            pool.map(lambda _: directory.get_or_create_person("user_a", "a@example.com"), range(32))
        )

    assert len(set(pids)) == 1
    counts = db.execute(
        "SELECT (SELECT count(*) FROM directory.people) AS people,"
        " (SELECT count(*) FROM directory.profiles) AS profiles"
    ).fetchone()
    assert counts == {"people": 1, "profiles": 1}


def test_every_pid_is_a_version_4_uuid(directory: DirectoryRepository) -> None:
    pids = [directory.get_or_create_person(f"user_{n}", f"{n}@example.com") for n in range(20)]

    assert len(set(pids)) == 20
    assert {pid.version for pid in pids} == {4}


def test_an_unknown_clerk_user_has_no_pid_and_an_unknown_pid_no_profile(
    directory: DirectoryRepository,
) -> None:
    assert directory.find_pid("nobody") is None
    assert directory.profile(UUID(int=0)) is None


def test_display_names_returns_only_the_names_that_are_set(
    directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    named = directory.get_or_create_person("user_a", "a@example.com")
    unnamed = directory.get_or_create_person("user_b", "b@example.com")
    db.execute("UPDATE directory.profiles SET display_name = 'Ada' WHERE pid = %s", (named,))

    assert directory.display_names([named, unnamed, UUID(int=0)]) == {named: "Ada"}
    assert directory.display_names([]) == {}


def test_without_a_directory_url_there_is_no_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GAME_SERVER_DIRECTORY_DB_URL")

    with pytest.raises(DirectoryNotConfiguredError):
        get_directory(Settings())


# --- the schema ----------------------------------------------------------------------------------


def test_the_directory_has_no_foreign_key_to_or_from_the_game(
    db: psycopg.Connection[DictRow],
) -> None:
    rows = db.execute(
        "SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent"
        " FROM pg_constraint WHERE contype = 'f'"
        " AND (connamespace = 'directory'::regnamespace"
        "   OR conrelid::regclass::text LIKE 'directory.%'"
        "   OR confrelid::regclass::text LIKE 'directory.%')"
    ).fetchall()

    assert rows
    assert {row["parent"] for row in rows} == {"directory.people"}
    assert all(row["child"].startswith("directory.") for row in rows)
    assert "directory.erasures" not in {row["child"] for row in rows}  # it holds no identity


def test_erasing_a_person_removes_their_rows_but_not_the_erasure(
    directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")
    db.execute("INSERT INTO directory.role_grants (pid, role) VALUES (%s, 'organiser')", (pid,))
    db.execute("INSERT INTO directory.erasures (pid, source) VALUES (%s, 'self')", (pid,))

    db.execute("DELETE FROM directory.people WHERE pid = %s", (pid,))

    rows = db.execute(
        "SELECT (SELECT count(*) FROM directory.profiles) AS profiles,"
        " (SELECT count(*) FROM directory.role_grants) AS grants,"
        " (SELECT count(*) FROM directory.erasures) AS erasures"
    ).fetchone()
    assert rows == {"profiles": 0, "grants": 0, "erasures": 1}


@pytest.mark.parametrize("area", ["W4 1AB", "w4", "", "WESTBOURNE"])
def test_a_home_area_is_a_postcode_district_never_a_full_postcode(
    directory: DirectoryRepository, db: psycopg.Connection[DictRow], area: str
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE directory.profiles SET home_area = %s WHERE pid = %s", (area, pid))


@pytest.mark.parametrize("area", ["W4", "M1", "SW1A", "EC1A", "B33"])
def test_a_postcode_district_is_accepted(
    directory: DirectoryRepository, db: psycopg.Connection[DictRow], area: str
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")

    db.execute("UPDATE directory.profiles SET home_area = %s WHERE pid = %s", (area, pid))


def test_a_withdrawn_consent_keeps_its_history_and_a_new_grant_is_allowed(
    directory: DirectoryRepository, db: psycopg.Connection[DictRow]
) -> None:
    pid = directory.get_or_create_person("user_a", "a@example.com")
    grant = (
        "INSERT INTO directory.contact_consents (pid, purpose, wording_version, source)"
        " VALUES (%s, 'upcoming_hunts', 'v1', 'join')"
    )
    db.execute(grant, (pid,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(grant, (pid,))  # one in force at a time

    db.execute("UPDATE directory.contact_consents SET withdrawn_at = now() WHERE pid = %s", (pid,))
    db.execute(grant, (pid,))

    count = db.execute("SELECT count(*) AS n FROM directory.contact_consents").fetchone()
    assert count == {"n": 2}


# --- the two logins ------------------------------------------------------------------------------


@contextmanager
def acting_as(db: psycopg.Connection[DictRow], role: str) -> Iterator[None]:
    db.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
    try:
        yield
    finally:
        db.execute("RESET ROLE")


def game_tables(db: psycopg.Connection[DictRow]) -> list[str]:
    rows = db.execute(
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace"
        " AND relkind IN ('r', 'v', 'p') ORDER BY relname"
    ).fetchall()
    return [row["relname"] for row in rows]


@pytest.mark.parametrize("table", DIRECTORY_TABLES)
def test_game_app_cant_read_the_directory(db: psycopg.Connection[DictRow], table: str) -> None:
    with acting_as(db, "game_app"), pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(
            psycopg.sql.SQL("SELECT * FROM directory.{}").format(psycopg.sql.Identifier(table))
        )


def test_game_app_cant_write_the_directory(db: psycopg.Connection[DictRow]) -> None:
    with acting_as(db, "game_app"), pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(NEW_PERSON)


def test_game_app_has_what_the_game_needs_on_every_game_table(
    db: psycopg.Connection[DictRow],
) -> None:
    tables = game_tables(db)
    assert {"submissions", "participants", "session_runs", "ruled_submissions"} <= set(tables)

    for table in tables:
        for privilege in GAME_PRIVILEGES:
            row = db.execute(
                "SELECT has_table_privilege('game_app', %s, %s) AS has",
                (f"public.{table}", privilege),
            ).fetchone()
            expected = privilege == "SELECT" or table != "flyway_schema_history"
            assert row is not None
            assert row["has"] == expected, f"{privilege} on {table}"


def test_game_app_can_run_the_games_statements(db: psycopg.Connection[DictRow]) -> None:
    with acting_as(db, "game_app"):
        db.execute(
            "INSERT INTO session_runs (session, started_at) VALUES (gen_random_uuid(), now())"
        )
        db.execute(
            "INSERT INTO arrivals (session, participant, checkpoint, code, issued_at, expires_at)"
            " VALUES (gen_random_uuid(), gen_random_uuid(), 1, 'c', now(), now())"
        )  # an identity column
        db.execute("SELECT max(version::integer) FROM flyway_schema_history")
        db.execute("DELETE FROM arrivals")
        db.execute("DELETE FROM session_runs")


@pytest.mark.parametrize("table", DIRECTORY_TABLES)
def test_directory_app_has_everything_on_the_directory(
    db: psycopg.Connection[DictRow], table: str
) -> None:
    for privilege in (*GAME_PRIVILEGES, "TRUNCATE", "REFERENCES", "TRIGGER"):
        row = db.execute(
            "SELECT has_table_privilege('directory_app', %s, %s) AS has",
            (f"directory.{table}", privilege),
        ).fetchone()
        assert row is not None
        assert row["has"], f"{privilege} on {table}"


def test_directory_app_can_use_the_directory(db: psycopg.Connection[DictRow]) -> None:
    with acting_as(db, "directory_app"):
        db.execute(NEW_PERSON)
        db.execute(
            "INSERT INTO directory.clerk_outbox (pid, payload)"
            " SELECT pid, '{}' FROM directory.people"
        )  # an identity column
        db.execute("DELETE FROM directory.people")


def test_directory_app_has_nothing_on_the_game(db: psycopg.Connection[DictRow]) -> None:
    for table in game_tables(db):
        row = db.execute(
            "SELECT has_table_privilege('directory_app', %s, 'SELECT') AS has", (f"public.{table}",)
        ).fetchone()
        assert row is not None
        assert not row["has"], table
    with acting_as(db, "directory_app"), pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("SELECT * FROM submissions")


def test_tables_added_later_get_the_same_split(db: psycopg.Connection[DictRow]) -> None:
    with db.transaction(force_rollback=True):
        db.execute("CREATE TABLE public.later_game_table (id INTEGER)")
        db.execute("CREATE TABLE directory.later_directory_table (id INTEGER)")
        rows = db.execute(
            "SELECT has_table_privilege('game_app', 'public.later_game_table', 'INSERT') AS g_game,"
            " has_table_privilege('directory_app', 'public.later_game_table', 'SELECT') AS d_game,"
            " has_table_privilege('directory_app', 'directory.later_directory_table', 'DELETE')"
            "   AS d_dir,"
            " has_table_privilege('game_app', 'directory.later_directory_table', 'SELECT')"
            "   AS g_dir"
        ).fetchone()

    assert rows == {"g_game": True, "d_game": False, "d_dir": True, "g_dir": False}


def test_the_migration_sets_no_password_and_no_login() -> None:
    lines = (MIGRATIONS / "V2__users_directory.sql").read_text().upper().splitlines()
    sql = "\n".join(line for line in lines if not line.lstrip().startswith("--"))

    assert "PASSWORD" not in sql
    assert " LOGIN" not in sql.replace("NOLOGIN", "")
