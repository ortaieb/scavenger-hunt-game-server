"""Record of challenge submissions and of the teams that joined, scoped by session.

Stored in the external PostgreSQL database; the tables are defined in `schema.sql`.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends
from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from game_server.arrivals import Arrival, LatestArrival
from game_server.checks import CheckResult, Rejection
from game_server.checks import rejections as rejections_of
from game_server.checks.base import AcceptedPhoto
from game_server.database import Database, get_database
from game_server.models import VerdictStatus
from game_server.phash import from_hex, to_hex
from game_server.referee import RefereeReport
from game_server.scoring import SessionResults
from game_server.session_runs import RunChange, SessionRun, session_phase

# How long the health check waits for a connection before reporting the database unavailable.
PING_TIMEOUT_SECONDS = 5.0


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
    # The active arrival the photo used; None when there was none.
    arrival_id: int | None = None

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
class ArrivalOutcome:
    """The team's active arrival, and whether this call created it."""

    arrival: Arrival
    new: bool


BlockedAction = Literal["join", "arrive", "photo"]
PhaseCode = Literal["session_not_started", "session_stopped"]
# Blocked attempts kept per session; older ones are deleted as new ones are recorded.
BLOCKED_KEPT = 500


@dataclass(frozen=True)
class BlockedAttempt:
    """A team trying to play outside the session: refused, or its photo didn't count."""

    at: datetime
    team: str
    action: BlockedAction
    code: PhaseCode


@dataclass(frozen=True)
class AcceptedSubmission:
    """A joined team's accepted photo (`pass` or `pending`): which checkpoint, and when."""

    team: str
    checkpoint: int
    verdict: VerdictStatus
    received_at: datetime


@dataclass(frozen=True)
class RecordedSubmission:
    """Identifiers the store assigned to a recorded submission."""

    id: int
    attempt: int


