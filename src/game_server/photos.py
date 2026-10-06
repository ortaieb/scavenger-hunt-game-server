"""The moderator's photo endpoints: a player's photo, and a checkpoint's reference photos.

The only endpoints that serve photos, and only with the session's moderator code. Each photo
is served the way the referee saw it (`prepare_image`: upright, EXIF stripped, long edge at
most `referee_max_image_edge`), never cached. Each one served is logged by session and
submission, or checkpoint and position: never by path, since a file name can describe the
place.
"""

import logging
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from game_server.challenge import get_image_store
from game_server.config import Settings, get_settings
from game_server.moderation import require_moderator
from game_server.referee import prepare_image
from game_server.sessions import GameSession, SessionRepository, get_session_repository
from game_server.storage import ImageStore
from game_server.submissions import SubmissionStore, get_submission_store

logger = logging.getLogger(__name__)

router = APIRouter()

JPEG_CONTENT_TYPE = "image/jpeg"
NO_STORE = {"Cache-Control": "no-store"}
MAX_SUBMISSION_ID = 2**63 - 1  # BIGINT
PHOTO_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {"content": {JPEG_CONTENT_TYPE: {}}, "description": "The photo, as the referee saw it"},
    401: {"description": "Moderator code required (code: moderator_unauthorised)"},
}


def _read(path: FilePath) -> bytes:
    """The file's bytes; 404 if it's gone. The path is never raised or logged."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "photo not found") from None


def _as_seen_by_referee(data: bytes, settings: Settings) -> Response:
    """The photo prepared as the referee receives it, as an uncached JPEG response."""
    prepared = prepare_image(data, settings.referee_max_image_edge)
    return Response(prepared.jpeg, media_type=JPEG_CONTENT_TYPE, headers=NO_STORE)


@router.get(
    "/sessions/{session}/submissions/{submission}/photo",
    response_class=Response,
    responses={
        **PHOTO_RESPONSES,
        404: {"description": "Unknown session or submission, or the photo's file is missing"},
    },
)
def submission_photo(
    session: Annotated[GameSession, Depends(require_moderator)],
    submission: Annotated[int, Path(ge=1, le=MAX_SUBMISSION_ID)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
    images: Annotated[ImageStore, Depends(get_image_store)],
) -> Response:
    """The player's photo of one of the session's submissions. Moderator only."""
    image_id = store.image_id(session.id, submission)
    if image_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown submission")
    served = _as_seen_by_referee(_read(images.path_for(image_id)), settings)
    logger.info("Moderator photo served: session %s submission %d", session.id, submission)
    return served


@router.get(
    "/sessions/{session}/checkpoints/{sequence}/reference-photos/{position}",
    response_class=Response,
    responses={
        **PHOTO_RESPONSES,
        404: {"description": "Unknown session, checkpoint or position"},
    },
)
def reference_photo(
    session: Annotated[GameSession, Depends(require_moderator)],
    sequence: Annotated[int, Path(ge=1, description="The checkpoint's `sequence`")],
    position: Annotated[int, Path(ge=0, description="From 0, in the sessions file's order")],
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
) -> Response:
    """The checkpoint's reference photo at `position`, as in the traces. Moderator only."""
    if sessions.get_checkpoint(session.id, sequence) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown checkpoint")
    paths = sessions.reference_photos(session.id, sequence)
    if position >= len(paths):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown reference photo")
    served = _as_seen_by_referee(_read(paths[position]), settings)
    logger.info(
        "Moderator reference photo served: session %s checkpoint %d position %d",
        session.id,
        sequence,
        position,
    )
    return served
