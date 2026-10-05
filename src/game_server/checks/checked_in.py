"""The team must have checked in at this checkpoint: a photo is held to its active arrival.

Arrive only issues an arrival at the team's current checkpoint, and a photo there ends it,
so this also holds photos to the team's current checkpoint.
"""

from dataclasses import dataclass

from game_server.checks.base import CheckResult, Rejection, SubmissionContext

CHECKED_IN = "checked_in"
CHECK_IN_EXPIRED = Rejection(
    "check_in_expired", "Your check-in ran out. Tap I'm here again, then send your photo."
)
NOT_CHECKED_IN = Rejection(
    "not_checked_in", "Tap I'm here at the checkpoint before sending a photo."
)


@dataclass(frozen=True)
class CheckedInCheck:
    """Passes when the team had an active arrival at the checkpoint at `received_at`."""

    def __call__(self, ctx: SubmissionContext, /) -> CheckResult:
        """`detail` names the arrival, or why none was active; never its code."""
        latest, at = ctx.arrival, ctx.received_at
        if latest is None:
            return _failed(NOT_CHECKED_IN, "no arrival at this checkpoint")
        arrival = latest.arrival
        if latest.active_at(at):
            return CheckResult(
                CHECKED_IN,
                "passed",
                1.0,
                "You checked in at this checkpoint.",
                detail=f"arrival {arrival.id}",
            )
        if latest.expired_at(at):
            detail = f"arrival {arrival.id} expired at {arrival.expires_at.isoformat()}"
            return _failed(CHECK_IN_EXPIRED, detail)
        return _failed(NOT_CHECKED_IN, f"arrival {arrival.id} already used by a photo")


def _failed(rejection: Rejection, detail: str) -> CheckResult:
    return CheckResult(CHECKED_IN, "failed", 1.0, rejection.message, rejection, detail=detail)
