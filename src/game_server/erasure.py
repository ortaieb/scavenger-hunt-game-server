"""Erasing a person (the right to be forgotten), and what the game holds of them.

The game knows a person only by `pid`, so one job keyed by `pid` erases them:

1. The request is recorded in `directory.erasures`, the person is marked `erasing`, and their
   Clerk sessions are revoked, so their tokens stop working within a minute.
2. **Game data**: their photo files are deleted, and so are their team memberships and their
   moderator rows. Their photos and check-ins stay, for their team's results (which
   checkpoints, when, the verdicts), but with no `pid`, and without what describes them: where
   they stood, the photo's fingerprint, the referee's reasons and trace, and ruling notes.
3. **Clerk**: their Clerk user is deleted, unless Clerk started the erasure.
4. **Directory**: their `people` row is deleted, and their profile, consents and role go with
   it. If they asked not to be contacted, a keyed hash of their email is kept first.
5. They're told it's done, and the erasure is marked complete.

Each step's time is recorded and every step can run again, so a failed job simply runs again:
the sweep retries unfinished erasures in the background. Clerk is deleted from before the
directory, which holds the Clerk user id. Logs name a person by `pid` only.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Protocol
from uuid import UUID

import psycopg
import psycopg_pool
from fastapi import Depends, status
from psycopg.types.json import Jsonb

from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database, database_config, open_database
from game_server.directory.clerk import ClerkError, ClerkUsers, get_clerk_users
from game_server.directory.erasures import (
    Erasure,
    ErasureRecords,
    ErasureSource,
    suppression_hmac,
)
from game_server.directory.repository import (
    DirectoryNotConfiguredError,
    DirectoryRepository,
    directory_database,
)
from game_server.errors import ApiError
from game_server.storage import ImageStore

logger = logging.getLogger(__name__)

# Their photos and the rows that hang off them, scrubbed of what describes them. Checks and
# rejections keep which ran and how they came out, without the reasons, which can describe the
# photo.
_ERASE_GAME_DATA = (
    "DELETE FROM referee_traces"
    " WHERE submission_id IN (SELECT id FROM submissions WHERE pid = %(pid)s)",
    "UPDATE rulings SET note = NULL"
    " WHERE submission_id IN (SELECT id FROM submissions WHERE pid = %(pid)s)"
    " AND note IS NOT NULL",
    "UPDATE submissions SET pid = NULL, lat = NULL, long = NULL, distance_m = NULL, phash = NULL,"
    " checks = (SELECT coalesce(jsonb_agg(c || %(check_scrub)s ORDER BY i), '[]'::jsonb)"
    "   FROM jsonb_array_elements(checks) WITH ORDINALITY AS e(c, i)),"
    " rejections = (SELECT coalesce(jsonb_agg(r || %(rejection_scrub)s ORDER BY i), '[]'::jsonb)"
    "   FROM jsonb_array_elements(rejections) WITH ORDINALITY AS e(r, i))"
    " WHERE pid = %(pid)s",
    "UPDATE arrivals SET pid = NULL WHERE pid = %(pid)s",
    "DELETE FROM team_members WHERE pid = %(pid)s",
    "DELETE FROM session_moderators WHERE pid = %(pid)s",
    "UPDATE session_moderators SET added_by = NULL WHERE added_by = %(pid)s",
    "UPDATE rulings SET pid = NULL WHERE pid = %(pid)s",
    "UPDATE hunt_drafts SET pid = NULL WHERE pid = %(pid)s",
)


@dataclass(frozen=True)
class PlayHistory:
    """What the game holds about a person, for their export: rows of plain JSON values."""

    teams: list[dict[str, Any]]
    photos: list[dict[str, Any]]
    check_ins: list[dict[str, Any]]
    rulings: list[dict[str, Any]]
    moderating: list[dict[str, Any]]
    drafts: list[dict[str, Any]]


class GameRecords:
    """The person's rows in the game's tables: erasing them, and reading them for an export."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def photo_ids(self, pid: UUID) -> list[UUID]:
        """The image ids of the photos the person sent and that still carry their pid."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT image_id FROM submissions WHERE pid = %s ORDER BY id", (pid,)
            ).fetchall()
        return [image_id for (image_id,) in rows]

    def erase(self, pid: UUID) -> None:
        """Take the person out of the game's tables, in one transaction (see the module)."""
        params = {
            "pid": pid,
            "check_scrub": Jsonb({"reason": "", "detail": None}),
            "rejection_scrub": Jsonb({"message": ""}),
        }
        with self._database.transaction() as conn:
            for statement in _ERASE_GAME_DATA:
                conn.execute(statement, params)

    def history(self, pid: UUID) -> PlayHistory:
        """Everything the game holds by the person's pid, read in one snapshot. No photos."""
        with self._database.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

            def rows(query: str) -> list[dict[str, Any]]:
                cursor = conn.execute(query, (pid,))
                names = [column.name for column in cursor.description or ()]
                return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

            return PlayHistory(
                teams=rows(
                    "SELECT m.session, m.participant, p.team, m.joined_at, m.photo_consent_at"
                    " FROM team_members m JOIN participants p ON p.id = m.participant"
                    " WHERE m.pid = %s ORDER BY m.joined_at, m.session"
                ),
                photos=[
                    {**photo, "checks": _player_checks(photo["checks"])}
                    for photo in rows(
                        "SELECT session, checkpoint, attempt, received_at, capture_time, lat,"
                        " long, verdict, effective_verdict, checks, rejections"
                        " FROM ruled_submissions WHERE pid = %s ORDER BY received_at, id"
                    )
                ],
                check_ins=rows(
                    "SELECT session, checkpoint, issued_at FROM arrivals"
                    " WHERE pid = %s ORDER BY issued_at, id"
                ),
                rulings=rows(
                    "SELECT session, submission_id, ruling, note, ruled_at FROM rulings"
                    " WHERE pid = %s ORDER BY ruled_at, id"
                ),
                moderating=rows(
                    "SELECT session, added_at FROM session_moderators"
                    " WHERE pid = %s ORDER BY added_at, session"
                ),
                drafts=rows(
                    "SELECT id, status, request, created_at, published_session FROM hunt_drafts"
                    " WHERE pid = %s ORDER BY created_at, id"
                ),
            )


