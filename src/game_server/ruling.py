"""`POST /sessions/{session}/submissions/{submission}/ruling`: the moderator rules on a photo.

Approve or reject any photo in the session: a `pending` one the referee couldn't decide, or a
`pass` or `failed` the referee got wrong. The ruling is recorded beside the referee's verdict,
which is never changed, and scoring follows the effective verdict (see `rulings`). Moderator
only. The note may describe the photo, so it's never logged.
"""

import logging
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field

from game_server.clock import Clock, get_clock, utc_iso
from game_server.models import VerdictStatus
from game_server.moderation import require_moderator
from game_server.rulings import RecordedRuling, Ruling, StoredRuling
from game_server.sessions import GameSession
from game_server.submissions import SubmissionStore, get_submission_store

logger = logging.getLogger(__name__)

router = APIRouter()

NOTE_MAX_LENGTH = 500
MAX_SUBMISSION_ID = 2**63 - 1  # BIGINT


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


class RulingIn(BaseModel):
    """Approve or reject the photo, with an optional note for the record."""

    model_config = ConfigDict(extra="forbid")

    ruling: Ruling
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class RulingOut(_KebabModel):
    """A ruling as recorded."""

    ruling: Ruling
    note: str | None
    ruled_at: str

    @classmethod
    def of(cls, stored: StoredRuling) -> "RulingOut":
        """The ruling as returned: `ruled-at` in UTC, to the second."""
        return cls(ruling=stored.ruling, note=stored.note, ruled_at=utc_iso(stored.ruled_at))


class SubmissionRulingOut(_KebabModel):
    """The referee's `verdict`, unchanged, the ruling, and the effective verdict scoring uses."""

    submission: int
    verdict: VerdictStatus
    ruling: RulingOut
    effective_verdict: VerdictStatus

    @classmethod
    def of(cls, recorded: RecordedRuling) -> "SubmissionRulingOut":
        """The recorded ruling as returned."""
        return cls(
            submission=recorded.submission,
            verdict=recorded.verdict,
            ruling=RulingOut.of(recorded.ruling),
            effective_verdict=recorded.effective_verdict,
        )


@router.post(
    "/sessions/{session}/submissions/{submission}/ruling",
    status_code=status.HTTP_200_OK,
    responses={
        201: {"model": SubmissionRulingOut, "description": "The submission's first ruling"},
        200: {"description": "Replaced the submission's earlier ruling"},
        401: {"description": "Moderator code required (code: moderator_unauthorised)"},
        404: {"description": "Unknown session, or a submission that isn't in it"},
    },
)
def rule_on_submission(
    session: Annotated[GameSession, Depends(require_moderator)],
    submission: Annotated[int, Path(ge=1, le=MAX_SUBMISSION_ID)],
    body: RulingIn,
    response: Response,
    clock: Annotated[Clock, Depends(get_clock)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> SubmissionRulingOut:
    """Approve or reject one of the session's photos, whatever its verdict. Moderator only.

    Allowed while the session runs and after it stops: the results aren't final until every
    `pending` photo is ruled on. Posting again replaces the ruling.
    """
    now = clock().astimezone(UTC)
    recorded = store.rule(session.id, submission, body.ruling, body.note, now)
    if recorded is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown submission")
    logger.info(
        "Moderator ruling session %s submission %d %s verdict %s -> %s first=%s",
        session.id,
        submission,
        body.ruling,
        recorded.verdict,
        recorded.effective_verdict,
        "yes" if recorded.first else "no",
    )
    if recorded.first:
        response.status_code = status.HTTP_201_CREATED
    return SubmissionRulingOut.of(recorded)
