from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from game_server import rate_limit
from game_server.rate_limit import RateLimiter

T0 = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
INTERVAL = timedelta(seconds=10)


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(INTERVAL)


def test_first_call_is_allowed(limiter: RateLimiter) -> None:
    assert limiter.check("a", T0) is None


def test_second_call_within_interval_is_refused_with_time_left(limiter: RateLimiter) -> None:
    limiter.check("a", T0)

    assert limiter.check("a", T0 + timedelta(seconds=3)) == timedelta(seconds=7)


def test_call_exactly_one_interval_later_is_allowed(limiter: RateLimiter) -> None:
    limiter.check("a", T0)

    assert limiter.check("a", T0 + INTERVAL) is None


def test_refused_calls_do_not_extend_the_wait(limiter: RateLimiter) -> None:
    limiter.check("a", T0)
    limiter.check("a", T0 + timedelta(seconds=9))

    assert limiter.check("a", T0 + INTERVAL) is None


def test_keys_are_independent(limiter: RateLimiter) -> None:
    limiter.check("a", T0)

    assert limiter.check("b", T0) is None


def test_expired_keys_are_pruned_past_the_size_cap(
    limiter: RateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rate_limit, "_PRUNE_ABOVE", 3)
    for key in ("a", "b", "c"):
        limiter.check(key, T0)

    limiter.check("d", T0 + INTERVAL)

    assert len(limiter) == 1


def test_concurrent_calls_for_one_key_allow_exactly_one(limiter: RateLimiter) -> None:
    barrier = Barrier(8)

    def call(_: int) -> bool:
        barrier.wait()
        return limiter.check("a", T0) is None

    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(call, range(8)))

    assert allowed.count(True) == 1
