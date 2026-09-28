from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import cached_property
from types import MethodType
from typing import cast
from uuid import UUID

import pytest
from pytest_mock import MockerFixture

from game_server.checks import (
    Check,
    CheckResult,
    Rejection,
    SubmissionContext,
    decide_verdict,
    get_checks,
    rejections,
    run_checks,
)
from game_server.checks.base import AcceptedPhoto
from game_server.checks.duplicate_photo import DuplicatePhotoCheck
from game_server.checks.geofence import GeofenceCheck
from game_server.checks.time_window import TimeWindowCheck
from game_server.config import Settings
from game_server.models import ChallengeMetadata, CheckOutcome, Location
from game_server.sessions import Checkpoint, GameSession

WINDOW = Rejection("outside_window", "This checkpoint isn't open right now.")
GEOFENCE = Rejection("outside_geofence", "You don't seem to be at the checkpoint.")


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
    metadata = ChallengeMetadata.model_validate(
        {
            "session": session.id,
            "participant": UUID(int=2),
            "checkpoint": 1,
            "location": {"lat": 0, "long": 0},
            "capture-time": "2026-01-01T12:00:00Z",
        }
    )
    return SubmissionContext(
        metadata, datetime(2026, 1, 1, 12, tzinfo=UTC), session, checkpoint, b"img", 0
    )


PASSED = CheckResult.passed("ok", "Fine.")
WINDOW_FAILED = CheckResult.failed("window_open", WINDOW)
GEOFENCE_FAILED = CheckResult.failed("in_range", GEOFENCE)
UNCERTAIN = CheckResult("scene_matches", "uncertain", 0.4, "We couldn't tell.")
SKIPPED = CheckResult("pose_correct", "skipped", 0.0, "Not checked.")


def returning(result: CheckResult) -> Check:
    return lambda ctx: result


def test_no_checks_no_results(ctx: SubmissionContext) -> None:
    assert run_checks([], ctx) == []


def test_returns_every_result_in_check_order(ctx: SubmissionContext) -> None:
    checks = [returning(WINDOW_FAILED), returning(PASSED), returning(GEOFENCE_FAILED)]

    results = run_checks(checks, ctx)

    assert results == [WINDOW_FAILED, PASSED, GEOFENCE_FAILED]
    assert rejections(results) == [WINDOW, GEOFENCE]


def test_runs_every_check_after_a_failure(ctx: SubmissionContext) -> None:
    calls: list[str] = []

    def first(ctx: SubmissionContext) -> CheckResult:
        calls.append("first")
        return WINDOW_FAILED

    def second(ctx: SubmissionContext) -> CheckResult:
        calls.append("second")
        return PASSED

    run_checks([first, second], ctx)

    assert calls == ["first", "second"]


@pytest.mark.parametrize(
    ("results", "verdict"),
    [
        ([], "pending"),
        ([PASSED], "pending"),
        ([PASSED, UNCERTAIN, SKIPPED], "pending"),
        ([WINDOW_FAILED], "failed"),
        ([PASSED, WINDOW_FAILED, GEOFENCE_FAILED], "failed"),
        ([UNCERTAIN, WINDOW_FAILED], "failed"),
    ],
)
def test_decide_verdict(results: list[CheckResult], verdict: str) -> None:
    assert decide_verdict(results) == verdict


