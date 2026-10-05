"""A team's check-ins at a checkpoint, and when one still holds for a photo.

An arrival is active until it expires or a photo uses it. A photo is held to the team's
active arrival at its checkpoint, and the referee judges the pose that arrival issued.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Arrival:
    """A check-in at a checkpoint, with the one-time code to hold up in the photo."""

    id: int
    checkpoint: int
    code: str
    pose: str | None
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LatestArrival:
    """The team's latest arrival at a checkpoint, and whether a photo has used it.

    A photo uses an arrival by recording it, and any photo the team sent to the checkpoint
    after the arrival was issued ends it too.
    """

    arrival: Arrival
    used: bool

    def active_at(self, at: datetime) -> bool:
        """Whether a photo received at `at` may use it: issued by then, unused, unexpired."""
        return not self.used and self.arrival.issued_at <= at < self.arrival.expires_at

    def expired_at(self, at: datetime) -> bool:
        """Whether it ran out by `at` with no photo sent for it."""
        return not self.used and at >= self.arrival.expires_at
