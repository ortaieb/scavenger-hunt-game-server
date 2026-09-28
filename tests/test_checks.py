from datetime import UTC, datetime
from functools import cached_property
from uuid import UUID

import pytest

from game_server.checks import (
    Check,
    Rejection,
    SubmissionContext,
    decide_verdict,
    get_checks,
    run_checks,
)
from game_server.models import ChallengeMetadata, Location
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
        metadata, datetime(2026, 1, 1, 12, tzinfo=UTC), session, checkpoint, b"img"
    )


def returning(result: Rejection | None) -> Check:
    return lambda ctx: result


def test_no_checks_no_rejections(ctx: SubmissionContext) -> None:
    assert run_checks([], ctx) == []


def test_collects_rejections_in_check_order(ctx: SubmissionContext) -> None:
    checks = [returning(WINDOW), returning(None), returning(GEOFENCE)]

    assert run_checks(checks, ctx) == [WINDOW, GEOFENCE]


def test_runs_every_check_after_a_rejection(ctx: SubmissionContext) -> None:
    calls: list[str] = []

    def first(ctx: SubmissionContext) -> Rejection:
        calls.append("first")
        return WINDOW

    def second(ctx: SubmissionContext) -> None:
        calls.append("second")

    run_checks([first, second], ctx)

    assert calls == ["first", "second"]


@pytest.mark.parametrize(
    ("rejections", "verdict"),
    [([], "pending"), ([WINDOW], "failed"), ([WINDOW, GEOFENCE], "failed")],
)
def test_decide_verdict(rejections: list[Rejection], verdict: str) -> None:
    assert decide_verdict(rejections) == verdict


def test_verdict_is_never_pass_without_rejections() -> None:
    assert decide_verdict([]) != "pass"


def test_no_checks_registered_yet() -> None:
    assert list(get_checks()) == []


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

    extended = Extended(ctx.metadata, ctx.received_at, ctx.session, ctx.checkpoint, ctx.image)

    assert (extended.image_size, extended.image_size) == (3, 3)
    assert computed == [1]
