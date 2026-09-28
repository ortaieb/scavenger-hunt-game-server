import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from game_server.checks import CheckResult, Rejection
from game_server.config import Settings
from game_server.submissions import (
    _MIGRATIONS,
    NewSubmission,
    SubmissionStore,
    get_submission_store,
    open_submission_store,
)

SESSION = UUID(int=1)
PARTICIPANT = UUID(int=2)

SUBMISSION = NewSubmission(
    session=SESSION,
    participant=PARTICIPANT,
    checkpoint=1,
    received_at=datetime(2026, 10, 3, 9, 30, tzinfo=UTC),
    capture_time=datetime(2026, 10, 3, 10, 29, tzinfo=timezone(timedelta(hours=1))),
    lat=51.5,
    long=-0.1,
    image_id=UUID(int=3),
    verdict="pending",
    checks=(),
    distance_m=12.5,
    phash=0xFEDC_BA98_7654_3210,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "game.sqlite3"


@pytest.fixture
def store(db_path: Path) -> SubmissionStore:
    return SubmissionStore(db_path)


def rows(db_path: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()


def test_creates_schema_and_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "game.sqlite3"

    SubmissionStore(db_path)

    assert rows(db_path) == []


def test_reopening_keeps_existing_rows(store: SubmissionStore, db_path: Path) -> None:
    store.record(SUBMISSION)

    SubmissionStore(db_path).record(SUBMISSION)

    assert [row["attempt"] for row in rows(db_path)] == [1, 2]


def test_records_all_fields(store: SubmissionStore, db_path: Path) -> None:
    submission = replace(
        SUBMISSION,
        verdict="failed",
        checks=(
            CheckResult.failed("window_open", Rejection("outside_window", "Closed")),
            CheckResult("scene_matches", "uncertain", 0.5, "Unsure.", detail="blurry"),
            CheckResult.failed("b_ok", Rejection("b", "B")),
        ),
    )

    recorded = store.record(submission)

    [row] = rows(db_path)
    assert row["id"] == recorded.id
    assert (row["session"], row["participant"]) == (str(SESSION), str(PARTICIPANT))
    assert (row["checkpoint"], row["attempt"]) == (1, 1)
    assert row["received_at"] == "2026-10-03T09:30:00+00:00"
    assert row["capture_time"] == "2026-10-03T10:29:00+01:00"
    assert (row["lat"], row["long"]) == (51.5, -0.1)
    assert row["image_id"] == str(UUID(int=3))
    assert row["verdict"] == "failed"
    assert row["distance_m"] == 12.5
    assert json.loads(row["rejections"]) == [
        {"code": "outside_window", "message": "Closed"},
        {"code": "b", "message": "B"},
    ]


def test_attempts_are_numbered_per_session_participant_and_checkpoint(
    store: SubmissionStore,
) -> None:
    other_session = replace(SUBMISSION, session=uuid4())
    other_participant = replace(SUBMISSION, participant=uuid4())
    other_checkpoint = replace(SUBMISSION, checkpoint=2)
    sequence = [
        SUBMISSION,
        SUBMISSION,
        other_session,
        other_participant,
        other_checkpoint,
        other_checkpoint,
        SUBMISSION,
    ]

    attempts = [store.record(submission).attempt for submission in sequence]

    assert attempts == [1, 2, 1, 1, 1, 2, 3]


def test_concurrent_submissions_get_distinct_attempts(store: SubmissionStore) -> None:
    count = 20

    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda _: store.record(SUBMISSION).attempt, range(count)))

    assert sorted(attempts) == list(range(1, count + 1))


def test_duplicate_attempt_is_refused_by_the_database(
    store: SubmissionStore, db_path: Path
) -> None:
    store.record(SUBMISSION)

    with closing(sqlite3.connect(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO submissions (session, participant, checkpoint, attempt, received_at,"
            " capture_time, lat, long, image_id, verdict, rejections)"
            " VALUES (?, ?, 1, 1, '', '', 0, 0, '', 'pending', '[]')",
            (str(SESSION), str(PARTICIPANT)),
        )


def test_failed_insert_is_rolled_back(store: SubmissionStore, db_path: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.record(replace(SUBMISSION, verdict="bogus"))  # type: ignore[arg-type]  # CHECK

    assert rows(db_path) == []
    assert store.record(SUBMISSION).attempt == 1


def test_open_store_is_cached_per_path(db_path: Path) -> None:
    assert open_submission_store(db_path) is open_submission_store(db_path)


def test_dependency_uses_configured_path(db_path: Path) -> None:
    store = get_submission_store(Settings(db_path=db_path))

    assert store.db_path == db_path.resolve()
    assert db_path.exists()


# --- schema migrations -------------------------------------------------------

# The table exactly as #8 created it, before any migration.
LATEST_VERSION = 11

V0_SCHEMA = """
CREATE TABLE submissions (
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, participant TEXT NOT NULL,
    checkpoint INTEGER NOT NULL, attempt INTEGER NOT NULL, received_at TEXT NOT NULL,
    capture_time TEXT NOT NULL, lat REAL NOT NULL, long REAL NOT NULL,
    image_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('failed', 'pending', 'pass')),
    rejections TEXT NOT NULL, UNIQUE (session, participant, checkpoint, attempt)
);
INSERT INTO submissions VALUES
    (1, '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002',
     1, 1, '', '', 0, 0, '', 'pending', '[]');
"""


def user_version(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
        return version


def test_new_database_is_fully_migrated(store: SubmissionStore, db_path: Path) -> None:
    assert user_version(db_path) == LATEST_VERSION
    store.record(SUBMISSION)
    assert rows(db_path)[0]["distance_m"] == 12.5


def test_migrates_database_created_before_distance_column(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(V0_SCHEMA)

    store = SubmissionStore(db_path)

    assert user_version(db_path) == LATEST_VERSION
    (old,) = rows(db_path)
    assert old["distance_m"] is None  # recorded before distances were measured
    assert store.record(SUBMISSION).attempt == 2  # the old row still counts as an attempt


def test_reopening_does_not_rerun_migrations(store: SubmissionStore, db_path: Path) -> None:
    store.record(SUBMISSION)

    SubmissionStore(db_path)

    assert user_version(db_path) == LATEST_VERSION
    assert len(rows(db_path)) == 1


def test_failed_migration_rolls_back_and_stops(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(V0_SCHEMA)
        # Version 1, but migration 2's column already exists: migration 2 will fail.
        conn.executescript(
            "ALTER TABLE submissions ADD COLUMN phash TEXT; PRAGMA user_version = 1;"
        )

    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        SubmissionStore(db_path)

    assert user_version(db_path) == 1


def test_upgrades_version_3_database_with_checks_column(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(V0_SCHEMA)
        for statement in _MIGRATIONS[:3]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 3")

    store = SubmissionStore(db_path)

    assert user_version(db_path) == LATEST_VERSION
    (old,) = rows(db_path)
    assert old["checks"] is None  # recorded before checks were listed
    store.record(SUBMISSION)
    assert json.loads(rows(db_path)[1]["checks"]) == []
