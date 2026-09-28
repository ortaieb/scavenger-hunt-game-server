"""Player-facing checkpoint information that is safe to reveal before arrival."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path

from game_server.lookup import find_checkpoint
from game_server.models import PoseInstruction
from game_server.sessions import SessionRepository, get_session_repository

router = APIRouter()


@router.get(
    "/sessions/{session}/checkpoints/{sequence}/challenge",
    responses={404: {"description": "Unknown session, or unknown checkpoint in the session"}},
)
def checkpoint_challenge(
    session: UUID,
    sequence: Annotated[int, Path(ge=1, description="The checkpoint's `sequence`")],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
) -> PoseInstruction:
    """The pose the player must strike in the photo, so they can see it before taking it.

    Only the pose: never the scene description (it gives away the place), nor the
    checkpoint's name, clue, location or window.
    """
    _, checkpoint = find_checkpoint(sessions, session, sequence)
    challenge = checkpoint.challenge
    return PoseInstruction(pose=challenge.pose if challenge else None)
