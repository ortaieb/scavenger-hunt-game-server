"""Keeping Clerk in step with the directory: the outbox worker and the nightly reconcile.

Clerk copies each person's `pid` and `role` from their public metadata into every session
token, so the directory writes them there ahead of time. The outbox retries writes until they
land; the nightly reconcile queues a write for any active person whose Clerk user differs. The
directory is the source of truth: edits made in the Clerk Dashboard are overwritten.

Logs name a person by `pid` only.
"""

import asyncio
import contextlib
import logging
from datetime import datetime, time, timedelta

import psycopg
import psycopg_pool

from game_server.clock import Clock
from game_server.config import Settings
from game_server.directory.clerk import ClerkError, ClerkUsers, get_clerk_users
from game_server.directory.outbox import ClerkOutbox
from game_server.directory.repository import (
    DirectoryNotConfiguredError,
    DirectoryRepository,
    directory_database,
)

logger = logging.getLogger(__name__)

BATCH = 20


def send_due(outbox: ClerkOutbox, clerk: ClerkUsers, clock: Clock) -> int:
    """Send every due write, a batch at a time; returns how many landed."""
    landed = 0
    while writes := outbox.claim(clock(), BATCH):
        for write in writes:
            try:
                clerk.write_identity(write.clerk_user_id, write.pid, write.role)
            except ClerkError as exc:
                outbox.retry_later(write, clock())
                logger.warning(
                    "Clerk write for person %s failed (%s, attempt %d): retrying later",
                    write.pid,
                    exc,
                    write.attempts + 1,
                )
            else:
                outbox.done(write.id, clock())
                landed += 1
        if len(writes) < BATCH:
            break
    return landed


def reconcile(
    directory: DirectoryRepository, outbox: ClerkOutbox, clerk: ClerkUsers, clock: Clock
) -> int:
    """Queue a write for every active person whose Clerk user doesn't hold their identity;
    returns how many. Clerk users the directory doesn't know are left alone: they're
    provisioned when they next sign in."""
    wanted = directory.active_identities()
    queued = 0
    for found in clerk.identities():
        identity = wanted.get(found.user_id)
        if identity is None:
            continue
        pid = str(identity.pid)
        if (found.external_id, found.pid, found.role) != (pid, pid, identity.role):
            outbox.enqueue(identity.pid, clock())
            logger.info("Reconcile: person %s differs in Clerk, write queued", identity.pid)
            queued += 1
    return queued


def next_reconcile_at(now: datetime, hour: int) -> datetime:
    """The next `hour`:00 UTC after `now`."""
    at = datetime.combine(now.date(), time(hour), tzinfo=now.tzinfo)
    return at if at > now else at + timedelta(days=1)


class ClerkSync:
    """The background task: sends due writes every `interval`, and reconciles nightly."""

    def __init__(
        self,
        directory: DirectoryRepository,
        outbox: ClerkOutbox,
        clerk: ClerkUsers,
        clock: Clock,
        interval: timedelta,
        reconcile_hour: int,
    ) -> None:
        self._directory = directory
        self._outbox = outbox
        self._clerk = clerk
        self._clock = clock
        self._interval = interval
        self._reconcile_hour = reconcile_hour
        self._next_reconcile = next_reconcile_at(clock(), reconcile_hour)
        self._task: asyncio.Task[None] | None = None

    def tick(self) -> None:
        """One round: send what's due, and reconcile if it's time. Never raises for a database
        or Clerk failure: the next round tries again."""
        try:
            send_due(self._outbox, self._clerk, self._clock)
            if self._clock() >= self._next_reconcile:
                self._next_reconcile = next_reconcile_at(self._clock(), self._reconcile_hour)
                queued = reconcile(self._directory, self._outbox, self._clerk, self._clock)
                logger.info("Reconcile with Clerk done: %d writes queued", queued)
        except (psycopg.Error, psycopg_pool.PoolTimeout, ClerkError) as exc:
            logger.warning("Clerk sync round failed (%s)", type(exc).__name__)

    def start(self) -> None:
        """Run rounds on the current event loop until `stop`."""
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Cancel the task and wait for it to end."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(self._interval.total_seconds())


def start_clerk_sync(settings: Settings, clock: Clock) -> ClerkSync | None:
    """Start the background task on the current event loop, if there is a directory and a
    Clerk key; else None."""
    clerk = get_clerk_users(settings)
    if clerk is None:
        return None
    try:
        database = directory_database(settings)
    except DirectoryNotConfiguredError:
        return None
    sync = ClerkSync(
        DirectoryRepository(database),
        ClerkOutbox(database),
        clerk,
        clock,
        timedelta(seconds=settings.clerk_sync_interval_seconds),
        settings.clerk_reconcile_hour_utc,
    )
    sync.start()
    return sync
