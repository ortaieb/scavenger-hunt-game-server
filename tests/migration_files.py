"""The Flyway migrations in db/migrations, and the version a database is at."""

import re
from pathlib import Path

import psycopg
from psycopg.rows import DictRow

DB_DIR = Path(__file__).parent.parent / "db"
MIGRATIONS = DB_DIR / "migrations"
# `V<n>__<what_it_does>.sql`: a positive integer version, lower-case snake case.
MIGRATION_NAME = re.compile(r"^V([1-9][0-9]*)__([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")


def migration_versions() -> list[int]:
    """Every migration's version, in file-name order; raises on a file that isn't one."""
    versions = []
    for path in sorted(MIGRATIONS.iterdir()):
        match = MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"not a migration file name: {path.name}")
        versions.append(int(match.group(1)))
    return versions


def newest_version() -> int:
    """The highest migration version in db/migrations."""
    return max(migration_versions())


def applied_version(conn: psycopg.Connection[DictRow]) -> int | None:
    """The highest version Flyway applied successfully, or None if it never ran here."""
    row = conn.execute("SELECT to_regclass('flyway_schema_history') IS NOT NULL AS ok").fetchone()
    if row is None or not row["ok"]:
        return None
    row = conn.execute(
        "SELECT max(version::integer) AS v FROM flyway_schema_history"
        " WHERE success AND version IS NOT NULL"
    ).fetchone()
    return None if row is None else row["v"]
