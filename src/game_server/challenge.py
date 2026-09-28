"""`POST /challenge`: receive a participant's challenge photo and decide its verdict."""

import logging
from collections.abc import Sequence
from datetime import UTC
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from game_server.checks import (
    Check,
    Rejection,
    SubmissionContext,
    decide_verdict,
    get_checks,
    run_checks,
)
from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.models import (
    ChallengeMetadata,
    ChallengeVerdict,
    CheckpointVerdict,
    RejectionOut,
    Verdict,
)
from game_server.sessions import Checkpoint, GameSession, SessionRepository, get_session_repository
from game_server.storage import ImageStore
from game_server.submissions import NewSubmission, SubmissionStore, get_submission_store

logger = logging.getLogger(__name__)

router = APIRouter()

JPEG_CONTENT_TYPE = "image/jpeg"
# Every JPEG file starts with an SOI marker followed by another marker's first byte.
JPEG_MAGIC = b"\xff\xd8\xff"


def get_image_store(settings: Annotated[Settings, Depends(get_settings)]) -> ImageStore:
    """Dependency providing the image store rooted at the configured base path."""
    return ImageStore(settings.image_base_path)


def parse_metadata(raw: str) -> ChallengeMetadata:
    """Parse the `metadata` part, reporting failures as a standard 422 validation error."""
    try:
        return ChallengeMetadata.model_validate_json(raw)
    except ValidationError as exc:
        errors = [
            {**error, "loc": ("body", "metadata", *error["loc"])}
            for error in exc.errors(include_url=False)
        ]
        raise RequestValidationError(errors) from exc


def read_jpeg(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an uploaded JPEG, rejecting wrong content types, oversize and non-JPEG data."""
    if upload.content_type != JPEG_CONTENT_TYPE:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"challenge-image must be {JPEG_CONTENT_TYPE}, got {upload.content_type}",
        )
    data = upload.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"challenge-image exceeds {max_bytes} bytes",
        )
    if not data.startswith(JPEG_MAGIC):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "challenge-image is not a valid JPEG",
        )
    return data


def find_target(
    sessions: SessionRepository, metadata: ChallengeMetadata
) -> tuple[GameSession, Checkpoint]:
    """Look up the submission's session and checkpoint, or fail with 404."""
    session = sessions.get_session(metadata.session)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    checkpoint = sessions.get_checkpoint(metadata.session, metadata.checkpoint)
    if checkpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown checkpoint")
    return session, checkpoint


def store_submission(
    images: ImageStore,
    submissions: SubmissionStore,
    ctx: SubmissionContext,
    rejections: Sequence[Rejection],
) -> tuple[NewSubmission, int, Path]:
    """Save the image and record the submission; remove the image if recording fails."""
    image_id, image_path = images.save(ctx.image)
    metadata = ctx.metadata
    submission = NewSubmission(
        session=metadata.session,
        participant=metadata.participant,
        checkpoint=metadata.checkpoint,
        received_at=ctx.received_at,
        capture_time=metadata.capture_time,
        lat=metadata.location.lat,
        long=metadata.location.long,
        image_id=image_id,
        verdict=decide_verdict(rejections),
        rejections=rejections,
        distance_m=ctx.distance_m,
    )
    try:
        recorded = submissions.record(submission)
    except BaseException:
        image_path.unlink(missing_ok=True)
        raise
    return submission, recorded.attempt, image_path


def describe(submission: NewSubmission, attempt: int, image_path: Path) -> str:
    """Build the log line announcing a received challenge and its verdict.

    Includes the distance for moderator review: server logs only, never the response.
    """
    codes = ",".join(rejection.code for rejection in submission.rejections) or "-"
    return (
        f"Received challenge request for {submission.session}[{submission.participant}] "
        f"arrived at {submission.capture_time.isoformat()} "
        f"from ({submission.lat},{submission.long}), image stored in: {image_path}; "
        f"checkpoint {submission.checkpoint} attempt {attempt} "
        f"distance {submission.distance_m:.1f}m "
        f"verdict {submission.verdict} rejections [{codes}]"
    )


def to_response(submission: NewSubmission, attempt: int) -> ChallengeVerdict:
    """Build the verdict body returned to the client."""
    return ChallengeVerdict(
        verdict=Verdict(
            game=submission.session,
            participant=submission.participant,
            checkpoint=CheckpointVerdict(
                sequence=submission.checkpoint,
                attempt=attempt,
                time=submission.received_at,
                verdict=submission.verdict,
                rejections=[
                    RejectionOut(code=rejection.code, message=rejection.message)
                    for rejection in submission.rejections
                ],
            ),
        ),
        image_id=submission.image_id,
    )


@router.post(
    "/challenge",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": ChallengeVerdict, "description": "Recorded; verdict `failed`"},
        202: {"description": "Recorded; verdict `pending`"},
        404: {"description": "Unknown session, or unknown checkpoint in the session"},
        413: {"description": "Image larger than the configured limit"},
        415: {"description": "Image part is not image/jpeg"},
    },
)
def submit_challenge(
    metadata: Annotated[
        str,
        Form(
            description="JSON: session, participant, checkpoint, location {lat, long}, capture-time"
        ),
    ],
    challenge_image: Annotated[
        UploadFile, File(alias="challenge-image", description="The photo, as image/jpeg")
    ],
    response: Response,
    clock: Annotated[Clock, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    checks: Annotated[Sequence[Check], Depends(get_checks)],
    images: Annotated[ImageStore, Depends(get_image_store)],
    submissions: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> ChallengeVerdict:
    """Check the submission, record it as the next attempt, and return the verdict.

    Everything is validated and looked up before anything is written, so a rejected
    request (4xx) stores neither an image nor a row.
    """
    received_at = clock().astimezone(UTC)
    parsed = parse_metadata(metadata)
    image = read_jpeg(challenge_image, settings.max_image_bytes)
    session, checkpoint = find_target(sessions, parsed)
    ctx = SubmissionContext(parsed, received_at, session, checkpoint, image)
    rejections = run_checks(checks, ctx)
    submission, attempt, image_path = store_submission(images, submissions, ctx, rejections)
    logger.info(describe(submission, attempt, image_path))
    if submission.verdict == "failed":
        response.status_code = status.HTTP_200_OK
    return to_response(submission, attempt)
