"""`directory.erasures`: each erasure and how far it got, and the directory's own part of it.

An erasure row holds no identity (only the `pid`), so it outlives the person: it's the evidence
that the erasure happened, and the list to replay after a restore from a backup. Each step's
time is set once it's done; every step can run again safely.
"""

import hashlib
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from game_server.database import Database

ErasureSource = Literal["self", "admin", "clerk", "replay"]
Step = Literal["game", "clerk", "directory"]

# Marking a step done keeps the first time it was.
_STEP_DONE: dict[Step, str] = {
    "game": "UPDATE directory.erasures SET game_done_at = coalesce(game_done_at, %s)"
    " WHERE pid = %s",
    "clerk": "UPDATE directory.erasures SET clerk_done_at = coalesce(clerk_done_at, %s)"
    " WHERE pid = %s",
    "directory": "UPDATE directory.erasures"
    " SET directory_done_at = coalesce(directory_done_at, %s) WHERE pid = %s",
}


@dataclass(frozen=True)
class Erasure:
    """An erasure: who asked (`source`), whether to suppress contact, and when each step ended."""

    pid: UUID
    source: ErasureSource
    do_not_contact: bool
    requested_at: datetime
    game_done_at: datetime | None
    clerk_done_at: datetime | None
    directory_done_at: datetime | None
    completed_at: datetime | None


def suppression_hmac(key: str, email: str) -> bytes:
    """The keyed hash a suppression is stored by: never the address itself.

    The address is normalised (trimmed, lower-cased), so the same mailbox always matches.
    """
    normalised = email.strip().lower()
    return hmac.new(key.encode(), normalised.encode(), hashlib.sha256).digest()


class ErasureRecords:
    """The `directory.erasures` table, and deleting a person from the directory."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def request(
        self, pid: UUID, source: ErasureSource, do_not_contact: bool, now: datetime
    ) -> Erasure:
        """Record that the person is to be erased; a repeated request keeps the first one, and
        asking not to be contacted is never undone."""
        with self._database.connection() as conn:
            row = conn.execute(
                "INSERT INTO directory.erasures (pid, source, do_not_contact, requested_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (pid) DO UPDATE"
                " SET do_not_contact = erasures.do_not_contact OR EXCLUDED.do_not_contact"
                " RETURNING pid, source, do_not_contact, requested_at, game_done_at,"
                " clerk_done_at, directory_done_at, completed_at",
                (pid, source, do_not_contact, now),
            ).fetchone()
        if row is None:
            raise RuntimeError("the upsert returned no row")
        return Erasure(*row)

    def get(self, pid: UUID) -> Erasure | None:
        """The person's erasure, if one was asked for."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT pid, source, do_not_contact, requested_at, game_done_at, clerk_done_at,"
                " directory_done_at, completed_at FROM directory.erasures WHERE pid = %s",
                (pid,),
            ).fetchone()
        return None if row is None else Erasure(*row)

    def unfinished(self) -> list[UUID]:
        """The pids of erasures not yet complete, oldest first."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT pid FROM directory.erasures WHERE completed_at IS NULL"
                " ORDER BY requested_at, pid"
            ).fetchall()
        return [pid for (pid,) in rows]

    def completed(self) -> list[Erasure]:
        """Every complete erasure, oldest first."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT pid, source, do_not_contact, requested_at, game_done_at, clerk_done_at,"
                " directory_done_at, completed_at FROM directory.erasures"
                " WHERE completed_at IS NOT NULL ORDER BY requested_at, pid"
            ).fetchall()
        return [Erasure(*row) for row in rows]

    def step_done(self, pid: UUID, step: Step, now: datetime) -> None:
        """Mark a step done, keeping the first time it was."""
        with self._database.connection() as conn:
            conn.execute(_STEP_DONE[step], (now, pid))

    def complete(self, pid: UUID, now: datetime) -> None:
        """Mark the erasure complete, keeping the first time it was."""
        with self._database.connection() as conn:
            conn.execute(
                "UPDATE directory.erasures SET completed_at = coalesce(completed_at, %s)"
                " WHERE pid = %s",
                (now, pid),
            )

    @contextmanager
    def lock(self, pid: UUID) -> Iterator[bool]:
        """Hold the person's erasure lock for the block, if no one else holds it: yields whether
        this caller got it. Two runs of one erasure (a request and the sweep, or two server
        processes) never overlap."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('erasure:' || %s, 0))",
                (str(pid),),
            ).fetchone()
            held = bool(row and row[0])
            try:
                yield held
            finally:
                if held:
                    conn.execute(
                        "SELECT pg_advisory_unlock(hashtextextended('erasure:' || %s, 0))",
                        (str(pid),),
                    )

    def clerk_user_id(self, pid: UUID) -> str | None:
        """The person's Clerk user id, while the directory still has them."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT clerk_user_id FROM directory.people WHERE pid = %s", (pid,)
            ).fetchone()
        return None if row is None else str(row[0])

    def erase_person(self, pid: UUID, suppression: bytes | None) -> None:
        """Delete the person from the directory, in one transaction.

        Their profile, consents, role grant, role history and outbox rows go with their
        `people` row, by cascade. Their pid is also cleared where it records them granting
        someone else's role. With `suppression`, its keyed hash is added first, so they are
        never contacted again.
        """
        with self._database.transaction() as conn:
            if suppression is not None:
                conn.execute(
                    "INSERT INTO directory.suppressions (email_hmac, reason)"
                    " VALUES (%s, 'erasure') ON CONFLICT (email_hmac) DO NOTHING",
                    (suppression,),
                )
            conn.execute(
                "UPDATE directory.role_grants SET granted_by = NULL WHERE granted_by = %s", (pid,)
            )
            conn.execute(
                "UPDATE directory.role_changes SET changed_by = NULL WHERE changed_by = %s",
                (pid,),
            )
            conn.execute("DELETE FROM directory.people WHERE pid = %s", (pid,))
