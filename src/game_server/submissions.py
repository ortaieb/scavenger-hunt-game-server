"""SQLite record of challenge submissions and of the teams that joined, scoped by session."""

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends

from game_server.checks import CheckResult, Rejection
from game_server.checks import rejections as rejections_of
from game_server.checks.base import AcceptedPhoto
from game_server.config import Settings, get_settings
from game_server.models import VerdictStatus
from game_server.phash import from_hex, to_hex
from game_server.referee import RefereeReport

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
    # 2 (#11): perceptual hash of the photo, 16 hex digits (SQLite integers are signed).
    "ALTER TABLE submissions ADD COLUMN phash TEXT",
    # 3 (#11): on a duplicate_photo rejection, the accepted submission it matched.
    "ALTER TABLE submissions ADD COLUMN phash_match_id INTEGER",
    # 4 (#19): every check that ran, as JSON {check, outcome, confidence, reason, detail}.
    "ALTER TABLE submissions ADD COLUMN checks TEXT",
    # 5-11 (#22): the referee's report, for moderator audit and cost tracking. NULL when the
    # referee wasn't consulted. referee_judgement holds the model's verdicts, confidences and
    # reasons (they describe the photo): server-side only, deleted with the session.
    "ALTER TABLE submissions ADD COLUMN referee_status TEXT",
    "ALTER TABLE submissions ADD COLUMN referee_model TEXT",
    "ALTER TABLE submissions ADD COLUMN referee_error TEXT",
    "ALTER TABLE submissions ADD COLUMN referee_judgement TEXT",
    "ALTER TABLE submissions ADD COLUMN referee_input_tokens INTEGER",
    "ALTER TABLE submissions ADD COLUMN referee_output_tokens INTEGER",
    "ALTER TABLE submissions ADD COLUMN referee_latency_ms INTEGER",
    # 12 (#37): one participant per team that has joined, keyed by a server-generated id.
    # consented_at is updated on every join (the player ticked the consent box again).
    """CREATE TABLE participants (
        id           TEXT PRIMARY KEY,
        session      TEXT NOT NULL,
        team         TEXT NOT NULL,
        joined_at    TEXT NOT NULL,
        consented_at TEXT NOT NULL,
        UNIQUE (session, team)
    )""",
    # 13-14 (#39): a team's check-ins at a checkpoint, each with a one-time code. The first
    # arrival at a checkpoint is the team's check-in time (for scoring by order of arrival).
    """CREATE TABLE arrivals (
        id          INTEGER PRIMARY KEY,
        session     TEXT    NOT NULL,
        participant TEXT    NOT NULL,
        checkpoint  INTEGER NOT NULL,
        code        TEXT    NOT NULL,
        pose        TEXT,
        issued_at   TEXT    NOT NULL,
        expires_at  TEXT    NOT NULL
    )""",
    "CREATE INDEX arrivals_by_team ON arrivals (session, participant, checkpoint)",
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
    checks: Sequence[CheckResult]
    distance_m: float
    phash: int
    phash_match_id: int | None = None
    referee: RefereeReport | None = None

    @property
    def rejections(self) -> list[Rejection]:
        """The failed checks' rejections, in check order."""
        return rejections_of(self.checks)


@dataclass(frozen=True)
class JoinOutcome:
    """The team's participant id, and whether this join created it."""

    participant: UUID
    first: bool


@dataclass(frozen=True)
class ParticipantRecord:
    """A joined team's row."""

    id: UUID
    session: UUID
    team: str
    joined_at: datetime
    consented_at: datetime


