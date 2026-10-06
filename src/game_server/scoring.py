"""Points by order of arrival: lowest total wins.

Scoring goes by each photo's effective verdict: the moderator's ruling if there is one
(approve → `pass`, reject → `failed`), else the referee's verdict. At each checkpoint on its
route, a team with a `pass` photo scores its place there: 1 if its first `pass` photo was
received first among all teams, 2 if second, and so on; equal times share a place. An
approved photo takes its place by when it was received, not when it was ruled. Every other
checkpoint counts N+1, N being the number of teams that joined. A `pending` photo scores
nothing until it's ruled on. The order is set by the server's receive time of the photo,
never by the arrive tap, which takes no location.

So a team's total is always what it would score if the session finished now.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from game_server.session_runs import SessionPhase
from game_server.sessions import Team


@dataclass(frozen=True)
class SessionResults:
    """What scoring needs from a session's submissions, by effective verdict, keyed by team."""

    joined: frozenset[str]
    # (team, checkpoint) → `received_at` of the team's first `pass` there.
    passes: Mapping[tuple[str, int], datetime]
    # (team, checkpoint) with an unruled `pending` photo, whether or not there's also a `pass`.
    pending: frozenset[tuple[str, int]]
    # The session's unruled `pending` photos, from any participant: what's left to review.
    to_review: int = 0


@dataclass(frozen=True)
class TeamPoints:
    """A team's total, and how many of its checkpoints wait on the moderator's review."""

    points: int
    in_review: int


def checkpoint_place(results: SessionResults, team: str, checkpoint: int) -> int | None:
    """The team's place at a checkpoint by its first `pass`, or None without one."""
    received_at = results.passes.get((team, checkpoint))
    if received_at is None:
        return None
    earlier = sum(
        1
        for (other, sequence), at in results.passes.items()
        if sequence == checkpoint and other in results.joined and at < received_at
    )
    return earlier + 1


def team_points(team: Team, results: SessionResults) -> TeamPoints:
    """The team's points over the checkpoints on its route."""
    unplaced = len(results.joined) + 1
    points = in_review = 0
    for checkpoint in team.order:
        place = checkpoint_place(results, team.name, checkpoint)
        points += unplaced if place is None else place
        if place is None and (team.name, checkpoint) in results.pending:
            in_review += 1
    return TeamPoints(points=points, in_review=in_review)


def results_final(results: SessionResults, phase: SessionPhase) -> bool:
    """Whether the places are final: the session has stopped, and no `pending` photo in it
    is left unruled."""
    return phase == "stopped" and results.to_review == 0


def places(points: Mapping[str, int]) -> dict[str, int]:
    """Each team's position, lowest points first; ties share a place (1, 1, 3)."""
    totals = list(points.values())
    return {team: 1 + sum(1 for other in totals if other < total) for team, total in points.items()}