class SubmissionStore:
    """Appends submissions to the database, numbering attempts per checkpoint."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @contextmanager
    def transaction(self, session: UUID) -> Iterator["SubmissionTransaction"]:
        """Open a write transaction for `session`: commit on success, roll back on any exception.

        It first takes the session's advisory lock, held until the transaction ends, so
        everything read and written inside is serialised against the session's other writes:
        concurrent submissions can't share an attempt number, two uploads of one photo can't
        both be accepted, and two phones joining at once share one participant. Every rule it
        protects is scoped to a session, so other sessions' writes aren't held up.
        """
        with self._database.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(session),))
            yield SubmissionTransaction(conn)

    def ping(self) -> None:
        """Check the database answers and has its schema; raises `psycopg.Error` if not."""
        with self._database.connection(timeout=PING_TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1 FROM submissions LIMIT 1").fetchall()

    def session_run(self, session: UUID) -> SessionRun | None:
        """The session's run, or None if it was never started."""
        with self._database.connection() as conn:
            return _session_run(conn, session)

    def start_run(self, session: UUID, now: datetime) -> RunChange:
        """Start a scheduled session; leave a running or stopped one as it is.

        Session-locked, so two moderators starting at once stamp one `started_at`.
        """
        with self.transaction(session) as transaction:
            return transaction.start_run(session, now)

    def stop_run(self, session: UUID, now: datetime) -> RunChange:
        """Stop a running session; leave a scheduled or stopped one as it is."""
        with self.transaction(session) as transaction:
            return transaction.stop_run(session, now)

    def join_team(self, session: UUID, team: str, now: datetime) -> JoinOutcome:
        """Create the team's participant, or return it and record the renewed consent.

        One session-locked transaction, so two phones joining at once share one id.
        """
        with self.transaction(session) as transaction:
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

        An arrival is active until it expires or a photo uses it (see `LatestArrival`).
        One session-locked transaction, so two taps at once can't issue two codes.
        """
        with self.transaction(session) as transaction:
            return transaction.arrive(
                session, participant, checkpoint, pose=pose, now=now, ttl=ttl, new_code=new_code
            )

    def latest_arrival(
        self, session: UUID, participant: UUID, checkpoint: int, at: datetime
    ) -> LatestArrival | None:
        """The team's latest arrival at the checkpoint issued by `at`, if it ever arrived."""
        with self._database.connection() as conn:
            return _latest_arrival(conn, (session, participant, checkpoint), at)

    def completed_checkpoints(self, session: UUID, participant: UUID) -> frozenset[int]:
        """Checkpoints the participant has an accepted submission for (`pass` or `pending`).

        "Accepted" as the duplicate-photo check defines it. A `pending` verdict completes a
        checkpoint: the moderator's review changes the team's score, not its progress.
        """
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT checkpoint FROM submissions"
                " WHERE session = %s AND participant = %s AND verdict IN ('pass', 'pending')",
                (session, participant),
            ).fetchall()
        return frozenset(checkpoint for (checkpoint,) in rows)

    def session_results(self, session: UUID) -> SessionResults:
        """The teams that joined the session, with their first `pass` and any `pending` photo
        per checkpoint. One statement, so a consistent snapshot."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT p.team, s.checkpoint, s.verdict, MIN(s.received_at)"
                " FROM participants p LEFT JOIN submissions s"
                " ON s.session = p.session AND s.participant = p.id"
                " AND s.verdict IN ('pass', 'pending')"
                " WHERE p.session = %s"
                " GROUP BY p.team, s.checkpoint, s.verdict",
                (session,),
            ).fetchall()
        return SessionResults(
            joined=frozenset(team for team, *_ in rows),
            passes={
                (team, checkpoint): at
                for team, checkpoint, verdict, at in rows
                if verdict == "pass"
            },
            pending=frozenset(
                (team, checkpoint) for team, checkpoint, verdict, _ in rows if verdict == "pending"
            ),
        )

    def accepted_submissions(self, session: UUID) -> list[AcceptedSubmission]:
        """Every accepted photo (`pass` or `pending`) of the teams that joined the session."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT p.team, s.checkpoint, s.verdict, s.received_at"
                " FROM submissions s JOIN participants p"
                " ON s.session = p.session AND s.participant = p.id"
                " WHERE s.session = %s AND s.verdict IN ('pass', 'pending')"
                " ORDER BY s.received_at, s.id",
                (session,),
            ).fetchall()
        return [AcceptedSubmission(*row) for row in rows]

    def record_blocked(
        self, session: UUID, team: str, action: BlockedAction, code: PhaseCode, at: datetime
    ) -> None:
        """Record a team's join or arrive refused for a phase reason."""
        with self.transaction(session) as transaction:
            transaction.record_blocked(session, team, action, code, at)

    def blocked_attempts(self, session: UUID, limit: int) -> list[BlockedAttempt]:
        """The session's newest blocked attempts, newest first."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT at, team, action, code FROM blocked_attempts WHERE session = %s"
                " ORDER BY at DESC, id DESC LIMIT %s",
                (session, limit),
            ).fetchall()
        return [BlockedAttempt(*row) for row in rows]

    def find_participant(self, session: UUID, participant: UUID) -> ParticipantRecord | None:
        """The participant's row, if it joined this session."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id, session, team, joined_at, consented_at FROM participants"
                " WHERE id = %s AND session = %s",
                (participant, session),
            ).fetchone()
        if row is None:
            return None
        id_, session_, team, joined_at, consented_at = row
        return ParticipantRecord(id_, session_, team, joined_at, consented_at)

    def record(self, submission: NewSubmission) -> RecordedSubmission:
        """Record `submission` in a transaction of its own."""
        with self.transaction(submission.session) as transaction:
            return transaction.record(submission)