@dataclass(frozen=True)
class Arrival:
    """A check-in at a checkpoint, with the one-time code to hold up in the photo."""

    checkpoint: int
    code: str
    pose: str | None
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ArrivalOutcome:
    """The team's active arrival, and whether this call created it."""

    arrival: Arrival
    new: bool


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

    @contextmanager
    def transaction(self) -> Iterator["SubmissionTransaction"]:
        """Open a write transaction: commit on success, roll back on any exception.

        `BEGIN IMMEDIATE` takes the database write lock up front, so everything read and
        written inside is serialised against other submissions: concurrent submissions
        can't share an attempt number, and two uploads of one photo can't both be accepted.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield SubmissionTransaction(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def ping(self) -> None:
        """Check the database answers and has its schema; raises `sqlite3.Error` if not."""
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM submissions LIMIT 1").fetchall()

    def join_team(self, session: UUID, team: str, now: datetime) -> JoinOutcome:
        """Create the team's participant, or return it and record the renewed consent.

        One `BEGIN IMMEDIATE` transaction, so two phones joining at once share one id.
        """
        with self.transaction() as transaction:
            return transaction.join_team(session, team, now)

    def arrive(
        self,
        session: UUID,
        participant: UUID,
        checkpoint: int,
        *,
        pose: str | None,
        now: datetime,
        ttl: timedelta,
        new_code: Callable[[], str],
    ) -> ArrivalOutcome:
        """Return the team's active arrival at the checkpoint, or issue a fresh one.

        An arrival is active until it expires or the team submits a photo for that
        checkpoint after it was issued. One `BEGIN IMMEDIATE` transaction, so two taps at
        once can't issue two codes.
        """
        with self.transaction() as transaction:
            return transaction.arrive(
                session, participant, checkpoint, pose=pose, now=now, ttl=ttl, new_code=new_code
            )

    def completed_checkpoints(self, session: UUID, participant: UUID) -> frozenset[int]:
        """Checkpoints the participant has an accepted submission for (`pass` or `pending`).

        "Accepted" as the duplicate-photo check defines it. A `pending` verdict completes a
        checkpoint: the moderator's review changes the team's score, not its progress.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT checkpoint FROM submissions"
                " WHERE session = ? AND participant = ? AND verdict IN ('pass', 'pending')",
                (str(session), str(participant)),
            ).fetchall()
        return frozenset(checkpoint for (checkpoint,) in rows)

    def find_participant(self, session: UUID, participant: UUID) -> ParticipantRecord | None:
        """The participant's row, if it joined this session."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, session, team, joined_at, consented_at FROM participants"
                " WHERE id = ? AND session = ?",
                (str(participant), str(session)),
            ).fetchone()
        if row is None:
            return None
        return ParticipantRecord(
            id=UUID(row[0]),
            session=UUID(row[1]),
            team=row[2],
            joined_at=datetime.fromisoformat(row[3]),
            consented_at=datetime.fromisoformat(row[4]),
        )

    def record(self, submission: NewSubmission) -> RecordedSubmission:
        """Record `submission` in a transaction of its own."""
        with self.transaction() as transaction:
            return transaction.record(submission)


class SubmissionTransaction:
    """Reads and writes inside one `SubmissionStore.transaction()`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def accepted_photos(self, session: UUID) -> tuple[AcceptedPhoto, ...]:
        """Photos of this session's submissions whose verdict is not `failed`."""
        rows = self._conn.execute(
            "SELECT id, phash FROM submissions"
            " WHERE session = ? AND verdict != 'failed' AND phash IS NOT NULL ORDER BY id",
            (str(session),),
        ).fetchall()
        return tuple(AcceptedPhoto(submission_id=id_, phash=from_hex(hex_)) for id_, hex_ in rows)

    def arrive(
        self,
        session: UUID,
        participant: UUID,
        checkpoint: int,
        *,
        pose: str | None,
        now: datetime,
        ttl: timedelta,
        new_code: Callable[[], str],
    ) -> ArrivalOutcome:
        """See `SubmissionStore.arrive`."""
        key = (str(session), str(participant), checkpoint)
        active = self._active_arrival(key, now)
        if active is not None:
            return ArrivalOutcome(active, new=False)
        arrival = Arrival(checkpoint, new_code(), pose, issued_at=now, expires_at=now + ttl)
        self._conn.execute(
            "INSERT INTO arrivals (session, participant, checkpoint, code, pose, issued_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*key, arrival.code, pose, now.isoformat(), arrival.expires_at.isoformat()),
        )
        return ArrivalOutcome(arrival, new=True)

    def _active_arrival(self, key: tuple[str, str, int], now: datetime) -> Arrival | None:
        """The latest arrival, if unexpired and not yet followed by a submission there.

        Times are compared as datetimes, not as strings: ISO strings with and without
        microseconds don't sort correctly as text.
        """
        row = self._conn.execute(
            "SELECT checkpoint, code, pose, issued_at, expires_at FROM arrivals"
            " WHERE session = ? AND participant = ? AND checkpoint = ? ORDER BY id DESC LIMIT 1",
            key,
        ).fetchone()
        if row is None:
            return None
        arrival = Arrival(
            row[0], row[1], row[2], datetime.fromisoformat(row[3]), datetime.fromisoformat(row[4])
        )
        if now >= arrival.expires_at:
            return None
        submitted = self._conn.execute(
            "SELECT received_at FROM submissions"
            " WHERE session = ? AND participant = ? AND checkpoint = ?",
            key,
        ).fetchall()
        if any(datetime.fromisoformat(at) >= arrival.issued_at for (at,) in submitted):
            return None
        return arrival

    def join_team(self, session: UUID, team: str, now: datetime) -> JoinOutcome:
        """See `SubmissionStore.join_team`."""
        existing = self._conn.execute(
            "SELECT id FROM participants WHERE session = ? AND team = ?", (str(session), team)
        ).fetchone()
        if existing is not None:
            self._conn.execute(
                "UPDATE participants SET consented_at = ? WHERE id = ?",
                (now.isoformat(), existing[0]),
            )
            return JoinOutcome(participant=UUID(existing[0]), first=False)
        participant = uuid4()
        self._conn.execute(
            "INSERT INTO participants (id, session, team, joined_at, consented_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(participant), str(session), team, now.isoformat(), now.isoformat()),
        )
        return JoinOutcome(participant=participant, first=True)

    def record(self, submission: NewSubmission) -> RecordedSubmission:
        """Insert `submission` as the next attempt for its (session, participant, checkpoint)."""
        key = (str(submission.session), str(submission.participant), submission.checkpoint)
        (earlier,) = self._conn.execute(
            "SELECT COUNT(*) FROM submissions"
            " WHERE session = ? AND participant = ? AND checkpoint = ?",
            key,
        ).fetchone()
        attempt = int(earlier) + 1
        cursor = self._conn.execute(
            "INSERT INTO submissions (session, participant, checkpoint, attempt,"
            " received_at, capture_time, lat, long, image_id, verdict, rejections,"
            " distance_m, phash, phash_match_id, checks, referee_status, referee_model,"
            " referee_error, referee_judgement, referee_input_tokens, referee_output_tokens,"
            " referee_latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                to_hex(submission.phash),
                submission.phash_match_id,
                json.dumps([_check_record(result) for result in submission.checks]),
                *_referee_columns(submission.referee),
            ),
        )
        if cursor.lastrowid is None:  # pragma: no cover - sqlite3 always sets it after INSERT
            raise RuntimeError("sqlite3 did not report the inserted row id")
        return RecordedSubmission(id=cursor.lastrowid, attempt=attempt)


def _check_record(result: CheckResult) -> dict[str, object]:
    """A check result as stored: everything but the rejection, including `detail`."""
    return {
        "check": result.check,
        "outcome": result.outcome,
        "confidence": result.confidence,
        "reason": result.reason,
        "detail": result.detail,
    }


def _referee_columns(report: RefereeReport | None) -> tuple[object, ...]:
    """Values for the referee_* columns, in order; all NULL when it wasn't consulted."""
    if report is None:
        return (None,) * 7
    judgement = report.judgement.model_dump_json() if report.judgement else None
    return (
        report.status,
        report.model,
        report.error_code,
        judgement,
        report.input_tokens,
        report.output_tokens,
        report.latency_ms,
    )


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
