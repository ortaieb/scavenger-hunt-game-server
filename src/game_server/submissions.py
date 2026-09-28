"""SQLite record of every challenge submission, scoped by game session."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends

from game_server.checks import Rejection
from game_server.config import Settings, get_settings
from game_server.models import VerdictStatus

# The table as first released (#8). Never edit it: later changes go in _MIGRATIONS.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id           INTEGER PRIMARY KEY,
    session      TEXT    NOT NULL,
    participant  TEXT    NOT NULL,
    checkpoint   INTEGER NOT NULL,
    attempt      INTEGER NOT NULL,
    received_at  TEXT    NOT NULL,
    capture_time TEXT    NOT NULL,
    lat          REAL    NOT NULL,
    long         REAL    NOT NULL,
    image_id     TEXT    NOT NULL,
    verdict      TEXT    NOT NULL CHECK (verdict IN ('failed', 'pending', 'pass')),
    rejections   TEXT    NOT NULL,
    UNIQUE (session, participant, checkpoint, attempt)
);
CREATE INDEX IF NOT EXISTS submissions_by_session ON submissions (session);
"""

# Applied in order; PRAGMA user_version records how many have run. Append only.
# Per-check audit columns are nullable: rows recorded before the column existed have none.
_MIGRATIONS: tuple[str, ...] = (
    # 1 (#10): geofence distance from the submitted location to the checkpoint, in metres.
    "ALTER TABLE submissions ADD COLUMN distance_m REAL",
)

_BUSY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class NewSubmission:
    """A submission to record; the store assigns its id and attempt number."""

    session: UUID
    participant: UUID
    checkpoint: int
    received_at: datetime
    capture_time: datetime
    lat: float
    long: float
    image_id: UUID
    verdict: VerdictStatus
    rejections: Sequence[Rejection]
    distance_m: float


@dataclass(frozen=True)
class RecordedSubmission:
    """Identifiers the store assigned to a recorded submission."""

    id: int
    attempt: int


class SubmissionStore:
    """Appends submissions to a SQLite database, numbering attempts per checkpoint."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            _migrate(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Autocommit mode: transactions are opened explicitly where needed.
        with closing(
            sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
        ) as conn:
            yield conn

    def record(self, submission: NewSubmission) -> RecordedSubmission:
        """Insert `submission` as the next attempt for its (session, participant, checkpoint).

        The count and insert run in one `BEGIN IMMEDIATE` transaction, which takes the
        database write lock up front, so concurrent submissions can't get the same attempt.
        """
        key = (str(submission.session), str(submission.participant), submission.checkpoint)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                (earlier,) = conn.execute(
                    "SELECT COUNT(*) FROM submissions"
                    " WHERE session = ? AND participant = ? AND checkpoint = ?",
                    key,
                ).fetchone()
                attempt = int(earlier) + 1
                cursor = conn.execute(
                    "INSERT INTO submissions (session, participant, checkpoint, attempt,"
                    " received_at, capture_time, lat, long, image_id, verdict, rejections,"
                    " distance_m)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *key,
                        attempt,
                        submission.received_at.isoformat(),
                        submission.capture_time.isoformat(),
                        submission.lat,
                        submission.long,
                        str(submission.image_id),
                        submission.verdict,
                        json.dumps([asdict(rejection) for rejection in submission.rejections]),
                        submission.distance_m,
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        if cursor.lastrowid is None:  # pragma: no cover - sqlite3 always sets it after INSERT
            raise RuntimeError("sqlite3 did not report the inserted row id")
        return RecordedSubmission(id=cursor.lastrowid, attempt=attempt)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply the migrations this database hasn't run yet, each in its own transaction."""
    (applied,) = conn.execute("PRAGMA user_version").fetchone()
    for version, statement in enumerate(_MIGRATIONS[applied:], start=applied + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")  # int from enumerate, not input
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@lru_cache
def open_submission_store(db_path: Path) -> SubmissionStore:
    """Open (creating the schema if needed) the store at `db_path`, once per path."""
    return SubmissionStore(db_path)


def get_submission_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SubmissionStore:
    """Dependency providing the submission store at the configured database path."""
    return open_submission_store(settings.db_path)