class SubmissionTransaction:
    """Reads and writes inside one `SubmissionStore.transaction()`."""

    def __init__(self, conn: Connection[TupleRow]) -> None:
        self._conn = conn

    def accepted_photos(self, session: UUID) -> tuple[AcceptedPhoto, ...]:
        """Photos of this session's submissions whose verdict is not `failed`."""
        rows = self._conn.execute(
            "SELECT id, phash FROM submissions WHERE session = %s AND verdict != 'failed'"
            " ORDER BY id",
            (session,),
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
        key = (session, participant, checkpoint)
        latest = _latest_arrival(self._conn, key, now)
        if latest is not None and latest.active_at(now):
            return ArrivalOutcome(latest.arrival, new=False)
        code, expires_at = new_code(), now + ttl
        inserted = self._conn.execute(
            "INSERT INTO arrivals (session, participant, checkpoint, code, pose, issued_at,"
            " expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (*key, code, pose, now, expires_at),
        ).fetchone()
        if inserted is None:  # pragma: no cover - INSERT ... RETURNING always returns the row
            raise RuntimeError("the database did not return the inserted row id")
        arrival = Arrival(inserted[0], checkpoint, code, pose, issued_at=now, expires_at=expires_at)
        return ArrivalOutcome(arrival, new=True)

    def latest_arrival(
        self, session: UUID, participant: UUID, checkpoint: int, at: datetime
    ) -> LatestArrival | None:
        """See `SubmissionStore.latest_arrival`: read under the lock, so it can't go stale."""
        return _latest_arrival(self._conn, (session, participant, checkpoint), at)

    def start_run(self, session: UUID, now: datetime) -> RunChange:
        """See `SubmissionStore.start_run`."""
        run = _session_run(self._conn, session)
        if session_phase(run) != "scheduled":
            return RunChange(run, changed=False)
        self._conn.execute(
            "INSERT INTO session_runs (session, started_at) VALUES (%s, %s)"
            " ON CONFLICT (session) DO UPDATE SET started_at = EXCLUDED.started_at",
            (session, now),
        )
        return RunChange(SessionRun(started_at=now, stopped_at=None), changed=True)

    def stop_run(self, session: UUID, now: datetime) -> RunChange:
        """See `SubmissionStore.stop_run`."""
        run = _session_run(self._conn, session)
        if run is None or session_phase(run) != "running":
            return RunChange(run, changed=False)
        self._conn.execute(
            "UPDATE session_runs SET stopped_at = %s WHERE session = %s", (now, session)
        )
        return RunChange(SessionRun(started_at=run.started_at, stopped_at=now), changed=True)

    def join_team(self, session: UUID, team: str, now: datetime) -> JoinOutcome:
        """See `SubmissionStore.join_team`."""
        existing = self._conn.execute(
            "UPDATE participants SET consented_at = %s WHERE session = %s AND team = %s"
            " RETURNING id",
            (now, session, team),
        ).fetchone()
        if existing is not None:
            return JoinOutcome(participant=existing[0], first=False)
        participant = uuid4()
        self._conn.execute(
            "INSERT INTO participants (id, session, team, joined_at, consented_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (participant, session, team, now, now),
        )
        return JoinOutcome(participant=participant, first=True)

    def record_blocked(
        self, session: UUID, team: str, action: BlockedAction, code: PhaseCode, at: datetime
    ) -> None:
        """See `SubmissionStore.record_blocked`."""
        self._conn.execute(
            "INSERT INTO blocked_attempts (session, team, action, code, at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (session, team, action, code, at),
        )
        self._trim_blocked(session)

    def record_blocked_photo(
        self, session: UUID, participant: UUID, code: PhaseCode, at: datetime
    ) -> None:
        """Record a photo that didn't count for a phase reason, under the participant's team.

        Nothing is recorded for a participant that never joined: there's no team to show.
        """
        inserted = self._conn.execute(
            "INSERT INTO blocked_attempts (session, team, action, code, at)"
            " SELECT session, team, 'photo', %s, %s FROM participants"
            " WHERE id = %s AND session = %s",
            (code, at, participant, session),
        )
        if inserted.rowcount:
            self._trim_blocked(session)

    def _trim_blocked(self, session: UUID) -> None:
        """Keep only the session's newest `BLOCKED_KEPT` blocked attempts."""
        self._conn.execute(
            "DELETE FROM blocked_attempts WHERE session = %s AND id NOT IN ("
            " SELECT id FROM blocked_attempts WHERE session = %s"
            " ORDER BY at DESC, id DESC LIMIT %s)",
            (session, session, BLOCKED_KEPT),
        )

    def record(self, submission: NewSubmission) -> RecordedSubmission:
        """Insert `submission` as the next attempt for its (session, participant, checkpoint)."""
        key = (submission.session, submission.participant, submission.checkpoint)
        (earlier,) = self._conn.execute(
            "SELECT COUNT(*) FROM submissions"
            " WHERE session = %s AND participant = %s AND checkpoint = %s",
            key,
        ).fetchone() or (0,)
        attempt = int(earlier) + 1
        inserted = self._conn.execute(
            "INSERT INTO submissions (session, participant, checkpoint, attempt,"
            " received_at, capture_time, lat, long, image_id, verdict, rejections,"
            " distance_m, phash, phash_match_id, checks, referee_status, referee_model,"
            " referee_error, referee_judgement, referee_input_tokens, referee_output_tokens,"
            " referee_latency_ms, arrival_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s, %s)"
            " RETURNING id",
            (
                *key,
                attempt,
                submission.received_at,
                submission.capture_time,
                submission.lat,
                submission.long,
                submission.image_id,
                submission.verdict,
                Jsonb([asdict(rejection) for rejection in submission.rejections]),
                submission.distance_m,
                to_hex(submission.phash),
                submission.phash_match_id,
                Jsonb([_check_record(result) for result in submission.checks]),
                *_referee_columns(submission.referee),
                submission.arrival_id,
            ),
        ).fetchone()
        if inserted is None:  # pragma: no cover - INSERT ... RETURNING always returns the row
            raise RuntimeError("the database did not return the inserted row id")
        return RecordedSubmission(id=inserted[0], attempt=attempt)


def _session_run(conn: Connection[TupleRow], session: UUID) -> SessionRun | None:
    row = conn.execute(
        "SELECT started_at, stopped_at FROM session_runs WHERE session = %s", (session,)
    ).fetchone()
    return None if row is None else SessionRun(started_at=row[0], stopped_at=row[1])


def _latest_arrival(
    conn: Connection[TupleRow], key: tuple[UUID, UUID, int], at: datetime
) -> LatestArrival | None:
    """The latest arrival for (session, participant, checkpoint) issued by `at`.

    Used if a photo recorded it, or the team sent a photo there after it was issued.
    """
    row = conn.execute(
        "SELECT id, checkpoint, code, pose, issued_at, expires_at,"
        " EXISTS (SELECT 1 FROM submissions AS submission"
        "   WHERE submission.arrival_id = arrival.id"
        "   OR (submission.session = arrival.session"
        "   AND submission.participant = arrival.participant"
        "   AND submission.checkpoint = arrival.checkpoint"
        "   AND submission.received_at > arrival.issued_at))"
        " FROM arrivals AS arrival"
        " WHERE session = %s AND participant = %s AND checkpoint = %s AND issued_at <= %s"
        " ORDER BY id DESC LIMIT 1",
        (*key, at),
    ).fetchone()
    if row is None:
        return None
    *fields, used = row
    return LatestArrival(Arrival(*fields), used=used)


def _lock_key(session: UUID) -> int:
    """The session's advisory lock key: its UUID's first 64 bits, as a signed BIGINT.

    Two sessions sharing a key would only serialise each other's writes.
    """
    return int.from_bytes(session.bytes[:8], "big", signed=True)


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
    judgement = Jsonb(report.judgement.model_dump(mode="json")) if report.judgement else None
    return (
        report.status,
        report.model,
        report.error_code,
        judgement,
        report.input_tokens,
        report.output_tokens,
        report.latency_ms,
    )


def get_submission_store(database: Annotated[Database, Depends(get_database)]) -> SubmissionStore:
    """Dependency providing the submission store on the configured database's pool."""
    return SubmissionStore(database)
