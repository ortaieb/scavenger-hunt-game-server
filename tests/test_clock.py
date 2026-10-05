import time
from datetime import UTC, datetime
from itertools import count

from game_server.clock import Stopwatch, get_clock, get_timer, utc_now


def test_utc_now_is_aware_utc() -> None:
    now = utc_now()

    assert now.tzinfo is UTC


def test_utc_now_is_current() -> None:
    before = datetime.now(UTC)
    now = utc_now()
    after = datetime.now(UTC)

    assert before <= now <= after


def test_get_clock_returns_utc_now() -> None:
    assert get_clock() is utc_now


def test_get_timer_is_monotonic() -> None:
    assert get_timer() is time.monotonic


def test_stopwatch_reads_whole_milliseconds_since_it_started() -> None:
    readings = iter([10.0, 10.0004, 12.5])
    stopwatch = Stopwatch(lambda: next(readings))  # started at 10.0

    assert stopwatch.elapsed_ms() == 0
    assert stopwatch.elapsed_ms() == 2500


def test_stopwatch_reads_the_timer_once_to_start() -> None:
    ticks = count()

    stopwatch = Stopwatch(lambda: next(ticks))

    assert next(ticks) == 1
    assert stopwatch.elapsed_ms() == 2000  # read at 2, started at 0
