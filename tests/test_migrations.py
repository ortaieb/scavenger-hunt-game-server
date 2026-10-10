"""The Flyway migrations, their settings, and the database they build."""

import re
import tomllib
from pathlib import Path

import psycopg
from migration_files import (
    DB_DIR,
    MIGRATION_NAME,
    MIGRATIONS,
    applied_version,
    migration_versions,
    newest_version,
)
from psycopg.rows import DictRow

TABLES = [
    "arrivals",
    "blocked_attempts",
    "flyway_schema_history",
    "hunt_drafts",
    "participants",
    "published_sessions",
    "referee_prompts",
    "referee_traces",
    "rulings",
    "session_codes",
    "session_runs",
    "submissions",
]
MAKEFILE = Path(__file__).parent.parent / "Makefile"


def statements(path: Path) -> list[str]:
    """The file's SQL lines, without comments or blank lines."""
    lines = (line.split("--", 1)[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


# --- the database they build ---------------------------------------------------------------


def test_the_migrated_database_has_exactly_the_tables(db: psycopg.Connection[DictRow]) -> None:
    rows = db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"
    ).fetchall()

    assert [row["tablename"] for row in rows] == TABLES


def test_the_ruled_submissions_view_exists(db: psycopg.Connection[DictRow]) -> None:
    row = db.execute("SELECT to_regclass('ruled_submissions') IS NOT NULL AS ok").fetchone()

    assert row is not None and row["ok"]


def test_every_migration_is_applied_and_none_failed(db: psycopg.Connection[DictRow]) -> None:
    rows = db.execute(
        "SELECT version::integer AS v, success FROM flyway_schema_history"
        " WHERE version IS NOT NULL ORDER BY installed_rank"
    ).fetchall()

    assert [row["v"] for row in rows] == migration_versions()
    assert all(row["success"] for row in rows)
    assert applied_version(db) == newest_version()


def test_the_history_survives_each_test(db: psycopg.Connection[DictRow]) -> None:
    # Every test starts by emptying the tables: never Flyway's history.
    row = db.execute("SELECT COUNT(*) AS n FROM flyway_schema_history").fetchone()

    assert row is not None and row["n"] >= 1


# --- the files -----------------------------------------------------------------------------


def test_migration_names_are_valid() -> None:
    names = sorted(path.name for path in MIGRATIONS.iterdir())

    assert names
    assert [name for name in names if not MIGRATION_NAME.match(name)] == []


def test_versions_are_unique_and_contiguous_from_one() -> None:
    versions = sorted(migration_versions())

    assert versions == list(range(1, len(versions) + 1))


def test_the_baseline_only_creates() -> None:
    sql = " ".join(statements(MIGRATIONS / "V1__baseline.sql")).upper()

    for destructive in (
        r"\bDROP\b",
        r"\bTRUNCATE\b",
        r"\bDELETE\s+FROM\b",
        r"\bBEGIN\s*;",
        r"\bCOMMIT\s*;",
    ):
        assert not re.search(destructive, sql)
    assert sql.count("CREATE TABLE ") == len(TABLES) - 1  # all but Flyway's history


# --- the settings and the one way to run Flyway ------------------------------------------------


def test_flyway_settings_are_safe() -> None:
    with (DB_DIR / "flyway.toml").open("rb") as file:
        flyway = tomllib.load(file)["flyway"]

    assert flyway == {
        "locations": ["filesystem:/flyway/project/migrations"],
        "validateMigrationNaming": True,
        "cleanDisabled": True,
        "baselineOnMigrate": False,
        "placeholderReplacement": False,
        "outOfOrder": False,
    }


def test_flyway_settings_hold_no_credentials() -> None:
    text = (DB_DIR / "flyway.toml").read_text().lower()

    for key in ("url", "user", "password"):
        assert not re.search(rf"^\s*{key}\s*=", text, re.MULTILINE)


def test_the_flyway_image_is_pinned_exactly() -> None:
    [image] = re.findall(r"^FLYWAY_IMAGE\s*\?=\s*(\S+)$", MAKEFILE.read_text(), re.MULTILINE)

    assert re.fullmatch(r"flyway/flyway:\d+\.\d+\.\d+", image)


def test_only_the_local_reset_may_clean() -> None:
    lines = [line for line in MAKEFILE.read_text().splitlines() if "cleanDisabled=false" in line]

    assert len(lines) == 1
    reset = MAKEFILE.read_text().split("DB_RESET_FLYWAY =", 1)[1].split("\n\n", 1)[0]
    assert "--network container:$(DB_CONTAINER)" in reset
    assert "-e FLYWAY_USER=postgres -e FLYWAY_PASSWORD=postgres" in reset
    for line in MAKEFILE.read_text().split("db-reset:", 1)[1].split("\n\n", 1)[0].splitlines()[1:]:
        assert "-url=jdbc:postgresql://localhost:5432/game_server" in line
