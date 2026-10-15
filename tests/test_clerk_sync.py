"""Keeping Clerk in step: the outbox, the worker that sends it, and the nightly reconcile."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from clerk_fakes import FakeClerk, FakeClock
from psycopg.rows import DictRow

from game_server.config import Settings, get_settings
from game_server.directory import DirectoryRepository, get_directory
from game_server.directory.clerk import ClerkIdentity, get_clerk_users
from game_server.directory.outbox import LEASE, ClerkOutbox, backoff, get_clerk_outbox
from game_server.directory.sync import (
    ClerkSync,
    next_reconcile_at,
    reconcile,
    send_due,
    start_clerk_sync,
)

CLERK_KEY = "sk_test_never_sent"  # a fake: no test calls Clerk


@pytest.fixture
def directory() -> DirectoryRepository:
    return get_directory(get_settings())


@pytest.fixture
def outbox() -> ClerkOutbox:
    return get_clerk_outbox(get_settings())


@pytest.fixture
def clerk() -> FakeClerk:
    return FakeClerk()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def person(directory: DirectoryRepository, user: str = "user_a") -> UUID:
    return directory.get_or_create_person(user, f"{user}@example.com")


def grant(db: psycopg.Connection[DictRow], pid: UUID, role: str) -> None:
    db.execute("INSERT INTO directory.role_grants (pid, role) VALUES (%s, %s)", (pid, role))


# --- the outbox ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "seconds"), [(0, 30), (1, 30), (2, 60), (3, 120), (7, 1920), (8, 3600), (99, 3600)]
)
def test_backoff_doubles_from_30_seconds_up_to_an_hour(attempts: int, seconds: int) -> None:
    assert backoff(attempts) == timedelta(seconds=seconds)


def test_a_due_write_is_sent_and_done(
    directory: DirectoryRepository, outbox: ClerkOutbox, clerk: FakeClerk, clock: FakeClock
) -> None:
    pid = person(directory)
    outbox.enqueue(pid, clock())

    assert send_due(outbox, clerk, clock) == 1

    assert clerk.writes == [("user_a", pid, "player")]
    assert outbox.pending(pid) == 0
    assert send_due(outbox, clerk, clock) == 0


def test_a_write_not_yet_due_waits(
    directory: DirectoryRepository, outbox: ClerkOutbox, clerk: FakeClerk, clock: FakeClock
) -> None:
    outbox.enqueue(person(directory), clock() + timedelta(seconds=1))

    assert send_due(outbox, clerk, clock) == 0


def test_the_role_sent_is_the_one_held_when_sending(
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: FakeClerk,
    clock: FakeClock,
    db: psycopg.Connection[DictRow],
) -> None:
    pid = person(directory)
    outbox.enqueue(pid, clock())
    grant(db, pid, "admin")

    send_due(outbox, clerk, clock)

    assert clerk.writes == [("user_a", pid, "admin")]


def test_a_failed_write_is_retried_after_the_backoff(
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: FakeClerk,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pid = person(directory)
    outbox.enqueue(pid, clock())
    clerk.fail_writes = True

    with caplog.at_level(logging.WARNING):
        assert send_due(outbox, clerk, clock) == 0
    clerk.fail_writes = False
    clock.now += timedelta(seconds=29)
    assert send_due(outbox, clerk, clock) == 0
    clock.now += timedelta(seconds=1)
    assert send_due(outbox, clerk, clock) == 1

    assert [r.getMessage() for r in caplog.records] == [
        f"Clerk write for person {pid} failed (ConnectError, attempt 1): retrying later"
    ]


def test_failures_back_off_further_each_time(
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: FakeClerk,
    clock: FakeClock,
    db: psycopg.Connection[DictRow],
) -> None:
    start = clock()
    outbox.enqueue(person(directory), start)
    clerk.fail_writes = True

    send_due(outbox, clerk, clock)
    clock.now = start + timedelta(seconds=30)
    send_due(outbox, clerk, clock)

    row = db.execute("SELECT attempts, next_attempt_at FROM directory.clerk_outbox").fetchone()
    assert row == {"attempts": 2, "next_attempt_at": start + timedelta(seconds=90)}


def test_a_claimed_write_isnt_claimed_again_until_its_lease_ends(
    directory: DirectoryRepository, outbox: ClerkOutbox, clock: FakeClock
) -> None:
    outbox.enqueue(person(directory), clock())

    assert len(outbox.claim(clock(), 10)) == 1
    assert outbox.claim(clock() + LEASE - timedelta(seconds=1), 10) == []
    assert len(outbox.claim(clock() + LEASE, 10)) == 1


def test_an_erasing_persons_writes_are_never_sent(
    directory: DirectoryRepository, outbox: ClerkOutbox, clerk: FakeClerk, clock: FakeClock
) -> None:
    pid = person(directory)
    outbox.enqueue(pid, clock())
    directory.mark_erasing(pid)

    assert send_due(outbox, clerk, clock) == 0
    assert clerk.writes == []


def test_more_writes_than_a_batch_are_all_sent(
    directory: DirectoryRepository, outbox: ClerkOutbox, clerk: FakeClerk, clock: FakeClock
) -> None:
    for n in range(45):
        outbox.enqueue(person(directory, f"user_{n}"), clock())

    assert send_due(outbox, clerk, clock) == 45


# --- the nightly reconcile -----------------------------------------------------------------------


def test_reconcile_queues_only_the_people_clerk_holds_wrongly(
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: FakeClerk,
    clock: FakeClock,
    db: psycopg.Connection[DictRow],
) -> None:
    right, wrong_role, no_external, edited, erasing = (
        person(directory, user)
        for user in ("user_right", "user_role", "user_ext", "user_edit", "user_gone")
    )
    grant(db, wrong_role, "organiser")
    directory.mark_erasing(erasing)
    clerk.users = {
        identity.user_id: identity
        for identity in (
            ClerkIdentity("user_right", str(right), str(right), "player"),
            ClerkIdentity("user_role", str(wrong_role), str(wrong_role), "player"),
            ClerkIdentity("user_ext", None, str(no_external), "player"),
            ClerkIdentity("user_edit", str(edited), str(edited), "admin"),  # set in the Dashboard
            ClerkIdentity("user_gone", None, None, None),
            ClerkIdentity("user_unknown", None, None, None),
        )
    }

    assert reconcile(directory, outbox, clerk, clock) == 3

    queued = {
        pid for pid in (right, wrong_role, no_external, edited, erasing) if outbox.pending(pid)
    }
    assert queued == {wrong_role, no_external, edited}


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 15, 1, 0, tzinfo=UTC), datetime(2026, 10, 15, 3, 0, tzinfo=UTC)),
        (datetime(2026, 10, 15, 3, 0, tzinfo=UTC), datetime(2026, 10, 16, 3, 0, tzinfo=UTC)),
        (datetime(2026, 10, 15, 22, 0, tzinfo=UTC), datetime(2026, 10, 16, 3, 0, tzinfo=UTC)),
    ],
)
def test_the_reconcile_runs_at_the_next_hour_set(now: datetime, expected: datetime) -> None:
    assert next_reconcile_at(now, 3) == expected


# --- the background task -------------------------------------------------------------------------


def sync_for(
    directory: DirectoryRepository, outbox: ClerkOutbox, clerk: FakeClerk, clock: FakeClock
) -> ClerkSync:
    return ClerkSync(directory, outbox, clerk, clock, timedelta(milliseconds=10), reconcile_hour=3)


def test_a_round_sends_and_reconciles_when_its_time(
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: FakeClerk,
    clock: FakeClock,
) -> None:
    pid = person(directory)
    sync = sync_for(directory, outbox, clerk, clock)  # next reconcile: 03:00 tomorrow
    clerk.users["user_a"] = ClerkIdentity("user_a", None, None, None)

    sync.tick()
    assert clerk.writes == []  # nothing queued, and not yet time to reconcile
    clock.now = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    sync.tick()  # reconciles, queuing a write
    sync.tick()  # sends it

    assert clerk.writes == [("user_a", pid, "player")]


def test_a_failing_round_doesnt_stop_the_next(
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: FakeClerk,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sync = sync_for(directory, outbox, clerk, clock)
    clock.now = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    clerk.fail_reads = True

    with caplog.at_level(logging.WARNING):
        sync.tick()

    assert [r.getMessage() for r in caplog.records] == ["Clerk sync round failed (ClerkError)"]


def test_the_task_sends_in_the_background_until_stopped(
    directory: DirectoryRepository, outbox: ClerkOutbox, clerk: FakeClerk, clock: FakeClock
) -> None:
    pid = person(directory)
    outbox.enqueue(pid, clock())

    async def run() -> None:
        sync = sync_for(directory, outbox, clerk, clock)
        sync.start()
        for _ in range(200):
            if clerk.writes:
                break
            await asyncio.sleep(0.01)
        await sync.stop()

    asyncio.run(run())

    assert clerk.writes == [("user_a", pid, "player")]


def test_the_task_starts_only_with_a_directory_and_a_clerk_key(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    async def started(settings: Settings) -> bool:
        sync = start_clerk_sync(settings, clock)
        if sync is None:
            return False
        await sync.stop()
        return True

    with_key = Settings(clerk_secret_key=CLERK_KEY)
    assert asyncio.run(started(with_key))
    assert not asyncio.run(started(Settings()))
    monkeypatch.delenv("GAME_SERVER_DIRECTORY_DB_URL")
    assert not asyncio.run(started(Settings(clerk_secret_key=CLERK_KEY)))


def test_the_clerk_client_needs_its_key() -> None:
    assert get_clerk_users(Settings()) is None
    assert get_clerk_users(Settings(clerk_secret_key=CLERK_KEY)) is not None


def test_the_clerk_client_keeps_request_urls_out_of_the_logs() -> None:
    get_clerk_users(Settings(clerk_secret_key=CLERK_KEY))

    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
