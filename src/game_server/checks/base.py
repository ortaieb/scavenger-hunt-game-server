"""The check contract (`Check`, `SubmissionContext`, `CheckResult`) and verdict decision.

Every verdict is decided here, from the server's own data and clock. The client only
supplies claims (coordinates, capture time, the photo); nothing it sends can mark a check
as passed.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Protocol, Self

from game_server import geo
from game_server.models import ChallengeMetadata, CheckOutcome, VerdictStatus
from game_server.referee import RefereeReport
from game_server.sessions import Checkpoint, GameSession


@dataclass(frozen=True)
class Rejection:
    """Why a check ruled a submission out.

    `code` is stable snake_case (e.g. `outside_window`) for clients to branch on.
    `message` is shown to the player, so it must never contain checkpoint coordinates,
    distances or bearings.
    """

    code: str
    message: str


@dataclass(frozen=True)
class AcceptedPhoto:
    """A photo from a submission in the same session whose verdict is not `failed`."""

    submission_id: int
    phash: int


@dataclass(frozen=True)
class SubmissionContext:
    """Everything a check may look at for one submission.

    Frozen so a check can't alter what later checks see. For values that several checks
    share and that are costly to compute (e.g. the decoded image), add a
    `functools.cached_property` here: it is computed once, on first use, and works on a
    frozen dataclass.
    """

    metadata: ChallengeMetadata
    received_at: datetime
    session: GameSession
    checkpoint: Checkpoint
    image: bytes
    phash: int
    # Snapshot taken inside the write transaction the submission is recorded in, so it
    # can't go stale before the insert. Only ever this session's photos.
    accepted_photos: tuple[AcceptedPhoto, ...] = ()
    # The referee's report, or None when the referee wasn't consulted (a check before it
    # failed, or the checkpoint has no visual challenge). Filled in before the transaction.
    referee_report: RefereeReport | None = None

    @cached_property
    def distance_m(self) -> float:
        """Metres from the submitted (claimed) location to the checkpoint.

        Server-side only: never return it to the client, it would leak the answer.
        """
        return geo.distance_m(self.metadata.location, self.checkpoint.location)


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check on one submission.

    `check` is a stable snake_case name phrased as a positive assertion (`in_range`).
    `reason` is shown to the player, under the same rules as `Rejection.message`: no
    coordinates, distances, bearings or window times. `detail` is for moderators only:
    it is stored, never returned. `rejection` is set exactly when the check `failed`.
    """

    check: str
    outcome: CheckOutcome
    confidence: float
    reason: str
    rejection: Rejection | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        if (self.outcome == "failed") != (self.rejection is not None):
            raise ValueError("a rejection is required for, and only for, a failed check")

    @classmethod
    def passed(cls, check: str, reason: str) -> Self:
        """A deterministic pass: confidence 1.0."""
        return cls(check, "passed", 1.0, reason)

    @classmethod
    def failed(cls, check: str, rejection: Rejection) -> Self:
        """A deterministic failure: confidence 1.0, reason = the rejection's message."""
        return cls(check, "failed", 1.0, rejection.message, rejection)


class InTransaction:
    """Marker for checks that run inside the submission's write transaction.

    They need the accepted-photo snapshot or the referee's report, so they run after the
    referee, under the lock. Checks without the marker run first, outside the lock; if any
    of them fails the referee isn't consulted. The registry lists unmarked checks first.
    """


class Check(Protocol):
    """A rule that can rule a submission out; a deterministic pass never proves it valid."""

    def __call__(self, ctx: SubmissionContext, /) -> CheckResult: ...


def run_checks(checks: Iterable[Check], ctx: SubmissionContext) -> list[CheckResult]:
    """Run every check, in order, without stopping at the first failure."""
    return [check(ctx) for check in checks]


def rejections(results: Iterable[CheckResult]) -> list[Rejection]:
    """The failed results' rejections, in check order."""
    return [result.rejection for result in results if result.rejection is not None]


def decide_verdict(results: Sequence[CheckResult]) -> VerdictStatus:
    """`failed` if any check failed; `pass` if every check passed; otherwise `pending`.

    `pending` means a moderator reviews it: some check was `uncertain` or `skipped` (for
    example the referee is disabled, or the checkpoint has no visual challenge). With no
    checks at all nothing was verified, so that's `pending` too, never a vacuous `pass`.
    """
    if any(result.outcome == "failed" for result in results):
        return "failed"
    if results and all(result.outcome == "passed" for result in results):
        return "pass"
    return "pending"
