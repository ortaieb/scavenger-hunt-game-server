from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from game_server.checks.base import AcceptedPhoto, SubmissionContext
from game_server.checks.duplicate_photo import (
    CODE,
    MESSAGE,
    DuplicatePhotoCheck,
    DuplicatePhotoRejection,
)
from game_server.models import ChallengeMetadata, Location
from game_server.sessions import Checkpoint, GameSession

PHASH = 0xFFFF_0000_FFFF_0000
CHECK = DuplicatePhotoCheck(max_distance=6)


def flip(bits: int) -> int:
    """A hash `bits` Hamming-distance away from PHASH."""
    return PHASH ^ ((1 << bits) - 1)


@pytest.fixture
def ctx() -> SubmissionContext:
    checkpoint = Checkpoint(
        sequence=1, name="Spot", clue="Find it", location=Location(lat=0, long=0), proximity=10
    )
    session = GameSession(
        id=UUID(int=1),
        name="Hunt",
        location="Here",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 2, tzinfo=UTC),
        checkpoints=(checkpoint,),
    )
    metadata = ChallengeMetadata(
        session=session.id,
        participant=UUID(int=2),
        checkpoint=1,
        location=Location(lat=0, long=0),
        capture_time=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    return SubmissionContext(
        metadata, datetime(2026, 1, 1, 12, tzinfo=UTC), session, checkpoint, b"img", PHASH
    )


def with_accepted(ctx: SubmissionContext, *photos: tuple[int, int]) -> SubmissionContext:
    accepted = tuple(AcceptedPhoto(submission_id=i, phash=h) for i, h in photos)
    return replace(ctx, accepted_photos=accepted)


def test_nothing_accepted_yet(ctx: SubmissionContext) -> None:
    result = CHECK(ctx)

    assert (result.check, result.outcome, result.rejection) == ("photo_unique", "passed", None)


@pytest.mark.parametrize(
    ("bits", "duplicate"),
    [(0, True), (3, True), (6, True), (7, False), (32, False)],
    ids=["identical", "close", "exactly-at-threshold", "one-over", "unrelated"],
)
def test_threshold_is_inclusive(ctx: SubmissionContext, bits: int, duplicate: bool) -> None:
    rejection = CHECK(with_accepted(ctx, (10, flip(bits)))).rejection

    assert (rejection is not None) == duplicate


def test_rejection_names_the_matched_submission(ctx: SubmissionContext) -> None:
    rejection = CHECK(with_accepted(ctx, (10, flip(40)), (11, flip(2)))).rejection

    assert isinstance(rejection, DuplicatePhotoRejection)
    assert (rejection.code, rejection.message) == (CODE, MESSAGE)
    assert rejection.matched_submission_id == 11


def test_closest_match_wins(ctx: SubmissionContext) -> None:
    rejection = CHECK(with_accepted(ctx, (10, flip(5)), (11, flip(1)), (12, flip(3)))).rejection

    assert isinstance(rejection, DuplicatePhotoRejection)
    assert rejection.matched_submission_id == 11


def test_equal_distance_ties_go_to_the_earliest(ctx: SubmissionContext) -> None:
    rejection = CHECK(with_accepted(ctx, (12, flip(2)), (10, flip(2)))).rejection

    assert isinstance(rejection, DuplicatePhotoRejection)
    assert rejection.matched_submission_id == 10


def test_zero_threshold_matches_only_identical(ctx: SubmissionContext) -> None:
    strict = DuplicatePhotoCheck(max_distance=0)

    assert strict(with_accepted(ctx, (10, flip(1)))).rejection is None
    assert strict(with_accepted(ctx, (10, PHASH))).rejection is not None


def test_message_reveals_no_one_or_checkpoint() -> None:
    assert not any(char.isdigit() for char in MESSAGE)
    for word in ("participant", "player", "checkpoint", "by "):
        assert word not in MESSAGE.lower()
