from datetime import UTC, datetime

from game_server.clock import get_clock, utc_now


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
