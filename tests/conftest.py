import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import psycopg
import pytest
from migration_files import applied_version, newest_version
from psycopg.rows import DictRow, dict_row
from pydantic import TypeAdapter

from game_server.config import get_settings
from game_server.database import Database, close_databases, database_config, open_database
from game_server.proximity import hint_rate_limiter
from game_server.published_sessions import PublishedSessionRows
from game_server.referee import build_referee
from game_server.referee_references import build_reference_photos
from game_server.sessions import (
    GameSession,
    SessionRepository,
    parse_sessions,
    sessions_beside,
)
from game_server.submissions import SubmissionStore

# The suite needs a PostgreSQL database it may wipe: `make db-up` starts one in Docker.
TEST_DB_URL = os.environ.get(
    "GAME_SERVER_TEST_DB_URL", "postgresql://postgres:postgres@localhost:5432/game_server_test"
)
# The developer's own connection settings, which must not leak into the tests.
DB_SETTINGS = (
    "HOST",
    "PORT",
    "NAME",
    "USER",
    "PASSWORD",
    "SSLMODE",
    "SSLROOTCERT",
    "SSLCERT",
    "SSLKEY",
    "CONNECT_TIMEOUT_SECONDS",
    "POOL_MIN_SIZE",
    "POOL_MAX_SIZE",
    "POOL_TIMEOUT_SECONDS",
)


@pytest.fixture(scope="session")
def db() -> Iterator[psycopg.Connection[DictRow]]:
    """A connection to the test database, which Flyway has migrated to the newest version.

    The suite never creates the schema itself: `make test` (and `make check`) run
    `make db-migrate-test` first. Each test empties the tables, never `flyway_schema_history`.
    """
    try:
        conn = psycopg.connect(TEST_DB_URL, autocommit=True, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        pytest.exit(
            f"The tests need a PostgreSQL database at GAME_SERVER_TEST_DB_URL ({exc}). "
            "Start one with `make db-up`.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    with conn:
        _require_migrated(conn)
        yield conn


def _require_migrated(conn: psycopg.Connection[DictRow]) -> None:
    """Stop the run, with what to do, unless the test database is at the newest migration."""
    applied, newest = applied_version(conn), newest_version()
    if applied == newest:
        return
    if applied is None or applied < newest:
        what = f"at V{applied}" if applied is not None else "not migrated"
        advice = "Run `make db-migrate-test`."
    else:
        what = f"at V{applied}, ahead of the migrations here (another branch?)"
        advice = "Run `make db-reset` to rebuild the local databases."
    pytest.exit(
        f"The test database is {what}; the newest migration is V{newest}. {advice}",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )


@pytest.fixture(autouse=True)
def isolated_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    db: psycopg.Connection[DictRow],
) -> Iterator[None]:
    """Isolate every test from the developer's machine and from other tests.

    Settings read `.env` from the working directory, and a developer's `.env` may hold a
    real API key: run each test from its own temp dir so none can read it (and so none
    ever calls the real API). Only tests marked `live` keep the real environment.
    Every test starts with empty tables, and with process caches and pools reset.
    """
    if request.node.get_closest_marker("live") is None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GAME_SERVER_ANTHROPIC_API_KEY", raising=False)
    for name in DB_SETTINGS:
        monkeypatch.delenv(f"GAME_SERVER_DB_{name}", raising=False)
    monkeypatch.setenv("GAME_SERVER_DB_URL", TEST_DB_URL)
    monkeypatch.setenv("GAME_SERVER_DB_SSLMODE", "prefer")  # CI's server has no TLS
    monkeypatch.setenv("GAME_SERVER_IMAGE_BASE_PATH", str(tmp_path / "default-images"))
    db.execute(
        "TRUNCATE session_codes, published_sessions, hunt_drafts, rulings, referee_traces,"
        " referee_prompts, blocked_attempts, session_runs, arrivals, participants, submissions"
        " RESTART IDENTITY"
    )
    caches = (
        get_settings,
        hint_rate_limiter,
        build_referee,
        build_reference_photos,
        sessions_beside,
    )
    for cache in caches:
        cache.cache_clear()
    yield
    close_databases()
    for cache in caches:
        cache.cache_clear()


@pytest.fixture
def database() -> Database:
    """The test database's pool, as the app opens it."""
    return open_database(database_config(get_settings()))


@pytest.fixture
def store(database: Database) -> SubmissionStore:
    return SubmissionStore(database)


SessionSource = Literal["file", "published"]
_SESSIONS = TypeAdapter(list[GameSession])


@pytest.fixture(params=["file", "published"])
def session_source(request: pytest.FixtureRequest) -> SessionSource:
    """Where a test's sessions come from: the sessions file, or published to the database."""
    source: SessionSource = request.param
    return source


@pytest.fixture
def load_sessions(
    session_source: SessionSource, database: Database
) -> Callable[[str], SessionRepository]:
    """Turns sessions-file JSON into a repository, from the parametrised source.

    Published: the sessions are published into the database and served from there, by a
    repository with an empty file, so every route sees them exactly as the server would.
    """

    def load(raw: str) -> SessionRepository:
        if session_source == "file":
            return parse_sessions(raw)
        repository = SessionRepository().beside(PublishedSessionRows(database))
        for session in _SESSIONS.validate_json(raw):
            repository.publish_session(session)
        return SessionRepository().beside(PublishedSessionRows(database))  # cold: read back

    return load
