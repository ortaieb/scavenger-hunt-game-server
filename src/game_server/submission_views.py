"""Submissions as the organiser lists and reads them: compact rows, filtered in the query.

Both read the `ruled_submissions` view (see `db/migrations/`), so what counts as passed, failed
or needing review is the view's `effective_verdict`, defined once. Server-side only: a row holds
the checks' `detail`, which describes the photo. Never the photo, its location or its
participant's id.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg import Connection, sql
from psycopg.rows import TupleRow

from game_server.models import VerdictStatus
from game_server.referee_traces import StoredCheck
from game_server.review_queue import RefereeOutcome
from game_server.rulings import Ruling, StoredRuling

SubmissionFilter = Literal["needs_review", "failed", "passed", "overridden", "all"]

# By effective verdict, except `overridden`: every photo the moderator has ruled on.
_FILTERS: dict[SubmissionFilter, str] = {
    "needs_review": "s.effective_verdict = 'pending'",
    "failed": "s.effective_verdict = 'failed'",
    "passed": "s.effective_verdict = 'pass'",
    "overridden": "s.ruling IS NOT NULL",
    "all": "TRUE",
}


@dataclass(frozen=True)
class ListedSubmission:
    """One row of the list: the referee's verdict, the latest ruling and the effective verdict."""

    id: int
    # None only if the participant's row is gone.
    team: str | None
    checkpoint: int
    attempt: int
    received_at: datetime
    verdict: VerdictStatus
    ruling: Ruling | None
    effective_verdict: VerdictStatus
    # The `check` of each check that failed.
    failed_checks: tuple[str, ...]


@dataclass(frozen=True)
class SubmissionPage:
    """A page of submissions, newest first."""

    items: tuple[ListedSubmission, ...]
    # Pass as `before` for the next page; None on the last page.
    next_before: int | None


@dataclass(frozen=True)
class SubmissionDetail:
    """A submission read in full, but for the photo: why the referee decided as it did, and
    every ruling on it."""

    id: int
    team: str | None
    checkpoint: int
    attempt: int
    received_at: datetime
    verdict: VerdictStatus
    effective_verdict: VerdictStatus
    # The pose issued at the check-in the photo used; None if none was issued.
    pose: str | None
    checks: tuple[StoredCheck, ...]
    # None when the referee wasn't called.
    referee: RefereeOutcome | None
    # Oldest first; the last one is the one in force.
    rulings: tuple[StoredRuling, ...]


def read_submission_list(
    conn: Connection[TupleRow],
    session: UUID,
    filter: SubmissionFilter,
    team: str | None,
    checkpoint: int | None,
    limit: int,
    before: int | None,
) -> SubmissionPage:
    """Up to `limit` of the session's submissions matching `filter`, newest (highest id) first,
    below the submission id `before`. `team` is matched ignoring case."""
    rows = conn.execute(
        sql.SQL(
            "SELECT s.id, p.team, s.checkpoint, s.attempt, s.received_at, s.verdict,"
            " s.ruling, s.effective_verdict, s.checks"
            " FROM ruled_submissions s LEFT JOIN participants p"
            " ON p.session = s.session AND p.id = s.participant"
            " WHERE s.session = %(session)s AND {filter}"
            " AND (%(team)s::TEXT IS NULL OR lower(p.team) = lower(%(team)s::TEXT))"
            " AND (%(checkpoint)s::INTEGER IS NULL OR s.checkpoint = %(checkpoint)s::INTEGER)"
            " AND (%(before)s::BIGINT IS NULL OR s.id < %(before)s::BIGINT)"
            " ORDER BY s.id DESC LIMIT %(limit)s"
        ).format(filter=sql.SQL(_FILTERS[filter])),
        {
            "session": session,
            "team": team,
            "checkpoint": checkpoint,
            "before": before,
            "limit": limit + 1,
        },
    ).fetchall()
    page = rows[:limit]
    items = tuple(_listed(row) for row in page)
    return SubmissionPage(items, items[-1].id if len(rows) > limit else None)


def _listed(row: TupleRow) -> ListedSubmission:
    id_, team, checkpoint, attempt, received_at, verdict, ruling, effective, checks = row
    return ListedSubmission(
        id=id_,
        team=team,
        checkpoint=checkpoint,
        attempt=attempt,
        received_at=received_at,
        verdict=verdict,
        ruling=ruling,
        effective_verdict=effective,
        failed_checks=tuple(c["check"] for c in checks if c["outcome"] == "failed"),
    )


def read_submission(
    conn: Connection[TupleRow], session: UUID, submission: int
) -> SubmissionDetail | None:
    """One of the session's submissions in full, or None if it isn't in the session.

    Run it in one snapshot, so the rulings agree with the effective verdict.
    """
    row = conn.execute(
        "SELECT s.id, p.team, s.checkpoint, s.attempt, s.received_at, s.verdict,"
        " s.effective_verdict, a.pose, s.checks, t.status, t.error_code"
        " FROM ruled_submissions s"
        " LEFT JOIN participants p ON p.session = s.session AND p.id = s.participant"
        " LEFT JOIN arrivals a ON a.id = s.arrival_id"
        " LEFT JOIN referee_traces t ON t.submission_id = s.id"
        " WHERE s.session = %s AND s.id = %s",
        (session, submission),
    ).fetchone()
    if row is None:
        return None
    id_, team, checkpoint, attempt, received_at, verdict, effective, pose, checks, status, code = (
        row
    )
    rulings = conn.execute(
        "SELECT ruling, note, ruled_at FROM rulings WHERE submission_id = %s ORDER BY id",
        (submission,),
    ).fetchall()
    return SubmissionDetail(
        id=id_,
        team=team,
        checkpoint=checkpoint,
        attempt=attempt,
        received_at=received_at,
        verdict=verdict,
        effective_verdict=effective,
        pose=pose,
        checks=tuple(StoredCheck(**check) for check in checks),
        referee=RefereeOutcome(status, code) if status is not None else None,
        rulings=tuple(StoredRuling(*ruling) for ruling in rulings),
    )
