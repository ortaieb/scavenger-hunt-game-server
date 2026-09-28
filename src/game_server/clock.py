"""The server's clock, injectable so tests can fix time."""

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def get_clock() -> Clock:
    """Dependency providing the clock used to stamp when requests are received."""
    return utc_now