def test_passed_and_failed_constructors() -> None:
    assert CheckResult("ok", "passed", 1.0, "Fine.") == PASSED
    assert CheckResult("window_open", "failed", 1.0, WINDOW.message, WINDOW) == WINDOW_FAILED


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_must_be_between_zero_and_one(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        CheckResult("ok", "passed", confidence, "Fine.")


@pytest.mark.parametrize(
    ("outcome", "rejection"),
    [("failed", None), ("passed", WINDOW), ("uncertain", WINDOW), ("skipped", WINDOW)],
)
def test_rejection_only_and_always_on_failure(
    outcome: CheckOutcome, rejection: Rejection | None
) -> None:
    with pytest.raises(ValueError, match="rejection"):
        CheckResult("ok", outcome, 1.0, "Reason.", rejection)


def test_verdict_is_never_pass_without_rejections() -> None:
    assert decide_verdict([]) != "pass"


def test_registered_checks() -> None:
    settings = Settings(max_capture_age_seconds=60, max_clock_skew_seconds=5, phash_max_distance=4)

    *time_rules, geofence, duplicate = get_checks(settings)

    methods = [cast(MethodType, rule) for rule in time_rules]
    assert [method.__name__ for method in methods] == [
        "window_open",
        "capture_fresh",
        "capture_time_plausible",
    ]
    assert {method.__self__ for method in methods} == {
        TimeWindowCheck(timedelta(seconds=60), timedelta(seconds=5))
    }
    assert geofence == GeofenceCheck()
    assert duplicate == DuplicatePhotoCheck(max_distance=4)


def test_context_distance_is_computed_once(ctx: SubmissionContext, mocker: MockerFixture) -> None:
    distance = mocker.patch("game_server.checks.base.geo.distance_m", return_value=42.0)

    assert (ctx.distance_m, ctx.distance_m) == (42.0, 42.0)
    distance.assert_called_once_with(ctx.metadata.location, ctx.checkpoint.location)


def test_context_is_immutable(ctx: SubmissionContext) -> None:
    with pytest.raises(AttributeError):
        ctx.received_at = datetime(2000, 1, 1, tzinfo=UTC)  # type: ignore[misc]  # runtime guard


def test_context_supports_lazily_shared_values(ctx: SubmissionContext) -> None:
    """Later checks share costly values via cached_property on a context subclass."""
    computed: list[int] = []

    class Extended(SubmissionContext):
        @cached_property
        def image_size(self) -> int:
            computed.append(1)
            return len(self.image)

    extended = Extended(
        ctx.metadata, ctx.received_at, ctx.session, ctx.checkpoint, ctx.image, ctx.phash
    )

    assert (extended.image_size, extended.image_size) == (3, 3)
    assert computed == [1]


# --- reasons are safe to show (#19) ------------------------------------------


def contexts_covering_every_outcome(ctx: SubmissionContext) -> list[SubmissionContext]:
    """Contexts that between them make every registered check both pass and fail."""
    late = ctx.session.end_time + timedelta(hours=1)
    everything_fails = replace(
        ctx,
        received_at=late,
        metadata=ctx.metadata.model_copy(
            update={
                "capture_time": late - timedelta(days=1),
                "location": Location(lat=10, long=10),
            }
        ),
        accepted_photos=(AcceptedPhoto(submission_id=1, phash=ctx.phash),),
    )
    in_future = replace(
        ctx,
        metadata=ctx.metadata.model_copy(
            update={"capture_time": ctx.received_at + timedelta(hours=1)}
        ),
    )
    return [ctx, everything_fails, in_future]


def every_result(ctx: SubmissionContext) -> list[CheckResult]:
    checks = get_checks(Settings())
    return [r for c in contexts_covering_every_outcome(ctx) for r in run_checks(checks, c)]


CHECK_NAMES = ["window_open", "capture_fresh", "capture_time_plausible", "in_range", "photo_unique"]


@pytest.mark.parametrize("outcome", ["passed", "failed"])
@pytest.mark.parametrize("check", CHECK_NAMES)
def test_reason_reveals_no_coordinates_distance_or_times(
    ctx: SubmissionContext, check: str, outcome: str
) -> None:
    results = [r for r in every_result(ctx) if (r.check, r.outcome) == (check, outcome)]

    assert results, f"no {outcome} result for {check}"
    for result in results:
        # Coordinates, distances and times all need digits; a reason never has any.
        assert not any(char.isdigit() for char in result.reason)
        assert result.detail is None
        assert result.confidence == 1.0
