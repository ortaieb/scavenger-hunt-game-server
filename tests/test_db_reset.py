from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from game_server.db_reset import main, reset_schema, schema_script
from game_server.submissions import SubmissionStore

TABLES = ["arrivals", "participants", "session_runs", "submissions"]


def tables(db: psycopg.Connection[DictRow]) -> list[str]:
    rows = db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"
    ).fetchall()
    return [row["tablename"] for row in rows]


def participant_count(db: psycopg.Connection[DictRow]) -> int:
    row = db.execute("SELECT COUNT(*) AS n FROM participants").fetchone()
    assert row is not None
    count: int = row["n"]
    return count


@pytest.fixture
def joined(store: SubmissionStore) -> Iterator[None]:
    store.join_team(UUID(int=1), "Red Foxes", datetime(2026, 10, 3, tzinfo=UTC))
    yield


@pytest.mark.usefixtures("joined")
def test_reset_deletes_the_data_and_recreates_the_tables(
    db: psycopg.Connection[DictRow],
) -> None:
    reset_schema(db)

    assert tables(db) == TABLES
    assert participant_count(db) == 0


def test_reset_creates_the_tables_when_none_exist(db: psycopg.Connection[DictRow]) -> None:
    db.execute("DROP TABLE arrivals, participants, submissions")

    reset_schema(db)

    assert tables(db) == TABLES


def test_reset_leaves_other_tables_alone(db: psycopg.Connection[DictRow]) -> None:
    db.execute("CREATE TABLE unrelated (n INTEGER)")
    try:
        reset_schema(db)

        assert "unrelated" in tables(db)
    finally:
        db.execute("DROP TABLE unrelated")


@pytest.mark.usefixtures("joined")
def test_failed_reset_changes_nothing(db: psycopg.Connection[DictRow]) -> None:
    broken = schema_script().replace(b"CREATE TABLE arrivals", b"CREATE TABLE arrivals arrivals")

    with pytest.raises(psycopg.errors.SyntaxError):
        db.execute(broken)
    db.execute("ROLLBACK")  # the script's own transaction, left aborted by the error

    assert tables(db) == TABLES
    assert participant_count(db) == 1  # the drop was rolled back too


def test_script_is_one_transaction() -> None:
    script = schema_script().decode()

    assert script.lstrip().startswith("--")
    assert "\nBEGIN;\n" in script
    assert script.rstrip().endswith("COMMIT;")


@pytest.mark.usefixtures("joined")
def test_cli_resets_the_configured_database(
    db: psycopg.Connection[DictRow], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--yes"]) == 0

    assert participant_count(db) == 0
    assert "Recreated the schema in database 'game_server_test'" in capsys.readouterr().out


@pytest.mark.usefixtures("joined")
def test_cli_refuses_without_confirmation(
    db: psycopg.Connection[DictRow], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 2

    assert participant_count(db) == 1
    assert "without --yes" in capsys.readouterr().err
