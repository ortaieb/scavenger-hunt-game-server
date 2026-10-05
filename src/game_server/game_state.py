"""`GET /sessions/{session}/participants/{participant}/state`: where a team stands.

A team sees one clue at a time: the clue for the next checkpoint on its own route. With
per-team orders, one team's later clue is another team's current one, so the server never
hands out a clue before it's that team's turn.
"""

from collections.abc import Set
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from game_server.checks.time_window import window_is_open
from game_server.clock import Clock, get_clock, utc_iso
from game_server.join import find_participant
from game_server.scoring import SessionResults, places, results_final, team_points
from game_server.session_control import SessionClock, session_clock
from game_server.session_runs import SessionRun, session_phase
from game_server.sessions import GameSession, SessionRepository, Team, get_session_repository
from game_server.submissions import SubmissionStore, get_submission_store

router = APIRouter()

TeamStatus = Literal["not_started", "playing", "finished", "ended"]


@dataclass(frozen=True)
class CurrentCheckpoint:
    """The team's next checkpoint: the first on its route it hasn't completed."""

    sequence: int
    position: int  # its place on the team's route, counting from 1
    clue: str
    open: bool  # whether its effective window is open now (never when it opens or closes)


@dataclass(frozen=True)
class TeamState:
    """Where a team stands."""

    status: TeamStatus
    completed: int
    total: int
    current: CurrentCheckpoint | None


def team_state(
    session: GameSession,
    team: Team,
    completed: Set[int],
    now: datetime,
    run: SessionRun | None,
) -> TeamState:
    """Work out the team's state from the checkpoints it has completed and the session's run.

    `finished` once every checkpoint on its route is completed (even after a stop);
    otherwise `not_started` until the moderator starts the session, `ended` once they stop
    it, else `playing` with the first checkpoint on its route it hasn't completed. The
    file's planned times play no part.
    """
    route = team.order
    done = sum(1 for sequence in route if sequence in completed)
    if done == len(route):
        return TeamState("finished", done, len(route), None)
    phase = session_phase(run)
    if phase == "scheduled":
        return TeamState("not_started", done, len(route), None)
    if phase == "stopped":
        return TeamState("ended", done, len(route), None)
    position, sequence = next(
        (index, sequence)
        for index, sequence in enumerate(route, start=1)
        if sequence not in completed
    )
    checkpoint = next(c for c in session.checkpoints if c.sequence == sequence)
    current = CurrentCheckpoint(
        sequence=sequence,
        position=position,
        clue=checkpoint.clue,
        open=window_is_open(run, checkpoint, now),
    )
    return TeamState("playing", done, len(route), current)


class ProgressOut(BaseModel):
    """How many checkpoints on the team's route it has completed, out of how many."""

    completed: int
    total: int


class CurrentOut(BaseModel):
    """The current checkpoint: its sequence, place on the route, clue and whether it's open."""

    sequence: int
    position: int
    clue: str
    open: bool


class ScoreOut(BaseModel):
    """The team's own total only: never another team's, nor a per-checkpoint breakdown.

    `points` is what the team would score if the session finished now; lower is better.
    `place` is its final position among the teams that joined, set once the results are
    `final`: the session has stopped and the moderator has ruled on every `pending` photo.
    """

    model_config = ConfigDict(validate_by_name=True)

    points: int
    in_review: int = Field(alias="in-review")
    final: bool
    place: int | None


class TeamStateOut(BaseModel):
    """Only the current clue: never other clues, names, places, times or the route."""

    status: TeamStatus
    team: str
    progress: ProgressOut
    current: CurrentOut | None
    session: SessionClock
    score: ScoreOut


def team_score(session: GameSession, team: Team, results: SessionResults, final: bool) -> ScoreOut:
    """The team's score; its place among the joined teams once the session is `final`."""
    own = team_points(team, results)
    place = None
    if final:
        totals = {
            other.name: team_points(other, results).points
            for other in session.teams
            if other.name in results.joined
        }
        place = places(totals)[team.name]
    return ScoreOut(points=own.points, in_review=own.in_review, final=final, place=place)


@router.get(
    "/sessions/{session}/participants/{participant}/state",
    responses={404: {"description": "Unknown session, or a participant that didn't join it"}},
)
def participant_state(
    session: UUID,
    participant: UUID,
    clock: Annotated[Clock, Depends(get_clock)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> TeamStateOut:
    """The team's status, progress and, while playing, its current clue."""
    if sessions.get_session(session) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    joined = find_participant(store, sessions, session, participant)
    if joined is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown participant")
    completed = store.completed_checkpoints(session, participant)
    run = store.session_run(session)
    results = store.session_results(session)
    now = clock().astimezone(UTC)
    state = team_state(joined.session, joined.team, completed, now, run)
    final = results_final(results, session_phase(run))
    current = state.current
    return TeamStateOut(
        status=state.status,
        team=joined.team.name,
        progress=ProgressOut(completed=state.completed, total=state.total),
        current=CurrentOut(
            sequence=current.sequence,
            position=current.position,
            clue=current.clue,
            open=current.open,
        )
        if current
        else None,
        session=session_clock(joined.session, run, utc_iso(now)),
        score=team_score(joined.session, joined.team, results, final),
    )
