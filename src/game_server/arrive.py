"""`POST /sessions/{session}/participants/{participant}/arrive`: a team checks in.

Arriving issues the pose to strike and a one-time code with a short TTL to hold up in the
photo. The code turns a photo into evidence of being there *now*: it can't appear in a
photo taken before the team arrived. (Checking the code in the photo comes later.)

Arrive takes no location, on purpose: refusing out-of-range check-ins would make it an
unlimited yes/no oracle for the checkpoint's position. The geofence stays in POST /challenge.
"""

import logging
import secrets
from datetime import UTC, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from game_server.clock import Clock, get_clock, utc_iso
from game_server.config import Settings, get_settings
from game_server.errors import ApiError
from game_server.game_state import team_state
from game_server.join import find_participant
from game_server.session_runs import session_phase
from game_server.sessions import SessionRepository, get_session_repository
from game_server.submissions import Arrival, SubmissionStore, get_submission_store

logger = logging.getLogger(__name__)

router = APIRouter()


def draw_code() -> str:
    """A 4-digit one-time code (leading zeros kept), from a cryptographic source."""
    return f"{secrets.randbelow(10_000):04d}"


class ArriveRequest(BaseModel):
    """The checkpoint the team thinks it's at: it must be the team's current one."""

    model_config = ConfigDict(extra="forbid")

    checkpoint: int = Field(ge=1, strict=True, description="The checkpoint's `sequence`")


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class ArrivalOut(BaseModel):
    """The pose to strike and the one-time code to hold up in the photo."""

    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)

    checkpoint: int
    pose: str | None
    code: str
    issued_at: str
    expires_at: str

    @classmethod
    def of(cls, arrival: Arrival) -> "ArrivalOut":
        """The response for a stored arrival."""
        return cls(
            checkpoint=arrival.checkpoint,
            pose=arrival.pose,
            code=arrival.code,
            issued_at=utc_iso(arrival.issued_at),
            expires_at=utc_iso(arrival.expires_at),
        )


def _conflict(detail: str, code: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, detail, code)


@router.post(
    "/sessions/{session}/participants/{participant}/arrive",
    responses={
        201: {"model": ArrivalOut, "description": "A new arrival with a fresh code"},
        200: {"description": "The team's active arrival at this checkpoint, unchanged"},
        404: {"description": "Unknown session, participant or checkpoint"},
        409: {
            "description": "Not started, finished, ended, not current, or not open (codes: "
            "session_not_started, hunt_finished, session_stopped, not_current_checkpoint, "
            "checkpoint_closed)"
        },
    },
)
def arrive(
    session: UUID,
    participant: UUID,
    body: ArriveRequest,
    response: Response,
    clock: Annotated[Clock, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> ArrivalOut:
    """Check in at the team's current checkpoint and get the pose and a one-time code."""
    now = clock().astimezone(UTC)
    if sessions.get_session(session) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    joined = find_participant(store, sessions, session, participant)
    if joined is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown participant")
    checkpoint = sessions.get_checkpoint(session, body.checkpoint)
    if checkpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown checkpoint")
    run = store.session_run(session)
    if session_phase(run) == "scheduled":
        raise _conflict("session hasn't started", "session_not_started")
    completed = store.completed_checkpoints(session, participant)
    state = team_state(joined.session, joined.team, completed, now, run)
    if state.status == "finished":
        raise _conflict("hunt finished", "hunt_finished")
    if state.status == "ended":
        raise _conflict("session has ended", "session_stopped")
    if state.current is None or state.current.sequence != checkpoint.sequence:
        raise _conflict("not your current checkpoint", "not_current_checkpoint")
    if not state.current.open:
        raise _conflict("checkpoint isn't open", "checkpoint_closed")
    outcome = store.arrive(
        session,
        participant,
        checkpoint.sequence,
        pose=checkpoint.challenge.pose if checkpoint.challenge else None,
        now=now,
        ttl=timedelta(seconds=settings.arrival_code_ttl_seconds),
        new_code=draw_code,
    )
    logger.info(
        "Team arrived session %s participant %s checkpoint %d %s expires_at %s",
        session,
        participant,
        checkpoint.sequence,
        "new" if outcome.new else "existing",
        outcome.arrival.expires_at.isoformat(),
    )
    if outcome.new:
        response.status_code = status.HTTP_201_CREATED
    return ArrivalOut.of(outcome.arrival)
