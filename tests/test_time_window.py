from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from game_server.checks.base import Rejection, SubmissionContext, run_checks
from game_server.checks.base import rejections as rejections_of
from game_server.checks.time_window import (
    CAPTURE_IN_FUTURE,
    OUTSIDE_WINDOW,
    STALE_CAPTURE,
    TimeWindowCheck,
)
from game_server.config import Settings
from game_server.models import ChallengeMetadata, Location
from game_server.session_runs import SessionRun
from game_server.sessions import Checkpoint, GameSession, Window

SESSION_START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
SESSION_END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
WINDOW_OPENS = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
WINDOW_CLOSES = datetime(2026, 10, 3, 11, 0, tzinfo=UTC)
INSIDE = datetime(2026, 10, 3, 10, 30, tzinfo=UTC)
SECOND = timedelta(seconds=1)
CHECKPOINT_WINDOW = Window(opens_at=WINDOW_OPENS, closes_at=WINDOW_CLOSES)
# Started and stopped by the moderator exactly at the planned times.
ON_TIME = SessionRun(started_at=SESSION_START, stopped_at=SESSION_END)

CHECK = TimeWindowCheck(
    max_capture_age=timedelta(seconds=300), max_clock_skew=timedelta(seconds=30)
)


def make_ctx(
    received_at: datetime,
    capture_time: datetime | None = None,
    window: Window | None = CHECKPOINT_WINDOW,
    run: SessionRun | None = ON_TIME,
) -> SubmissionContext:
    """A submission for a checkpoint with `window`; capture defaults to `received_at`."""
    checkpoint = Checkpoint(
        sequence=1,
        name="Spot",
        clue="Find it",
        location=Location(lat=0, long=0),
        proximity=10,
        window=window,
    )
    session = GameSession(
        id=UUID(int=1),
        name="Hunt",
        location="Here",
        start_time=SESSION_START,
        end_time=SESSION_END,
        checkpoints=(checkpoint,),
    )
    metadata = ChallengeMetadata(
        session=session.id,
        participant=UUID(int=2),
        checkpoint=1,
        location=Location(lat=0, long=0),
        capture_time=capture_time or received_at,
    )
    return SubmissionContext(metadata, received_at, session, checkpoint, b"img", 0, run=run)


def rejections(ctx: SubmissionContext) -> list[Rejection]:
    return rejections_of(run_checks(CHECK.rules(), ctx))


# --- outside_window ----------------------------------------------------------


@pytest.mark.parametrize(
    ("received_at", "expected"),
    [
        pytest.param(WINDOW_OPENS - SECOND, [OUTSIDE_WINDOW], id="just-before-opening"),
        pytest.param(WINDOW_OPENS, [], id="exactly-at-opening"),
        pytest.param(INSIDE, [], id="inside"),
        pytest.param(WINDOW_CLOSES, [], id="exactly-at-closing"),
        pytest.param(WINDOW_CLOSES + SECOND, [OUTSIDE_WINDOW], id="just-after-closing"),
    ],
)
def test_checkpoint_window_bounds(received_at: datetime, expected: list[Rejection]) -> None:
    assert rejections(make_ctx(received_at)) == expected


@pytest.mark.parametrize(
    ("received_at", "expected"),
    [
        pytest.param(SESSION_START - SECOND, [OUTSIDE_WINDOW], id="before-session"),
        pytest.param(SESSION_START, [], id="session-start"),
        pytest.param(SESSION_END, [], id="session-end"),
        pytest.param(SESSION_END + SECOND, [OUTSIDE_WINDOW], id="after-session"),
    ],
)
def test_without_checkpoint_window_uses_the_session_run(
    received_at: datetime, expected: list[Rejection]
) -> None:
    assert rejections(make_ctx(received_at, window=None)) == expected


@pytest.mark.parametrize(
    "run",
    [None, SessionRun(started_at=None, stopped_at=None)],
    ids=["no-run", "not-started"],
)
def test_a_session_that_never_started_is_never_open(run: SessionRun | None) -> None:
    assert rejections(make_ctx(INSIDE, window=None, run=run)) == [OUTSIDE_WINDOW]


def test_the_planned_end_does_not_close_a_running_session() -> None:
    late = SESSION_END + timedelta(hours=2)
    run = SessionRun(started_at=SESSION_START, stopped_at=None)

    assert rejections(make_ctx(late, window=None, run=run)) == []


def test_a_late_start_opens_the_window_late() -> None:
    run = SessionRun(started_at=WINDOW_OPENS + timedelta(minutes=10), stopped_at=None)

    assert rejections(make_ctx(INSIDE - timedelta(minutes=25), run=run)) == [OUTSIDE_WINDOW]
    assert rejections(make_ctx(INSIDE, run=run)) == []


def test_an_early_stop_closes_the_window_early() -> None:
    run = SessionRun(started_at=SESSION_START, stopped_at=INSIDE)

    assert rejections(make_ctx(INSIDE + SECOND)) == []
    assert rejections(make_ctx(INSIDE + SECOND, run=run)) == [OUTSIDE_WINDOW]