def _player_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The checks as a player may see them: without the moderator-only `detail`."""
    return [{key: value for key, value in check.items() if key != "detail"} for check in checks]


class ErasureNotifier(Protocol):
    """Tells a person their erasure is done."""

    def erased(self, pid: UUID, email: str | None) -> None:
        """The erasure of `pid` is complete; `email` is where to tell them, if it was known."""
        ...


class LogNotifier:
    """Until there is an email provider: logs that the person would be told, by pid only."""

    def erased(self, pid: UUID, email: str | None) -> None:
        """Log the completion, never the address."""
        logger.info(
            "Erasure of person %s complete; to tell them: %s",
            pid,
            "no email provider yet" if email else "no email address known",
        )


class ErasureBlockedError(Exception):
    """A step can't run yet (no Clerk key, no suppression key): the erasure waits for it."""


class ErasureJob:
    """Requests, runs and replays erasures."""

    def __init__(
        self,
        records: ErasureRecords,
        directory: DirectoryRepository,
        game: GameRecords,
        images: ImageStore,
        clerk: ClerkUsers | None,
        suppression_key: str | None,
        clock: Clock,
        notifier: ErasureNotifier | None = None,
    ) -> None:
        self._records = records
        self._directory = directory
        self._game = game
        self._images = images
        self._clerk = clerk
        self._suppression_key = suppression_key
        self._clock = clock
        self._notifier = notifier or LogNotifier()

    def request(self, pid: UUID, source: ErasureSource, do_not_contact: bool) -> Erasure:
        """Step 1: record the request, mark the person `erasing` and revoke their Clerk
        sessions. A failed revocation is logged: deleting the Clerk user ends them too."""
        erasure = self._records.request(pid, source, do_not_contact, self._clock())
        if self._directory.mark_erasing(pid):
            logger.info("Person %s to be erased (%s)", pid, source)
        if source != "clerk" and self._clerk is not None:
            user_id = self._records.clerk_user_id(pid)
            if user_id is not None:
                try:
                    self._clerk.revoke_sessions(user_id)
                except ClerkError as exc:
                    logger.warning("Revoking the sessions of person %s failed (%s)", pid, exc)
        return erasure

    def find(self, pid: UUID) -> Erasure | None:
        """The person's erasure, if one was asked for."""
        return self._records.get(pid)

    def run(self, pid: UUID) -> bool:
        """Run the person's erasure to the end; True once it's complete.

        False if it's still unfinished: a step failed or is blocked (logged), or another run
        holds it. Then the sweep tries again later.
        """
        with self._records.lock(pid) as held:
            if not held:
                return False
            erasure = self._records.get(pid)
            if erasure is None:
                return False
            if erasure.completed_at is not None:
                return True
            return self._attempt(erasure, notify=True)

    def run_unfinished(self) -> int:
        """Run every unfinished erasure; how many completed."""
        return sum(self.run(pid) for pid in self._records.unfinished())

    def replay(self, pid: UUID, do_not_contact: bool = False) -> bool:
        """Erase the person again, every step, without telling them: after a restore from a
        backup brought their rows back. An erasure the restore lost is recorded again."""
        with self._records.lock(pid) as held:
            if not held:
                return False
            # Keeps a surviving record's source; adds one the restore lost.
            erasure = self._records.request(pid, "replay", do_not_contact, self._clock())
            return self._attempt(erasure, notify=False)

    def _attempt(self, erasure: Erasure, notify: bool) -> bool:
        try:
            self._steps(erasure, notify)
        except ErasureBlockedError as exc:
            logger.warning("Erasure of person %s waits: %s", erasure.pid, exc)
            return False
        except (psycopg.Error, psycopg_pool.PoolTimeout, OSError, ClerkError) as exc:
            logger.warning("Erasure of person %s failed (%s): retrying later", erasure.pid, exc)
            return False
        return True

    def _steps(self, erasure: Erasure, notify: bool) -> None:
        pid = erasure.pid
        profile = self._directory.profile(pid)  # read now: the directory step deletes it
        email = profile.email if profile is not None else None
        self._directory.mark_erasing(pid)
        for image_id in self._game.photo_ids(pid):
            self._images.path_for(image_id).unlink(missing_ok=True)
        self._game.erase(pid)
        self._records.step_done(pid, "game", self._clock())
        self._erase_from_clerk(erasure)
        self._records.step_done(pid, "clerk", self._clock())
        self._records.erase_person(pid, self._suppression(erasure, email))
        self._records.step_done(pid, "directory", self._clock())
        if notify:
            self._notifier.erased(pid, email)
        self._records.complete(pid, self._clock())
        logger.info("Erasure of person %s done", pid)

    def _erase_from_clerk(self, erasure: Erasure) -> None:
        if erasure.source == "clerk":
            return  # Clerk deleted them already
        user_id = self._records.clerk_user_id(erasure.pid)
        if user_id is None:
            return
        if self._clerk is None:
            raise ErasureBlockedError("no Clerk key to delete their Clerk user")
        self._clerk.delete_user(user_id)

    def _suppression(self, erasure: Erasure, email: str | None) -> bytes | None:
        if not erasure.do_not_contact or email is None:
            return None
        if self._suppression_key is None:
            raise ErasureBlockedError("asked not to be contacted, and no suppression key")
        return suppression_hmac(self._suppression_key, email)


