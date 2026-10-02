"""The external PostgreSQL database: a pool of connections the app borrows from."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from game_server.config import Settings, get_settings

APPLICATION_NAME = "game-server"


@dataclass(frozen=True)
class DatabaseConfig:
    """How to connect, and how many connections to pool. Hashable: one pool per config."""

    conninfo: str = field(repr=False)  # holds the password
    min_size: int
    max_size: int
    timeout: float


def database_config(settings: Settings) -> DatabaseConfig:
    """Build the connection string from the URL and the separate settings, which win.

    Raises `psycopg.ProgrammingError` if the URL is malformed.
    """
    conninfo = make_conninfo(
        settings.db_url.get_secret_value() if settings.db_url else "",
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password.get_secret_value() if settings.db_password else None,
        sslmode=settings.db_sslmode,
        sslrootcert=settings.db_sslrootcert,
        sslcert=str(settings.db_sslcert) if settings.db_sslcert else None,
        sslkey=str(settings.db_sslkey) if settings.db_sslkey else None,
        connect_timeout=settings.db_connect_timeout_seconds,
        application_name=APPLICATION_NAME,
    )
    return DatabaseConfig(
        conninfo=conninfo,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_pool_timeout_seconds,
    )


def _configure(conn: Connection[TupleRow]) -> None:
    """Run on each new connection: timestamps come back in UTC whatever the server's zone."""
    conn.execute("SET TIME ZONE 'UTC'")


class Database:
    """A connection pool. Connections are in autocommit mode: open transactions explicitly.

    Each connection is checked before it's lent out, so one the server or a proxy closed
    while idle is replaced rather than failing the request.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self._pool = ConnectionPool(
            config.conninfo,
            min_size=config.min_size,
            max_size=config.max_size,
            timeout=config.timeout,
            kwargs={"autocommit": True},
            configure=_configure,
            check=ConnectionPool.check_connection,
            name=APPLICATION_NAME,
            open=False,
        )

    def open(self) -> None:
        """Start the pool. Connects in the background: an unreachable server doesn't raise."""
        self._pool.open()

    def close(self) -> None:
        """Close every connection; borrowing afterwards raises `psycopg_pool.PoolClosed`."""
        self._pool.close()

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[Connection[TupleRow]]:
        """Borrow a connection, returning it to the pool on exit.

        Waits up to `timeout` seconds (default: the pool's) for a free one, then raises
        `psycopg_pool.PoolTimeout`.
        """
        with self._pool.connection(timeout=timeout) as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Connection[TupleRow]]:
        """Borrow a connection inside a transaction: commit on success, roll back on error."""
        with self.connection() as conn, conn.transaction():
            yield conn


_open: dict[DatabaseConfig, Database] = {}
_open_lock = threading.Lock()


def open_database(config: DatabaseConfig) -> Database:
    """The open pool for `config`, opening it on first use: one pool per config per process."""
    with _open_lock:
        database = _open.get(config)
        if database is None:
            database = Database(config)
            database.open()
            _open[config] = database
        return database


def close_databases() -> None:
    """Close every pool `open_database` opened; the next call opens a fresh one."""
    with _open_lock:
        databases = list(_open.values())
        _open.clear()
    for database in databases:
        database.close()


def get_database(settings: Annotated[Settings, Depends(get_settings)]) -> Database:
    """Dependency providing the pool for the configured database."""
    return open_database(database_config(settings))