def test_a_window_closing_before_a_late_start_is_never_open() -> None:
    run = SessionRun(started_at=WINDOW_CLOSES + timedelta(minutes=5), stopped_at=None)

    for at in (WINDOW_OPENS, INSIDE, WINDOW_CLOSES, WINDOW_CLOSES + timedelta(minutes=10)):
        assert rejections(make_ctx(at, run=run)) == [OUTSIDE_WINDOW]


@pytest.mark.parametrize(
    "received_at",
    [
        pytest.param(WINDOW_OPENS - SECOND, id="received-before-opening"),
        pytest.param(WINDOW_CLOSES + SECOND, id="received-after-closing"),
    ],
)
def test_capture_time_inside_window_cannot_rescue(received_at: datetime) -> None:
    # Claimed capture time on the window's edge (so inside it) and within both capture
    # limits, so the only possible rejection is the receive time.
    capture_time = received_at + (SECOND if received_at < WINDOW_OPENS else -SECOND)

    assert rejections(make_ctx(received_at, capture_time)) == [OUTSIDE_WINDOW]


# --- stale_capture -----------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        pytest.param(timedelta(0), [], id="same-instant"),
        pytest.param(timedelta(seconds=300), [], id="exactly-at-limit"),
        pytest.param(timedelta(seconds=301), [STALE_CAPTURE], id="one-second-over"),
        pytest.param(timedelta(days=3), [STALE_CAPTURE], id="days-old"),
    ],
)
def test_capture_age(age: timedelta, expected: list[Rejection]) -> None:
    assert rejections(make_ctx(INSIDE, INSIDE - age)) == expected


# --- capture_in_future -------------------------------------------------------


@pytest.mark.parametrize(
    ("ahead", "expected"),
    [
        pytest.param(timedelta(seconds=1), [], id="within-skew"),
        pytest.param(timedelta(seconds=30), [], id="exactly-at-skew"),
        pytest.param(timedelta(seconds=31), [CAPTURE_IN_FUTURE], id="beyond-skew"),
        pytest.param(timedelta(hours=2), [CAPTURE_IN_FUTURE], id="hours-ahead"),
    ],
)
def test_capture_ahead_of_server_clock(ahead: timedelta, expected: list[Rejection]) -> None:
    assert rejections(make_ctx(INSIDE, INSIDE + ahead)) == expected


# --- combinations and offsets ------------------------------------------------


def test_reports_every_applicable_rule() -> None:
    late = WINDOW_CLOSES + timedelta(minutes=10)

    assert rejections(make_ctx(late, late - timedelta(hours=1))) == [
        OUTSIDE_WINDOW,
        STALE_CAPTURE,
    ]


@pytest.mark.parametrize(
    "offset",
    [UTC, timezone(timedelta(hours=-6)), timezone(timedelta(hours=5, minutes=30))],
    ids=["Z", "-06:00", "+05:30"],
)
@pytest.mark.parametrize(
    ("received_at", "capture_time", "expected"),
    [
        pytest.param(WINDOW_OPENS, WINDOW_OPENS, [], id="at-opening"),
        pytest.param(WINDOW_CLOSES + SECOND, WINDOW_CLOSES, [OUTSIDE_WINDOW], id="after-close"),
        pytest.param(INSIDE, INSIDE - timedelta(seconds=301), [STALE_CAPTURE], id="stale"),
        pytest.param(INSIDE, INSIDE + timedelta(seconds=31), [CAPTURE_IN_FUTURE], id="future"),
    ],
)
def test_same_instant_in_any_offset_gives_same_result(
    offset: timezone, received_at: datetime, capture_time: datetime, expected: list[Rejection]
) -> None:
    ctx = make_ctx(received_at.astimezone(offset), capture_time.astimezone(offset))

    assert rejections(ctx) == expected


def test_mixed_offsets_between_received_and_capture() -> None:
    capture_time = (INSIDE - timedelta(seconds=300)).astimezone(timezone(timedelta(hours=-6)))

    assert rejections(make_ctx(INSIDE, capture_time)) == []


# --- configuration and messages ----------------------------------------------


def test_from_settings() -> None:
    settings = Settings(max_capture_age_seconds=120, max_clock_skew_seconds=10)

    assert TimeWindowCheck.from_settings(settings) == TimeWindowCheck(
        max_capture_age=timedelta(seconds=120), max_clock_skew=timedelta(seconds=10)
    )


def test_configured_limits_are_used() -> None:
    check = TimeWindowCheck(max_capture_age=timedelta(seconds=10), max_clock_skew=SECOND)

    stale = make_ctx(INSIDE, INSIDE - 11 * SECOND)
    future = make_ctx(INSIDE, INSIDE + 2 * SECOND)

    assert rejections_of(run_checks(check.rules(), stale)) == [STALE_CAPTURE]
    assert rejections_of(run_checks(check.rules(), future)) == [CAPTURE_IN_FUTURE]


@pytest.mark.parametrize("rejection", [OUTSIDE_WINDOW, STALE_CAPTURE, CAPTURE_IN_FUTURE])
def test_messages_reveal_no_times(rejection: Rejection) -> None:
    assert not any(char.isdigit() for char in rejection.message)