def erasure_job(settings: Settings, clock: Clock, clerk: ClerkUsers | None) -> ErasureJob:
    """The job on the configured databases and photo store, with this Clerk client.

    Raises `DirectoryNotConfiguredError` if there is no directory.
    """
    directory_db = directory_database(settings)
    key = settings.suppression_key
    return ErasureJob(
        ErasureRecords(directory_db),
        DirectoryRepository(directory_db),
        GameRecords(open_database(database_config(settings))),
        ImageStore(settings.image_base_path),
        clerk,
        key.get_secret_value() if key is not None else None,
        clock,
    )


def get_erasure_job(
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    clerk: Annotated[ClerkUsers | None, Depends(get_clerk_users)],
) -> ErasureJob:
    """Dependency providing the job, or `503 directory_unavailable` without a directory."""
    try:
        return erasure_job(settings, clock, clerk)
    except DirectoryNotConfiguredError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the users directory isn't set up",
            "directory_unavailable",
        ) from None


def run_now(job: ErasureJob, pid: UUID | None = None) -> None:
    """Run one erasure (or every unfinished one) straight after a request recorded it, as a
    background task. A failure waits for the sweep, which retries it."""
    try:
        if pid is None:
            job.run_unfinished()
        else:
            job.run(pid)
    except (psycopg.Error, psycopg_pool.PoolTimeout) as exc:
        logger.warning("Erasure run failed (%s): the sweep retries it", type(exc).__name__)


class ErasureSweep:
    """The background task: runs unfinished erasures every `interval`."""

    def __init__(self, run: Callable[[], int], interval: timedelta) -> None:
        self._run_unfinished = run
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def tick(self) -> None:
        """One round. Never raises for a database failure: the next round tries again."""
        try:
            self._run_unfinished()
        except (psycopg.Error, psycopg_pool.PoolTimeout) as exc:
            logger.warning("Erasure sweep failed (%s)", type(exc).__name__)

    def start(self) -> None:
        """Run rounds on the current event loop until `stop`."""
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the task and wait for it to end."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(self._interval.total_seconds())


def start_erasure_sweep(settings: Settings, clock: Clock) -> ErasureSweep | None:
    """Start the sweep on the current event loop, if there is a directory; else None."""
    try:
        job = erasure_job(settings, clock, get_clerk_users(settings))
    except DirectoryNotConfiguredError:
        return None
    sweep = ErasureSweep(
        job.run_unfinished, timedelta(seconds=settings.erasure_sweep_interval_seconds)
    )
    sweep.start()
    return sweep
