"""`GET /sessions/{session}/review`: the moderator's to-do list of photos to rule on.

Each photo waiting for a ruling, oldest first, with what it should show (the pose the team
was given, the scene, how many reference photos there are) and what the referee said about
each check. Then the latest rulings, so a decision can be changed. Moderator only: it shows
the scenes (the answers to the clues) and the referee's reasons. It never shows coordinates,
codes or participant ids; the photos themselves come from `photos`.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from game_server.clock import utc_iso
from game_server.models import VerdictStatus
from game_server.moderation import require_moderator
from game_server.referee import RefereeErrorCode
from game_server.referee_traces import TraceStatus
from game_server.review_queue import QueuedPhoto, RecentRuling
from game_server.rulings import Ruling
from game_server.sessions import GameSession, SessionRepository, get_session_repository
from game_server.submissions import SubmissionStore, get_submission_store
from game_server.traces import StoredCheckOut

router = APIRouter()

RECENT_SHOWN = 20


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


class CheckpointOut(_KebabModel):
    """The photo's checkpoint. `name` is null if it's no longer in the sessions file."""

    sequence: int
    name: str | None


class RefereeOut(_KebabModel):
    """How the referee's call went: with an `error-code` there are no reasons to read."""

    status: TraceStatus
    error_code: RefereeErrorCode | None


class QueuedPhotoOut(_KebabModel):
    """A photo to rule on, beside what it should show and what the referee said."""

    submission: int
    team: str | None
    checkpoint: CheckpointOut
    attempt: int
    received_at: str
    pose: str | None
    scene: str | None
    reference_photos: int
    checks: list[StoredCheckOut]
    referee: RefereeOut | None


class RecentRulingOut(_KebabModel):
    """A photo's latest ruling. `verdict` is the referee's own."""

    submission: int
    team: str | None
    checkpoint: CheckpointOut
    ruling: Ruling
    note: str | None
    ruled_at: str
    verdict: VerdictStatus


class ReviewOut(_KebabModel):
    """The photos to rule on, oldest first, and the latest rulings, newest first."""

    to_review: list[QueuedPhotoOut]
    recent: list[RecentRulingOut]


class _Checkpoints:
    """What the sessions file says about the session's checkpoints, by sequence."""

    def __init__(self, sessions: SessionRepository, session: UUID) -> None:
        self._sessions = sessions
        self._session = session

    def summary(self, sequence: int) -> CheckpointOut:
        checkpoint = self._sessions.get_checkpoint(self._session, sequence)
        return CheckpointOut(sequence=sequence, name=checkpoint.name if checkpoint else None)

    def scene(self, sequence: int) -> str | None:
        checkpoint = self._sessions.get_checkpoint(self._session, sequence)
        return checkpoint.challenge.scene if checkpoint and checkpoint.challenge else None

    def reference_photos(self, sequence: int) -> int:
        return len(self._sessions.reference_photos(self._session, sequence))


def queued_out(photo: QueuedPhoto, checkpoints: _Checkpoints) -> QueuedPhotoOut:
    """A photo to rule on as returned: `scene` and the checkpoint from the sessions file."""
    return QueuedPhotoOut(
        submission=photo.id,
        team=photo.team,
        checkpoint=checkpoints.summary(photo.checkpoint),
        attempt=photo.attempt,
        received_at=utc_iso(photo.received_at),
        pose=photo.pose,
        scene=checkpoints.scene(photo.checkpoint),
        reference_photos=checkpoints.reference_photos(photo.checkpoint),
        checks=[
            StoredCheckOut(
                check=check.check,
                outcome=check.outcome,
                confidence=check.confidence,
                reason=check.reason,
                detail=check.detail,
            )
            for check in photo.checks
        ],
        referee=RefereeOut(status=photo.referee.status, error_code=photo.referee.error_code)
        if photo.referee is not None
        else None,
    )


def recent_out(ruled: RecentRuling, checkpoints: _Checkpoints) -> RecentRulingOut:
    """A recent ruling as returned."""
    return RecentRulingOut(
        submission=ruled.id,
        team=ruled.team,
        checkpoint=checkpoints.summary(ruled.checkpoint),
        ruling=ruled.ruling.ruling,
        note=ruled.ruling.note,
        ruled_at=utc_iso(ruled.ruling.ruled_at),
        verdict=ruled.verdict,
    )


@router.get(
    "/sessions/{session}/review",
    responses={
        401: {"description": "Moderator code required (code: moderator_unauthorised)"},
        404: {"description": "Unknown session"},
    },
)
def review_queue(
    session: Annotated[GameSession, Depends(require_moderator)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> ReviewOut:
    """Every `pending` photo nobody has ruled on, oldest first, and the latest 20 rulings."""
    queue = store.review_queue(session.id, RECENT_SHOWN)
    checkpoints = _Checkpoints(sessions, session.id)
    return ReviewOut(
        to_review=[queued_out(photo, checkpoints) for photo in queue.to_review],
        recent=[recent_out(ruled, checkpoints) for ruled in queue.recent],
    )
