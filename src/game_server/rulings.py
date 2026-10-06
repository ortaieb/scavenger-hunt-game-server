"""The moderator's rulings on photos: recorded beside the referee's verdict, never over it.

A ruling approves or rejects one submission. The latest ruling per submission wins; earlier
ones stay as the audit trail. What follows from a ruling (the effective verdict, and whether
the photo completes its checkpoint) is defined once, by the `ruled_submissions` view in
`schema.sql`, which scoring, progress, the duplicate check, the overview and the traces read.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow

from game_server.models import VerdictStatus

Ruling = Literal["approve", "reject"]


@dataclass(frozen=True)
class StoredRuling:
    """A submission's latest ruling. `note` is moderator-only and never logged."""

    ruling: Ruling
    note: str | None
    ruled_at: datetime


@dataclass(frozen=True)
class RecordedRuling:
    """A ruling just recorded, with the submission's original and effective verdicts."""

    submission: int
    verdict: VerdictStatus
    ruling: StoredRuling
    effective_verdict: VerdictStatus
    # The submission's first ruling, rather than one replacing an earlier ruling.
    first: bool


def record_ruling(
    conn: Connection[TupleRow],
    session: UUID,
    submission: int,
    ruling: Ruling,
    note: str | None,
    now: datetime,
) -> RecordedRuling | None:
    """Record a ruling on one of the session's submissions, inside the caller's transaction.

    None, and nothing recorded, when the session has no such submission. The effective
    verdict is read back from `ruled_submissions`, so the rule lives in one place.
    """
    earlier = conn.execute(
        "SELECT ruling IS NOT NULL FROM ruled_submissions WHERE id = %s AND session = %s",
        (submission, session),
    ).fetchone()
    if earlier is None:
        return None
    conn.execute(
        "INSERT INTO rulings (session, submission_id, ruling, note, ruled_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (session, submission, ruling, note, now),
    )
    row = conn.execute(
        "SELECT verdict, ruling, note, ruled_at, effective_verdict FROM ruled_submissions"
        " WHERE id = %s",
        (submission,),
    ).fetchone()
    if row is None:  # pragma: no cover - the submission was just read in this transaction
        raise RuntimeError("the database did not return the ruled submission")
    verdict, latest, latest_note, ruled_at, effective = row
    return RecordedRuling(
        submission=submission,
        verdict=verdict,
        ruling=StoredRuling(latest, latest_note, ruled_at),
        effective_verdict=effective,
        first=not earlier[0],
    )
