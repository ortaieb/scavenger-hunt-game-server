"""The review queue as the store reads it (`SubmissionStore.review_queue`)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from game_server.checks import CheckResult
from game_server.models import VerdictStatus
from game_server.referee import RefereeCall, RefereeReport, SentImage
from game_server.referee_traces import StoredCheck
from game_server.review_queue import QueuedPhoto, RecentRuling, RefereeOutcome
from game_server.rulings import Ruling, StoredRuling
from game_server.submissions import NewSubmission, SubmissionStore

SESSION = UUID(int=1)
OTHER_SESSION = UUID(int=9)
PARTICIPANT = UUID(int=2)
IMAGE = UUID(int=3)
RECEIVED = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
RULED = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)
POSE_UNSURE = CheckResult(
    "pose_correct", "uncertain", 0.6, "A moderator will review it.", detail="Arms half up."
)
TIMEOUT = RefereeReport(
    status="error",
    error_code="timeout",
    model="claude-haiku-4-5",
    latency_ms=8000,
    call=RefereeCall("You are the referee.", "<scene>…</scene>", SentImage("ab" * 32, 10, 10)),
)

SUBMISSION = NewSubmission(
    session=SESSION,
    participant=PARTICIPANT,
    checkpoint=1,
    received_at=RECEIVED,
    capture_time=RECEIVED,
    lat=51.5,
    long=-0.1,
    image_id=IMAGE,
    verdict="pending",
    checks=(),
    distance_m=1.0,
    phash=0,
    processing_ms=40,
)


def record(
    store: SubmissionStore,
    verdict: VerdictStatus = "pending",
    *,
    session: UUID = SESSION,
    minutes: int = 0,
    checks: tuple[CheckResult, ...] = (),
    referee: RefereeReport | None = None,
) -> int:
    received = RECEIVED + timedelta(minutes=minutes)
    submission = replace(
        SUBMISSION,
        session=session,
        verdict=verdict,
        received_at=received,
        capture_time=received,
        checks=checks,
        referee=referee,
    )
    return store.record(submission).id


def rule(
    store: SubmissionStore,
    submission: int,
    ruling: Ruling,
    minutes: int = 0,
    session: UUID = SESSION,
) -> None:
    ruled_at = RULED + timedelta(minutes=minutes)
    assert store.rule(session, submission, ruling, None, ruled_at) is not None


def test_a_waiting_photo_with_its_checks_and_the_referees_outcome(store: SubmissionStore) -> None:
    submission = record(store, checks=(POSE_UNSURE,), referee=TIMEOUT)

    queue = store.review_queue(SESSION, 20)

    assert queue.to_review == (
        QueuedPhoto(
            id=submission,
            team=None,  # the participant never joined
            checkpoint=1,
            attempt=1,
            received_at=RECEIVED,
            pose=None,  # no check-in
            checks=(
                StoredCheck("pose_correct", "uncertain", 0.6, POSE_UNSURE.reason, "Arms half up."),
            ),
            referee=RefereeOutcome("error", "timeout"),
        ),
    )
    assert queue.recent == ()


@pytest.mark.parametrize("verdict", ["pass", "failed"])
def test_only_pending_photos_wait(store: SubmissionStore, verdict: VerdictStatus) -> None:
    record(store, verdict)

    assert store.review_queue(SESSION, 20).to_review == ()


def test_another_sessions_photos_are_not_read(store: SubmissionStore) -> None:
    rule(store, record(store, session=OTHER_SESSION), "approve", session=OTHER_SESSION)
    record(store, session=OTHER_SESSION)

    assert store.review_queue(SESSION, 20).to_review == ()
    assert store.review_queue(SESSION, 20).recent == ()


def test_a_ruled_photo_is_recent_with_its_latest_ruling(store: SubmissionStore) -> None:
    submission = record(store)
    rule(store, submission, "approve")
    rule(store, submission, "reject", minutes=1)

    queue = store.review_queue(SESSION, 20)

    assert queue.to_review == ()
    assert queue.recent == (
        RecentRuling(
            id=submission,
            team=None,
            checkpoint=1,
            verdict="pending",
            ruling=StoredRuling("reject", None, RULED + timedelta(minutes=1)),
        ),
    )


def test_recent_is_capped_newest_first(store: SubmissionStore) -> None:
    first, second, third = (record(store, minutes=n) for n in range(3))
    for minutes, submission in enumerate((second, third, first)):
        rule(store, submission, "approve", minutes)

    assert [ruled.id for ruled in store.review_queue(SESSION, 2).recent] == [first, third]
