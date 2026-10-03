from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow
from psycopg_pool import PoolClosed, PoolTimeout

from game_server.config import Settings
from game_server.database import (
    Database,
    DatabaseConfig,
    close_databases,
    database_config,
    get_database,
    open_database,
)

URL = "postgresql://game:url-secret@db.example:6543/hunt?sslmode=disable"
OVERRIDE = "field-secret"


@pytest.fixture
def no_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the test database's settings, to see what the given settings alone produce."""
    monkeypatch.delenv("GAME_SERVER_DB_URL")
    monkeypatch.delenv("GAME_SERVER_DB_SSLMODE")


def params(settings: Settings) -> dict[str, object]:
    return dict(conninfo_to_dict(database_config(settings).conninfo))


# --- connection settings ------------------------------------------------------------


@pytest.mark.usefixtures("no_db_env")
def test_url_alone_gives_its_connection_details_with_tls_required() -> None:
    assert params(Settings(db_url=URL)) == {
        "user": "game",
        "password": "url-secret",
        "host": "db.example",
        "port": "6543",
        "dbname": "hunt",
        "sslmode": "require",  # the setting wins over the URL's: TLS stays on unless set off
        "connect_timeout": "10",
        "application_name": "game-server",
    }


@pytest.mark.usefixtures("no_db_env")
def test_separate_fields_override_the_url() -> None:
    settings = Settings(
        db_url=URL,
        db_host="db.internal",
        db_port=5433,
        db_name="game",
        db_user="server",
        db_password=OVERRIDE,
    )

    assert {key: params(settings)[key] for key in ("host", "port", "dbname", "user")} == {
        "host": "db.internal",
        "port": "5433",
        "dbname": "game",
        "user": "server",
    }
    assert params(settings)["password"] == OVERRIDE


@pytest.mark.usefixtures("no_db_env")
def test_fields_without_a_url() -> None:
    settings = Settings(db_host="db.internal", db_name="game", db_user="server")

    assert params(settings) == {
        "host": "db.internal",
        "dbname": "game",
        "user": "server",
        "sslmode": "require",
        "connect_timeout": "10",
        "application_name": "game-server",
    }


@pytest.mark.usefixtures("no_db_env")
def test_certificate_settings_are_passed_to_libpq() -> None:
    settings = Settings(
        db_host="db.example",
        db_sslmode="verify-full",
        db_sslrootcert="/certs/ca.pem",
        db_sslcert=Path("/certs/client.crt"),
        db_sslkey=Path("/certs/client.key"),
        db_connect_timeout_seconds=3,
    )

    assert {
        key: params(settings)[key]
        for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout")
    } == {
        "sslmode": "verify-full",
        "sslrootcert": "/certs/ca.pem",
        "sslcert": "/certs/client.crt",
        "sslkey": "/certs/client.key",
        "connect_timeout": "3",
    }


def test_pool_settings() -> None:
    config = database_config(
        Settings(db_pool_min_size=2, db_pool_max_size=5, db_pool_timeout_seconds=1.5)
    )

    assert (config.min_size, config.max_size, config.timeout) == (2, 5, 1.5)


def test_config_repr_hides_the_password() -> None:
    assert "url-secret" not in repr(database_config(Settings(db_url=URL)))


def test_malformed_url_is_rejected() -> None:
    with pytest.raises(psycopg.ProgrammingError):
        database_config(Settings(db_url="postgresql://db.example/hunt?bogus=1"))


# --- the pool --------------------------------------------------------------------


def test_borrowed_connection_is_in_autocommit_and_utc(database: Database) -> None:
    with database.connection() as conn:
        assert conn.autocommit
        assert conn.execute("SHOW TimeZone").fetchone() == ("UTC",)
        assert conn.execute("SELECT current_setting('application_name')").fetchone() == (
            "game-server",
        )


def test_transaction_commits_on_success(
    database: Database, db: psycopg.Connection[DictRow]
) -> None:
    try:
        with database.transaction() as conn:
            conn.execute("CREATE TABLE committed (n INTEGER)")
            assert conn.info.transaction_status == TransactionStatus.INTRANS

        assert db.execute("SELECT to_regclass('committed') IS NOT NULL AS found").fetchone() == {
            "found": True
        }
    finally:
        db.execute("DROP TABLE IF EXISTS committed")


def test_transaction_rolls_back_on_error(database: Database) -> None:
    with pytest.raises(RuntimeError), database.transaction() as conn:
        conn.execute("CREATE TABLE rolled_back (n INTEGER)")
        raise RuntimeError

    with database.connection() as conn:
        assert conn.execute("SELECT to_regclass('rolled_back')").fetchone() == (None,)


def test_connections_are_reused() -> None:
    database = open_database(database_config(Settings(db_pool_min_size=1, db_pool_max_size=1)))

    with database.connection() as conn:
        (first,) = conn.execute("SELECT pg_backend_pid()").fetchone() or (None,)
    with database.connection() as conn:
        (second,) = conn.execute("SELECT pg_backend_pid()").fetchone() or (None,)

    assert first == second


def test_borrowing_waits_at_most_the_timeout() -> None:
    config = database_config(Settings(db_pool_min_size=1, db_pool_max_size=1))
    database = open_database(config)

    with database.connection(), pytest.raises(PoolTimeout), database.connection(timeout=0.2):
        pass  # the only connection is lent out


def test_unreachable_database_does_not_raise_until_used() -> None:
    settings = Settings(db_host="127.0.0.1", db_port=1, db_sslmode="disable")

    database = open_database(database_config(settings))  # no exception: connects lazily

    with pytest.raises(PoolTimeout), database.connection(timeout=0.3):
        pass


def test_open_database_is_cached_per_config() -> None:
    config = database_config(Settings())

    assert open_database(config) is open_database(config)
    assert open_database(DatabaseConfig(config.conninfo, 0, 2, 1.0)) is not open_database(config)


def test_close_databases_closes_every_pool() -> None:
    database = open_database(database_config(Settings()))

    close_databases()

    with pytest.raises(PoolClosed), database.connection():
        pass
    assert open_database(database_config(Settings())) is not database  # a fresh pool


def test_dependency_opens_the_configured_database() -> None:
    settings = Settings()

    assert get_database(settings) is open_database(database_config(settings))
