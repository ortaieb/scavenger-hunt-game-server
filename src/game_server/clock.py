"""The server's clock, injectable so tests can fix time."""

import time
from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]
# Seconds from an arbitrary start, never going back: for durations, not instants.
Timer = Callable[[], float]


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def utc_iso(moment: datetime) -> str:
    """`2026-10-03T09:41:05Z`: UTC, to the second, as every API timestamp is shown."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_clock() -> Clock:
    """Dependency providing the clock used to stamp when requests are received."""
    return utc_now


def get_timer() -> Timer:
    """Dependency providing the monotonic clock that times how long a request takes."""
    return time.monotonic


class Stopwatch:
    """Started on creation; reads the milliseconds since then."""

    def __init__(self, timer: Timer) -> None:
        self._timer = timer
        self._started = timer()

    def elapsed_ms(self) -> int:
        """Whole milliseconds since the stopwatch started."""
        return round((self._timer() - self._started) * 1000)
