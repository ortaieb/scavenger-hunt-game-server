"""In-memory, per-key rate limiting (single instance only)."""

from collections.abc import Hashable
from datetime import datetime, timedelta
from threading import Lock

# Past this many tracked keys, expired entries are pruned so memory stays bounded.
_PRUNE_ABOVE = 10_000


class RateLimiter:
    """Allows at most one call per `interval` for each key.

    Only allowed calls start a new interval: calls refused while waiting don't extend it.
    State lives in this process, so it isn't shared between instances and resets on restart.
    """

    def __init__(self, interval: timedelta) -> None:
        self.interval = interval
        self._last_allowed: dict[Hashable, datetime] = {}
        self._lock = Lock()

    def check(self, key: Hashable, now: datetime) -> timedelta | None:
        """Record a call for `key` at `now`: `None` if allowed, else the time left to wait."""
        with self._lock:
            last = self._last_allowed.get(key)
            if last is not None and now - last < self.interval:
                return self.interval - (now - last)
            self._last_allowed[key] = now
            if len(self._last_allowed) > _PRUNE_ABOVE:
                self._prune(now)
            return None

    def __len__(self) -> int:
        return len(self._last_allowed)

    def _prune(self, now: datetime) -> None:
        self._last_allowed = {
            key: at for key, at in self._last_allowed.items() if now - at < self.interval
        }
