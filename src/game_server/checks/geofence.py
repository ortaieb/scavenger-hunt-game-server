"""Geofence rule: the submitted coordinates must be within the checkpoint's proximity.

The coordinates are a claim, not proof: a phone can report any location. So this rule can
rule a submission out (the claim itself says "not here"), but passing it proves nothing
about presence.

The fence is never widened: there is no GPS-accuracy allowance, and the client can't send
one. If radii prove too tight, the moderator widens `proximity` in the sessions file.
"""

from dataclasses import dataclass

from game_server.checks.base import Rejection, SubmissionContext

# No distance, direction or coordinates, so retries can't be played as hot/cold.
OUT_OF_RANGE = Rejection("out_of_range", "Your location is outside the checkpoint area.")


@dataclass(frozen=True)
class GeofenceCheck:
    """Rejects when the submitted location is farther than `proximity` from the checkpoint."""

    def __call__(self, ctx: SubmissionContext, /) -> Rejection | None:
        """The boundary itself counts as in range."""
        if ctx.distance_m > ctx.checkpoint.proximity:
            return OUT_OF_RANGE
        return None
