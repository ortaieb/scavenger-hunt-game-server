"""The referee's traces: one row per call, written in its submission's transaction.

A trace keeps what the referee was sent and what came back, so a verdict stays explainable
after the prompt changes, and what the call cost. The tables are defined by the migrations in
`db/migrations/`.
Server-side only: a trace holds the scene (the answer to the clue) and the model's
description of the photo. Only the moderator reads them back (`read_traces`).
"""

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from psycopg import Connection, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from game_server.models import CheckOutcome, VerdictStatus
from game_server.referee import RefereeErrorCode, RefereeReport
from game_server.rulings import Ruling, StoredRuling

TraceStatus = Literal["ok", "error"]


@dataclass(frozen=True)
class StoredCheck:
    """A check result as the submission stores it, with the moderator-only `detail`."""

    check: str
    outcome: CheckOutcome
    confidence: float
    reason: str
    detail: str | None


@dataclass(frozen=True)
class Trace:
    """A recorded referee call. The fields are the `referee_traces` columns read back."""

    model: str
    status: TraceStatus
    error_code: RefereeErrorCode | None
    stop_reason: str | None
    request_id: str | None
    prompt_sha256: str
    user_text: str
    reference_photos: list[JsonValue]
    judgement: JsonValue
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    latency_ms: int


@dataclass(frozen=True)
class JudgedSubmission:
    """A submission's verdict and checks, the referee's trace if it made a call, and the
    moderator's latest ruling if there is one. `verdict` is always the referee's."""

    id: int
    # None only if the participant's row is gone.
    team: str | None
    checkpoint: int
    attempt: int
    received_at: datetime
    verdict: VerdictStatus
    processing_ms: int
    image_id: UUID
    checks: tuple[StoredCheck, ...]
    trace: Trace | None
    ruling: StoredRuling | None


@dataclass(frozen=True)
class TraceSummary:
    """The whole session: its verdicts, the referee's calls and spend, how long players waited.

    `verdicts` are the referee's; `rulings` count the submissions by their latest ruling. The
    `processing_ms` figures are nearest-rank, and None without submissions.
    """

    submissions: int
    verdicts: dict[VerdictStatus, int]
    rulings: dict[Ruling, int]
    referee_calls: int
    referee_errors: int
    cost_usd: Decimal
    processing_p50_ms: int | None
    processing_p95_ms: int | None
    processing_max_ms: int | None


@dataclass(frozen=True)
class TracePage:
    """The session's summary, a page of its submissions, and the prompts that page used."""

    summary: TraceSummary
    items: tuple[JudgedSubmission, ...]
    # The text of each `prompt_sha256` on the page.
    prompts: dict[str, str]
    # Pass as `before` for the next page; None on the last page.
    next_before: int | None


def record_trace(
    conn: Connection[TupleRow],
    submission_id: int,
    session: UUID,
    image_id: UUID,
    report: RefereeReport,
) -> None:
    """Record the call behind `report`, inside the caller's transaction.

    A disabled referee made no call, so its report has no trace. The system prompt is
    stored once, the first time it's used.
    """
    call = report.call
    if call is None:
        return
    _record_prompt(conn, call.prompt_sha256, call.system_prompt)
    image, judgement = call.image, report.judgement
    values: dict[str, object] = {
        "session": session,
        "submission_id": submission_id,
        "image_id": image_id,
        "image_sha256": image.sha256 if image else None,
        "image_width": image.width if image else None,
        "image_height": image.height if image else None,
        # By position and hash, never by path: a file name can describe the place.
        "reference_photos": Jsonb(
            [{"position": sent.position, "sha256": sent.sha256} for sent in call.references]
        ),
        "prompt_sha256": call.prompt_sha256,
        "user_text": call.user_text,
        "model": report.model,
        "request_id": report.request_id,
        "status": report.status,
        "error_code": report.error_code,
        "stop_reason": call.stop_reason,
        "response_text": call.response_text,
        "judgement": Jsonb(judgement.model_dump(mode="json")) if judgement else None,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "cost_usd": report.cost_usd,
        "latency_ms": report.latency_ms,
    }
    conn.execute(
        sql.SQL("INSERT INTO referee_traces (created_at, {}) VALUES (now(), {})").format(
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(map(sql.Placeholder, values)),
        ),
        values,
    )


def _record_prompt(conn: Connection[TupleRow], sha256: str, text: str) -> None:
    """Store the prompt's text, unless it's already there."""
    conn.execute(
        "INSERT INTO referee_prompts (sha256, text, first_used_at) VALUES (%s, %s, now())"
        " ON CONFLICT (sha256) DO NOTHING",
        (sha256, text),
    )


