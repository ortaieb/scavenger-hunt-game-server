"""Published sessions' rows: hunts stored in the database beside the sessions file.

Only storage: what a session is, and the rules it must meet, live in `sessions`. A published
session is stored as its sessions-file document, with its codes in `session_codes`, whose
primary key makes the database refuse a code used twice, even when two publishes race.
"""

from collections.abc import Collection, Sequence
from uuid import UUID

from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from game_server.database import Database
from game_server.session_runs import SessionRun


class AlreadyPublishedError(Exception):
    """The session id, or one of its codes, is already taken (the database refused it)."""


class PublishedSessionRows:
    """The `published_sessions` and `session_codes` tables."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def document(self, session: UUID) -> JsonValue | None:
        """The session's sessions-file document, or None if it isn't published."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT document FROM published_sessions WHERE id = %s", (session,)
            ).fetchone()
        return None if row is None else row[0]

    def documents(self) -> list[JsonValue]:
        """Every published session's document, oldest first."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT document FROM published_sessions ORDER BY published_at, id"
            ).fetchall()
        return [document for (document,) in rows]

    def runs(self, sessions: Collection[UUID]) -> dict[UUID, SessionRun]:
        """The runs of those of these sessions that were ever started (from `session_runs`)."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT session, started_at, stopped_at FROM session_runs WHERE session = ANY(%s)",
                (list(sessions),),
            ).fetchall()
        return {
            session: SessionRun(started_at, stopped_at) for session, started_at, stopped_at in rows
        }

    def session_for_code(self, code: str) -> UUID | None:
        """The published session a normalised join code belongs to, if any."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT session FROM session_codes WHERE code = %s AND kind = 'join'", (code,)
            ).fetchone()
        return None if row is None else row[0]

    def codes_in_use(self, codes: Collection[str]) -> set[str]:
        """Which of these normalised codes a published session already uses."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT code FROM session_codes WHERE code = ANY(%s)", (list(codes),)
            ).fetchall()
        return {code for (code,) in rows}

    def insert(
        self,
        session: UUID,
        document: JsonValue,
        draft: UUID | None,
        codes: Sequence[str],
    ) -> None:
        """Store the session and its codes in one transaction; `AlreadyPublishedError` if
        the id or a code is taken, and then nothing is stored."""
        try:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO published_sessions (id, document, draft, published_at)"
                    " VALUES (%s, %s, %s, now())",
                    (session, Jsonb(document), draft),
                )
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO session_codes (code, session, kind) VALUES (%s, %s, 'join')",
                        [(code, session) for code in codes],
                    )
        except pg_errors.UniqueViolation:
            # `from None`: the database's message quotes the duplicate key, a code.
            raise AlreadyPublishedError from None
