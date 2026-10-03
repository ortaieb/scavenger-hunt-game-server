"""Time rules: the server's receive time against the checkpoint window, and capture-time sanity.

The deciding clock is the server's `received_at`. The client's `capture-time` is a claim:
it can get a submission rejected (stale or in the future), but it can never rescue one
received outside the window.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

from game_server.checks.base import Check, CheckResult, Rejection, SubmissionContext
from game_server.config import Settings
from game_server.session_runs import SessionRun
from game_server.sessions import Checkpoint, SessionRepository

OUTSIDE_WINDOW = Rejection("outside_window", "This checkpoint isn't open right now.")
STALE_CAPTURE = Rejection("stale_capture", "Photo was taken too long ago, please take a new one.")
CAPTURE_IN_FUTURE = Rejection(
    "capture_in_future",
    "Photo's capture time is ahead of the server's clock. Check your phone's date and time, "
    "then take a new one.",
)


WINDOW_OPEN = "window_open"
CAPTURE_FRESH = "capture_fresh"
CAPTURE_TIME_PLAUSIBLE = "capture_time_plausible"


def window_is_open(run: SessionRun | None, checkpoint: Checkpoint, at: datetime) -> bool:
    """Whether `at` falls in the checkpoint's effective window, bounds inclusive."""
    window = SessionRepository.effective_window(run, checkpoint)
    if window is None:
        return False
    opens_at, closes_at = window
    return opens_at <= at <= closes_at


@dataclass(frozen=True)
class TimeWindowCheck:
    """Three independent time rules; each is a `Check` so every applicable one is reported."""

    max_capture_age: timedelta
    max_clock_skew: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Configure the limits from `GAME_SERVER_MAX_CAPTURE_AGE_SECONDS` / `..._SKEW_...`."""
        return cls(
            max_capture_age=timedelta(seconds=settings.max_capture_age_seconds),
            max_clock_skew=timedelta(seconds=settings.max_clock_skew_seconds),
        )

    def rules(self) -> tuple[Check, ...]:
        """The rules to register, in reporting order."""
        return (self.window_open, self.capture_fresh, self.capture_time_plausible)

    def window_open(self, ctx: SubmissionContext) -> CheckResult:
        """`received_at` must fall in the checkpoint's effective window, bounds inclusive."""
        if window_is_open(ctx.run, ctx.checkpoint, ctx.received_at):
            return CheckResult.passed(WINDOW_OPEN, "Submitted while the checkpoint was open.")
        return CheckResult.failed(WINDOW_OPEN, OUTSIDE_WINDOW)

    def capture_fresh(self, ctx: SubmissionContext) -> CheckResult:
        """The photo must have been taken at most `max_capture_age` before it was received."""
        if ctx.received_at - ctx.metadata.capture_time > self.max_capture_age:
            return CheckResult.failed(CAPTURE_FRESH, STALE_CAPTURE)
        return CheckResult.passed(CAPTURE_FRESH, "Photo was taken recently.")

    def capture_time_plausible(self, ctx: SubmissionContext) -> CheckResult:
        """The claimed capture time may be ahead of `received_at` by `max_clock_skew` at most."""
        if ctx.metadata.capture_time - ctx.received_at > self.max_clock_skew:
            return CheckResult.failed(CAPTURE_TIME_PLAUSIBLE, CAPTURE_IN_FUTURE)
        return CheckResult.passed(CAPTURE_TIME_PLAUSIBLE, "Photo's capture time is plausible.")