def read_traces(
    conn: Connection[TupleRow], session: UUID, limit: int, before: int | None
) -> TracePage:
    """A page of the session's submissions with their traces, and the whole session's summary.

    Up to `limit` submissions, newest (highest id) first, below the submission id `before`.
    Run it in one snapshot, so the summary and the page agree.
    """
    rows = _submission_rows(conn, session, limit + 1, before)
    page = rows[:limit]
    traces = _traces(conn, [row[0] for row in page])
    items = tuple(_judged(row, traces) for row in page)
    return TracePage(
        summary=_summary(conn, session),
        items=items,
        prompts=_prompts(conn, sorted({trace.prompt_sha256 for trace in traces.values()})),
        next_before=items[-1].id if len(rows) > limit else None,
    )


def _judged(row: TupleRow, traces: dict[int, Trace]) -> JudgedSubmission:
    """A row of `_submission_rows`, with its trace."""
    (
        id_,
        team,
        checkpoint,
        attempt,
        received_at,
        verdict,
        processing_ms,
        image_id,
        checks,
        ruling,
        note,
        ruled_at,
    ) = row
    return JudgedSubmission(
        id=id_,
        team=team,
        checkpoint=checkpoint,
        attempt=attempt,
        received_at=received_at,
        verdict=verdict,
        processing_ms=processing_ms,
        image_id=image_id,
        checks=tuple(StoredCheck(**check) for check in checks),
        trace=traces.get(id_),
        ruling=StoredRuling(ruling, note, ruled_at) if ruling is not None else None,
    )


def _submission_rows(
    conn: Connection[TupleRow], session: UUID, limit: int, before: int | None
) -> list[TupleRow]:
    """The submissions' columns `_judged` reads, with their latest ruling, newest first."""
    return conn.execute(
        "SELECT s.id, p.team, s.checkpoint, s.attempt, s.received_at, s.verdict,"
        " s.processing_ms, s.image_id, s.checks, s.ruling, s.note, s.ruled_at"
        " FROM ruled_submissions s LEFT JOIN participants p"
        " ON p.session = s.session AND p.id = s.participant"
        " WHERE s.session = %(session)s"
        " AND (%(before)s::BIGINT IS NULL OR s.id < %(before)s::BIGINT)"
        " ORDER BY s.id DESC LIMIT %(limit)s",
        {"session": session, "before": before, "limit": limit},
    ).fetchall()


def _traces(conn: Connection[TupleRow], submission_ids: list[int]) -> dict[int, Trace]:
    """The traces of these submissions, by submission id."""
    if not submission_ids:
        return {}
    columns = sql.SQL(", ").join(sql.Identifier(field.name) for field in fields(Trace))
    rows = conn.execute(
        sql.SQL(
            "SELECT submission_id, {} FROM referee_traces WHERE submission_id = ANY(%s)"
        ).format(columns),
        (submission_ids,),
    ).fetchall()
    return {submission_id: Trace(*values) for submission_id, *values in rows}


def _prompts(conn: Connection[TupleRow], hashes: list[str]) -> dict[str, str]:
    """The text of each prompt hash."""
    if not hashes:
        return {}
    rows = conn.execute(
        "SELECT sha256, text FROM referee_prompts WHERE sha256 = ANY(%s) ORDER BY sha256",
        (hashes,),
    ).fetchall()
    return {sha256: text for sha256, text in rows}


def _summary(conn: Connection[TupleRow], session: UUID) -> TraceSummary:
    """The whole session's counts, spend and processing times, in one statement."""
    row = conn.execute(
        "SELECT * FROM"
        " (SELECT COUNT(*), COUNT(*) FILTER (WHERE verdict = 'pass'),"
        "  COUNT(*) FILTER (WHERE verdict = 'pending'),"
        "  COUNT(*) FILTER (WHERE verdict = 'failed'),"
        "  COUNT(*) FILTER (WHERE ruling = 'approve'),"
        "  COUNT(*) FILTER (WHERE ruling = 'reject'),"
        "  percentile_disc(0.5) WITHIN GROUP (ORDER BY processing_ms),"
        "  percentile_disc(0.95) WITHIN GROUP (ORDER BY processing_ms),"
        "  MAX(processing_ms)"
        "  FROM ruled_submissions WHERE session = %(session)s) AS submitted,"
        " (SELECT COUNT(*), COUNT(*) FILTER (WHERE status = 'error'),"
        "  COALESCE(SUM(cost_usd), 0)"
        "  FROM referee_traces WHERE session = %(session)s) AS called",
        {"session": session},
    ).fetchone()
    if row is None:  # pragma: no cover - aggregates without GROUP BY always return a row
        raise RuntimeError("the database returned no summary row")
    (
        submissions,
        passed,
        pending,
        failed,
        approved,
        rejected,
        p50,
        p95,
        slowest,
        calls,
        errors,
        cost,
    ) = row
    return TraceSummary(
        submissions=submissions,
        verdicts={"pass": passed, "pending": pending, "failed": failed},
        rulings={"approve": approved, "reject": rejected},
        referee_calls=calls,
        referee_errors=errors,
        cost_usd=cost,
        processing_p50_ms=p50,
        processing_p95_ms=p95,
        processing_max_ms=slowest,
    )
