"""`GET /sessions/{session}/overview`: the moderator's view of a session.

The session clock, how many photos wait for the moderator's ruling, every team's standing and
progress (so the moderator can spot a team that's stuck), and the teams that tried to play
outside the session. Moderator only, so it names checkpoints; it never shows coordinates,
clues, scenes, photos or codes.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from game_server.clock import Clock, get_clock, utc_iso
from game_server.game_state import team_state
from game_server.models import VerdictStatus
from game_server.moderation import require_moderator
from game_server.scoring import SessionResults, places, results_final, team_points
from game_server.session_control import SessionClock, session_clock
from game_server.session_runs import SessionRun, session_phase
from game_server.sessions import GameSession, Team
from game_server.submissions import (
    BlockedAction,
    CompletingSubmission,
    PhaseCode,
    SubmissionStore,
    get_submission_store,
)

router = APIRouter()

BLOCKED_SHOWN = 50


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


class LastCompletedOut(_KebabModel):
    """The team's most recent photo that completed a checkpoint: which one, its effective
    verdict and its receive time."""

    sequence: int
    name: str
    verdict: VerdictStatus
    at: str


class CurrentOut(_KebabModel):
    """The checkpoint the team is on."""

    sequence: int
    name: str


class TeamOverviewOut(_KebabModel):
    """A team's standing and progress. Score fields are `null` for a team that hasn't joined.

    `points`, `in-review` and `place` are exactly what the team sees in its own state.
    """

    team: str
    joined: bool
    completed: int
    total: int
    points: int | None
    in_review: int | None
    place: int | None
    last_completed: LastCompletedOut | None
    current: CurrentOut | None


class BlockedOut(_KebabModel):
    """A team that tried to play outside the session."""

    at: str
    team: str
    action: BlockedAction
    code: PhaseCode


class OverviewOut(_KebabModel):
    """The session clock, the photos to review, the teams (standings first) and the newest
    blocked attempts.

    `to-review` counts the session's `pending` photos the moderator hasn't ruled on yet.
    """

    session: SessionClock
    to_review: int
    teams: list[TeamOverviewOut]
    blocked: list[BlockedOut]


@dataclass(frozen=True)
class SessionSnapshot:
    """What the overview reads, at one moment."""

    session: GameSession
    run: SessionRun | None
    results: SessionResults
    completing: Sequence[CompletingSubmission]
    now: datetime


def _last_completed(snapshot: SessionSnapshot, team: Team) -> LastCompletedOut | None:
    on_route = [
        c for c in snapshot.completing if c.team == team.name and c.checkpoint in team.order
    ]
    if not on_route:
        return None
    last = max(on_route, key=lambda completing: completing.received_at)
    return LastCompletedOut(
        sequence=last.checkpoint,
        name=_checkpoint_name(snapshot.session, last.checkpoint),
        verdict=last.verdict,
        at=utc_iso(last.received_at),
    )


def _checkpoint_name(session: GameSession, sequence: int) -> str:
    return next(c.name for c in session.checkpoints if c.sequence == sequence)


def team_overview(
    snapshot: SessionSnapshot, team: Team, final_places: dict[str, int] | None
) -> TeamOverviewOut:
    """One team's row: unjoined teams have no score, progress or current checkpoint."""
    if team.name not in snapshot.results.joined:
        return TeamOverviewOut(
            team=team.name,
            joined=False,
            completed=0,
            total=len(team.order),
            points=None,
            in_review=None,
            place=None,
            last_completed=None,
            current=None,
        )
    completed = {c.checkpoint for c in snapshot.completing if c.team == team.name}
    state = team_state(snapshot.session, team, completed, snapshot.now, snapshot.run)
    points = team_points(team, snapshot.results)
    current = state.current
    return TeamOverviewOut(
        team=team.name,
        joined=True,
        completed=state.completed,
        total=state.total,
        points=points.points,
        in_review=points.in_review,
        place=final_places[team.name] if final_places is not None else None,
        last_completed=_last_completed(snapshot, team),
        current=CurrentOut(
            sequence=current.sequence, name=_checkpoint_name(snapshot.session, current.sequence)
        )
        if current
        else None,
    )


def standings(snapshot: SessionSnapshot) -> list[TeamOverviewOut]:
    """Every team in the file: joined teams by points then name, then the rest by name."""
    joined = [t for t in snapshot.session.teams if t.name in snapshot.results.joined]
    final_places = None
    if results_final(snapshot.results, session_phase(snapshot.run)):
        final_places = places({t.name: team_points(t, snapshot.results).points for t in joined})
    rows = [team_overview(snapshot, team, final_places) for team in snapshot.session.teams]
    return sorted(rows, key=lambda row: (not row.joined, row.points or 0, row.team))


@router.get(
    "/sessions/{session}/overview",
    responses={
        401: {"description": "Moderator code required (code: moderator_unauthorised)"},
        404: {"description": "Unknown session"},
    },
)
def session_overview(
    session: Annotated[GameSession, Depends(require_moderator)],
    clock: Annotated[Clock, Depends(get_clock)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> OverviewOut:
    """The session clock, the photos to review, standings with each team's progress, and
    blocked attempts."""
    now = clock().astimezone(UTC)
    snapshot = SessionSnapshot(
        session=session,
        run=store.session_run(session.id),
        results=store.session_results(session.id),
        completing=store.completing_submissions(session.id),
        now=now,
    )
    return OverviewOut(
        session=session_clock(session, snapshot.run, utc_iso(now)),
        to_review=snapshot.results.to_review,
        teams=standings(snapshot),
        blocked=[
            BlockedOut(at=utc_iso(b.at), team=b.team, action=b.action, code=b.code)
            for b in store.blocked_attempts(session.id, BLOCKED_SHOWN)
        ],
    )
