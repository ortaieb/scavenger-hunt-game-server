"""`POST /challenge`: receive a participant's challenge photo and decide its verdict."""

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from game_server.checks import (
    Check,
    CheckResult,
    SubmissionContext,
    decide_verdict,
    get_checks,
    rejections,
    run_checks,
)
from game_server.checks.duplicate_photo import DuplicatePhotoRejection
from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.models import (
    ChallengeMetadata,
    ChallengeVerdict,
    CheckOut,
    CheckpointVerdict,
    RejectionOut,
    Verdict,
)
from game_server.phash import UndecodableImageError, perceptual_hash
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


def hash_image(image: bytes) -> int:
    """Perceptual hash of the photo; bytes that can't be decoded fail with 422."""
    try:
        return perceptual_hash(image)
    except UndecodableImageError as exc:
        # Pillow's message isn't meant for clients (it can include object addresses).
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "challenge-image could not be decoded"
        ) from exc


def new_submission(
    ctx: SubmissionContext, image_id: UUID, results: Sequence[CheckResult]
) -> NewSubmission:
    """The row to record for a judged submission."""
    metadata = ctx.metadata
    return NewSubmission(
        session=metadata.session,
        participant=metadata.participant,
        checkpoint=metadata.checkpoint,
        received_at=ctx.received_at,
        capture_time=metadata.capture_time,
        lat=metadata.location.lat,
        long=metadata.location.long,
        image_id=image_id,
        verdict=decide_verdict(results),
        checks=results,
        distance_m=ctx.distance_m,
        phash=ctx.phash,
        phash_match_id=next(
            (
                r.matched_submission_id
                for r in rejections(results)
                if isinstance(r, DuplicatePhotoRejection)
            ),
            None,
        ),
    )


def judge_and_record(
    ctx: SubmissionContext,
    checks: Sequence[Check],
    images: ImageStore,
    submissions: SubmissionStore,
) -> tuple[NewSubmission, int, Path]:
    """Run the checks and record the result, all inside one write transaction.

    The accepted-photo snapshot, the checks and the insert are serialised against other
    submissions, so two uploads of the same photo can't both be accepted. If anything
    fails after the image is saved, the image is removed.
    """
    saved: Path | None = None
    try:
        with submissions.transaction() as transaction:
            ctx = replace(ctx, accepted_photos=transaction.accepted_photos(ctx.metadata.session))
            results = run_checks(checks, ctx)
            image_id, image_path = images.save(ctx.image)
            saved = image_path
            submission = new_submission(ctx, image_id, results)
            recorded = transaction.record(submission)
    except BaseException:
        if saved is not None:
            saved.unlink(missing_ok=True)
        raise
    return submission, recorded.attempt, image_path


def describe(submission: NewSubmission, attempt: int, image_path: Path) -> str:
    """Build the log line announcing a received challenge and its verdict.

    Includes the distance for moderator review: server logs only, never the response.
    """
    codes = ",".join(rejection.code for rejection in submission.rejections) or "-"
    checks = ",".join(f"{result.check}:{result.outcome}" for result in submission.checks)
    return (
        f"Received challenge request for {submission.session}[{submission.participant}] "
        f"arrived at {submission.capture_time.isoformat()} "
        f"from ({submission.lat},{submission.long}), image stored in: {image_path}; "
        f"checkpoint {submission.checkpoint} attempt {attempt} "
        f"distance {submission.distance_m:.1f}m "
        f"verdict {submission.verdict} checks [{checks}] rejections [{codes}]"
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
                # Built field by field: `detail` is moderator-only and must never be sent.
                checks=[
                    CheckOut(
                        check=result.check,
                        outcome=result.outcome,
                        confidence=result.confidence,
                        reason=result.reason,
                    )
                    for result in submission.checks
                ],
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
        422: {"description": "Invalid metadata, or the image can't be decoded"},
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

    Everything is validated, decoded and looked up before anything is written, so a
    rejected request (4xx) stores neither an image nor a row.
    """
    received_at = clock().astimezone(UTC)
    parsed = parse_metadata(metadata)
    image = read_jpeg(challenge_image, settings.max_image_bytes)
    phash = hash_image(image)  # decoded outside the write lock: it's the slow part
    session, checkpoint = find_target(sessions, parsed)
    ctx = SubmissionContext(parsed, received_at, session, checkpoint, image, phash)
    submission, attempt, image_path = judge_and_record(ctx, checks, images, submissions)
    logger.info(describe(submission, attempt, image_path))
    if submission.verdict == "failed":
        response.status_code = status.HTTP_200_OK
    return to_response(submission, attempt)
