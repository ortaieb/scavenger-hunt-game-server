"""The moderator's review queue: the photos waiting for a ruling, and the latest rulings.

A photo waits for the moderator while its effective verdict is `pending`: the referee
couldn't decide, and nobody has ruled on it yet (see the `ruled_submissions` view in
`db/migrations/`). Server-side only: the checks' `detail` holds the referee's reasons, which
describe the photo. Only the moderator reads it back (`GET …/review`).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow

from game_server.models import VerdictStatus
from game_server.referee import RefereeErrorCode
from game_server.referee_traces import StoredCheck, TraceStatus
from game_server.rulings import StoredRuling


@dataclass(frozen=True)
class RefereeOutcome:
    """How the referee's call went: `error_code` says why it errored, leaving no reasons."""

    status: TraceStatus
    error_code: RefereeErrorCode | None


@dataclass(frozen=True)
class QueuedPhoto:
    """A photo waiting for a ruling, with what the referee was asked and what it said."""

    id: int
    # None only if the participant's row is gone.
    team: str | None
    checkpoint: int
    attempt: int
    received_at: datetime
    # The pose issued at the check-in the photo used; None if none was issued.
    pose: str | None
    checks: tuple[StoredCheck, ...]
    # None when the referee wasn't called (not consulted, or disabled).
    referee: RefereeOutcome | None


@dataclass(frozen=True)
class RecentRuling:
    """A ruled photo: its latest ruling, and the referee's own verdict."""

    id: int
    team: str | None
    checkpoint: int
    verdict: VerdictStatus
    ruling: StoredRuling


@dataclass(frozen=True)
class ReviewQueue:
    """The photos to review, oldest first, and the latest rulings, newest first."""

    to_review: tuple[QueuedPhoto, ...]
    recent: tuple[RecentRuling, ...]


def read_review_queue(conn: Connection[TupleRow], session: UUID, recent: int) -> ReviewQueue:
    """Every photo of the session waiting for a ruling, and its `recent` latest rulings.

    Run it in one snapshot, so a photo just ruled on is in one list, never both.
    """
    return ReviewQueue(
        to_review=tuple(_queued(row) for row in _waiting_rows(conn, session)),
        recent=tuple(_recent(row) for row in _ruled_rows(conn, session, recent)),
    )


def _waiting_rows(conn: Connection[TupleRow], session: UUID) -> list[TupleRow]:
    """The `pending` photos nobody has ruled on (as the overview counts them), oldest first."""
    return conn.execute(
        "SELECT s.id, p.team, s.checkpoint, s.attempt, s.received_at, a.pose, s.checks,"
        " t.status, t.error_code"
        " FROM ruled_submissions s"
        " LEFT JOIN participants p ON p.session = s.session AND p.id = s.participant"
        " LEFT JOIN arrivals a ON a.id = s.arrival_id"
        " LEFT JOIN referee_traces t ON t.submission_id = s.id"
        " WHERE s.session = %s AND s.effective_verdict = 'pending'"
        " ORDER BY s.received_at, s.id",
        (session,),
    ).fetchall()


def _queued(row: TupleRow) -> QueuedPhoto:
    """A row of `_waiting_rows`."""
    id_, team, checkpoint, attempt, received_at, pose, checks, status, error_code = row
    return QueuedPhoto(
        id=id_,
        team=team,
        checkpoint=checkpoint,
        attempt=attempt,
        received_at=received_at,
        pose=pose,
        checks=tuple(StoredCheck(**check) for check in checks),
        referee=RefereeOutcome(status, error_code) if status is not None else None,
    )


def _ruled_rows(conn: Connection[TupleRow], session: UUID, limit: int) -> list[TupleRow]:
    """The `limit` most recently ruled photos, each with its latest ruling, newest first."""
    return conn.execute(
        "SELECT s.id, p.team, s.checkpoint, s.verdict, s.ruling, s.note, s.ruled_at"
        " FROM ruled_submissions s"
        " LEFT JOIN participants p ON p.session = s.session AND p.id = s.participant"
        " WHERE s.session = %s AND s.ruling IS NOT NULL"
        " ORDER BY s.ruled_at DESC, s.id DESC LIMIT %s",
        (session, limit),
    ).fetchall()


def _recent(row: TupleRow) -> RecentRuling:
    """A row of `_ruled_rows`."""
    id_, team, checkpoint, verdict, ruling, note, ruled_at = row
    return RecentRuling(
        id=id_,
        team=team,
        checkpoint=checkpoint,
        verdict=verdict,
        ruling=StoredRuling(ruling, note, ruled_at),
    )
