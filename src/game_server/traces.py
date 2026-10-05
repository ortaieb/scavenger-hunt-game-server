"""`GET /sessions/{session}/traces`: every verdict in a session, with its referee trace.

For the moderator to see why a photo got its verdict, compare the model's reasons with what
the player was told, and watch what the session costs and how long players wait. Moderator
only: it shows the scenes (the answers to the clues), the model's reasons and the checks'
`detail`. It never shows coordinates, codes, participant ids or the photos themselves.
"""

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, JsonValue

from game_server.clock import utc_iso
from game_server.models import CheckOutcome, VerdictStatus
from game_server.moderation import require_moderator
from game_server.referee import RefereeErrorCode
from game_server.referee_traces import JudgedSubmission, Trace, TraceStatus, TraceSummary
from game_server.sessions import GameSession
from game_server.submissions import SubmissionStore, get_submission_store

router = APIRouter()

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_SUBMISSION_ID = 2**63 - 1  # BIGINT


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


class ProcessingOut(BaseModel):
    """Milliseconds from receipt to verdict (nearest-rank); null without submissions."""

    p50: int | None
    p95: int | None
    max: int | None


class SummaryOut(_KebabModel):
    """The whole session, whichever page this is."""

    submissions: int
    verdicts: dict[VerdictStatus, int]
    referee_calls: int
    referee_errors: int
    cost_usd: str
    processing_ms: ProcessingOut


class StoredCheckOut(_KebabModel):
    """A check that ran: what the player was told (`reason`), and why (`detail`)."""

    check: str
    outcome: CheckOutcome
    confidence: float
    reason: str
    detail: str | None


class TraceOut(_KebabModel):
    """The referee call behind a verdict, as recorded. Costs are decimal strings."""

    model: str
    status: TraceStatus
    error_code: RefereeErrorCode | None
    stop_reason: str | None
    request_id: str | None
    prompt_sha256: str
    user_text: str
    references: list[JsonValue]
    judgement: JsonValue
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: str | None
    latency_ms: int


class SubmissionTraceOut(_KebabModel):
    """A submission: its verdict, every check that ran, and the referee's trace if called."""

    submission: int
    team: str | None
    checkpoint: int
    attempt: int
    received_at: str
    verdict: VerdictStatus
    processing_ms: int
    image_id: UUID
    checks: list[StoredCheckOut]
    trace: TraceOut | None


class TracesOut(_KebabModel):
    """The session's summary, the text of the page's prompts, and the page, newest first."""

    summary: SummaryOut
    prompts: dict[str, str]
    items: list[SubmissionTraceOut]
    next: int | None


def _decimal(value: Decimal) -> str:
    """Exactly as stored, never in exponent notation."""
    return f"{value:f}"


def summary_out(summary: TraceSummary) -> SummaryOut:
    """The summary as returned."""
    return SummaryOut(
        submissions=summary.submissions,
        verdicts=summary.verdicts,
        referee_calls=summary.referee_calls,
        referee_errors=summary.referee_errors,
        cost_usd=_decimal(summary.cost_usd),
        processing_ms=ProcessingOut(
            p50=summary.processing_p50_ms,
            p95=summary.processing_p95_ms,
            max=summary.processing_max_ms,
        ),
    )


def trace_out(trace: Trace) -> TraceOut:
    """A trace as returned: everything recorded but the raw response and the image's hash."""
    return TraceOut(
        model=trace.model,
        status=trace.status,
        error_code=trace.error_code,
        stop_reason=trace.stop_reason,
        request_id=trace.request_id,
        prompt_sha256=trace.prompt_sha256,
        user_text=trace.user_text,
        references=trace.reference_photos,
        judgement=trace.judgement,
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        cost_usd=_decimal(trace.cost_usd) if trace.cost_usd is not None else None,
        latency_ms=trace.latency_ms,
    )


def item_out(item: JudgedSubmission) -> SubmissionTraceOut:
    """A submission as returned."""
    return SubmissionTraceOut(
        submission=item.id,
        team=item.team,
        checkpoint=item.checkpoint,
        attempt=item.attempt,
        received_at=utc_iso(item.received_at),
        verdict=item.verdict,
        processing_ms=item.processing_ms,
        image_id=item.image_id,
        checks=[
            StoredCheckOut(
                check=check.check,
                outcome=check.outcome,
                confidence=check.confidence,
                reason=check.reason,
                detail=check.detail,
            )
            for check in item.checks
        ],
        trace=trace_out(item.trace) if item.trace is not None else None,
    )


@router.get(
    "/sessions/{session}/traces",
    responses={
        401: {"description": "Moderator code required (code: moderator_unauthorised)"},
        404: {"description": "Unknown session"},
    },
)
def session_traces(
    session: Annotated[GameSession, Depends(require_moderator)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    before: Annotated[
        int | None,
        Query(ge=1, le=MAX_SUBMISSION_ID, description="A submission id: the page starts below it"),
    ] = None,
) -> TracesOut:
    """Every submission in the session, newest first, with its referee trace and the spend.

    Page with `before`: pass the previous page's `next` until it's null.
    """
    page = store.traces(session.id, limit, before)
    return TracesOut(
        summary=summary_out(page.summary),
        prompts=page.prompts,
        items=[item_out(item) for item in page.items],
        next=page.next_before,
    )
