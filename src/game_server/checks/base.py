"""The check contract (`Check`, `SubmissionContext`, `Rejection`) and verdict decision.

Every verdict is decided here, from the server's own data and clock. The client only
supplies claims (coordinates, capture time, the photo); nothing it sends can mark a check
as passed.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Protocol

from game_server import geo
from game_server.models import ChallengeMetadata, VerdictStatus
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

    @cached_property
    def distance_m(self) -> float:
        """Metres from the submitted (claimed) location to the checkpoint.

        Server-side only: never return it to the client, it would leak the answer.
        """
        return geo.distance_m(self.metadata.location, self.checkpoint.location)


class Check(Protocol):
    """A deterministic rule that can rule a submission out, but never proves it valid."""

    def __call__(self, ctx: SubmissionContext, /) -> Rejection | None: ...


def run_checks(checks: Iterable[Check], ctx: SubmissionContext) -> list[Rejection]:
    """Run every check, without stopping at the first rejection, and collect rejections."""
    return [rejection for check in checks if (rejection := check(ctx)) is not None]


def decide_verdict(rejections: Sequence[Rejection]) -> VerdictStatus:
    """`failed` if any check rejected, otherwise `pending`.

    Never `pass`: the deterministic checks can only rule a submission out. A phone can
    report any location, so passing them doesn't prove presence. `pass` is reserved for
    when presence-proof and visual-challenge checks exist.
    """
    return "failed" if rejections else "pending"
