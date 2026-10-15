"""`directory.clerk_outbox`: writes to Clerk, kept until they land.

Each row asks for one person's identity (pid and role) to be written to their Clerk user. What
is written is read when the row is sent, so a row never pushes a stale role. A worker claims due
rows by leasing them (pushing `next_attempt_at` past the send), so two workers never send one
row at once and a crashed worker's rows come back after the lease.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from psycopg.types.json import Jsonb

from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.directory.repository import directory_database

PAYLOAD = {"write": "identity"}
LEASE = timedelta(minutes=2)
FIRST_RETRY = timedelta(seconds=30)
LONGEST_RETRY = timedelta(hours=1)


def backoff(attempts: int) -> timedelta:
    """How long to wait after the `attempts`-th failure: doubling from 30 s, at most an hour."""
    doublings = min(max(attempts - 1, 0), 10)  # past 2**7 * 30 s the cap applies anyway
    return min(FIRST_RETRY * (1 << doublings), LONGEST_RETRY)


@dataclass(frozen=True)
class Write:
    """A claimed row: whose identity to write to which Clerk user, and failures so far."""

    id: int
    pid: UUID
    clerk_user_id: str
    role: str
    attempts: int


class ClerkOutbox:
    """The `directory.clerk_outbox` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def enqueue(self, pid: UUID, now: datetime) -> None:
        """Ask for the person's identity to be written to Clerk, from `now`."""
        with self._database.connection() as conn:
            conn.execute(
                "INSERT INTO directory.clerk_outbox (pid, payload, next_attempt_at, created_at)"
                " VALUES (%s, %s, %s, %s)",
                (pid, Jsonb(PAYLOAD), now, now),
            )

    def claim(self, now: datetime, limit: int) -> list[Write]:
        """Lease up to `limit` due rows of active people, oldest due first."""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "WITH due AS ("
                "  SELECT o.id FROM directory.clerk_outbox o"
                "  JOIN directory.people p ON p.pid = o.pid"
                "  WHERE o.done_at IS NULL AND o.next_attempt_at <= %(now)s"
                "    AND p.status = 'active'"
                "  ORDER BY o.next_attempt_at, o.id LIMIT %(limit)s"
                "  FOR UPDATE OF o SKIP LOCKED)"
                " UPDATE directory.clerk_outbox o SET next_attempt_at = %(lease)s"
                " FROM due, directory.people p"
                " LEFT JOIN directory.role_grants g ON g.pid = p.pid"
                " WHERE o.id = due.id AND p.pid = o.pid"
                " RETURNING o.id, o.pid, p.clerk_user_id, coalesce(g.role, 'player'), o.attempts",
                {"now": now, "limit": limit, "lease": now + LEASE},
            ).fetchall()
        return sorted((Write(*row) for row in rows), key=lambda write: write.id)

    def done(self, write_id: int, now: datetime) -> None:
        """The write landed."""
        with self._database.connection() as conn:
            conn.execute(
                "UPDATE directory.clerk_outbox SET done_at = %s WHERE id = %s", (now, write_id)
            )

    def retry_later(self, write: Write, now: datetime) -> None:
        """The write failed: count it and try again after the backoff."""
        attempts = write.attempts + 1
        with self._database.connection() as conn:
            conn.execute(
                "UPDATE directory.clerk_outbox SET attempts = %s, next_attempt_at = %s"
                " WHERE id = %s",
                (attempts, now + backoff(attempts), write.id),
            )

    def pending(self, pid: UUID) -> int:
        """How many of the person's writes haven't landed yet."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM directory.clerk_outbox WHERE pid = %s AND done_at IS NULL",
                (pid,),
            ).fetchone()
        return 0 if row is None else int(row[0])


def get_clerk_outbox(settings: Annotated[Settings, Depends(get_settings)]) -> ClerkOutbox:
    """Dependency providing the outbox, on the directory's pool.

    Raises `DirectoryNotConfiguredError` if the directory's URL isn't set.
    """
    return ClerkOutbox(directory_database(settings))
