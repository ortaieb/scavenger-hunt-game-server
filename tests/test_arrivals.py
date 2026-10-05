from datetime import UTC, datetime, timedelta

import pytest

from game_server.arrivals import Arrival, LatestArrival

ISSUED = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
EXPIRES = ISSUED + timedelta(minutes=10)
ARRIVAL = Arrival(7, 1, "1234", "Wave", issued_at=ISSUED, expires_at=EXPIRES)
SECOND = timedelta(seconds=1)


@pytest.mark.parametrize(
    ("at", "active"),
    [
        (ISSUED - SECOND, False),  # not issued yet
        (ISSUED, True),
        (EXPIRES - SECOND, True),
        (EXPIRES, False),  # expiry is exclusive, as for arrive
    ],
)
def test_unused_arrival_is_active_from_issue_until_expiry(at: datetime, active: bool) -> None:
    assert LatestArrival(ARRIVAL, used=False).active_at(at) is active


def test_used_arrival_is_never_active() -> None:
    assert LatestArrival(ARRIVAL, used=True).active_at(ISSUED) is False


@pytest.mark.parametrize(
    ("at", "used", "expired"),
    [
        (EXPIRES - SECOND, False, False),
        (EXPIRES, False, True),
        (EXPIRES + timedelta(hours=1), False, True),
        (EXPIRES, True, False),  # a photo was sent for it: it didn't run out
    ],
)
def test_expired_means_ran_out_unused(at: datetime, used: bool, expired: bool) -> None:
    assert LatestArrival(ARRIVAL, used=used).expired_at(at) is expired
