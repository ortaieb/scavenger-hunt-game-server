"""`POST /challenge`: receive a participant's challenge photo and its metadata."""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from game_server.config import Settings, get_settings
from game_server.models import ChallengeAccepted, ChallengeMetadata
from game_server.storage import ImageStore

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


def describe(metadata: ChallengeMetadata, image_path: Path) -> str:
    """Build the log line announcing a received challenge."""
    location = metadata.location
    return (
        f"Received challenge request for {metadata.session}[{metadata.participant}] "
        f"arrived at {metadata.capture_time.isoformat()} "
        f"from ({location.lat},{location.long}), image stored in: {image_path}"
    )


@router.post(
    "/challenge",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        413: {"description": "Image larger than the configured limit"},
        415: {"description": "Image part is not image/jpeg"},
    },
)
def submit_challenge(
    metadata: Annotated[
        str,
        Form(description="JSON: session, participant, location {lat, long}, capture-time"),
    ],
    challenge_image: Annotated[
        UploadFile, File(alias="challenge-image", description="The photo, as image/jpeg")
    ],
    store: Annotated[ImageStore, Depends(get_image_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChallengeAccepted:
    """Store the challenge image and log the submission.

    Metadata and image are both validated before anything is written, so a rejected
    request never leaves an orphan file behind.
    """
    parsed = parse_metadata(metadata)
    data = read_jpeg(challenge_image, settings.max_image_bytes)
    image_id, image_path = store.save(data)
    logger.info(describe(parsed, image_path))
    return ChallengeAccepted(image_id=image_id)
