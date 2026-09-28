"""Duplicate-photo rule: a photo that already counts in this session can't count again.

Compares perceptual hashes, so re-encoding, resizing or EXIF rotation doesn't evade it.
The comparison set is the session's accepted photos (verdict not `failed`), from any
participant at any checkpoint; photos from rejected attempts are not in it, so a player
can resubmit after e.g. a timing rejection. Never compared across sessions.
"""

from dataclasses import dataclass, field

from game_server.checks.base import AcceptedPhoto, Rejection, SubmissionContext
from game_server.phash import hamming_distance

CODE = "duplicate_photo"
# Doesn't say whose photo matched or at which checkpoint.
MESSAGE = "This photo has already been used. Please take a new one."


@dataclass(frozen=True)
class DuplicatePhotoRejection(Rejection):
    """`duplicate_photo`, carrying the matched submission for moderator audit.

    The id is server-side only: responses are built from `code` and `message`.
    """

    matched_submission_id: int = field(default=0, compare=False)


@dataclass(frozen=True)
class DuplicatePhotoCheck:
    """Rejects when the photo is within `max_distance` bits of an accepted photo."""

    max_distance: int

    def __call__(self, ctx: SubmissionContext, /) -> Rejection | None:
        """Report the closest match (lowest distance, then earliest submission)."""
        match = self.closest_match(ctx.phash, ctx.accepted_photos)
        if match is None:
            return None
        return DuplicatePhotoRejection(CODE, MESSAGE, matched_submission_id=match.submission_id)

    def closest_match(
        self, phash: int, accepted: tuple[AcceptedPhoto, ...]
    ) -> AcceptedPhoto | None:
        """The accepted photo nearest to `phash`, if it is within `max_distance`."""
        candidates = [
            (distance, photo.submission_id, photo)
            for photo in accepted
            if (distance := hamming_distance(phash, photo.phash)) <= self.max_distance
        ]
        return min(candidates)[2] if candidates else None
