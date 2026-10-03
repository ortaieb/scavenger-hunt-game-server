"""`POST /join`: a team joins its session with its code and the player's photo consent.

Joining returns the team's `participant` id, used on every later call. The id is random,
returned only to its team, and works as the team's key for the demo; it isn't a signed
token, and the server log shows it. Join codes are credentials: never returned or logged.
"""

import logging
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from game_server.clock import Clock, get_clock
from game_server.errors import ApiError
from game_server.models import JoinedSession, JoinRequest, JoinResponse
from game_server.session_runs import session_phase
from game_server.sessions import GameSession, SessionRepository, Team, get_session_repository
from game_server.submissions import (
    ParticipantRecord,
    SubmissionStore,
    get_submission_store,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class Participant:
    """A joined team: its row and its current definition in the sessions file."""

    record: ParticipantRecord
    session: GameSession
    team: Team


def find_participant(
    store: SubmissionStore, sessions: SessionRepository, session_id: UUID, participant_id: UUID
) -> Participant | None:
    """The participant and its team, or None if it never joined this session or its team
    is no longer in the sessions file."""
    record = store.find_participant(session_id, participant_id)
    session = sessions.get_session(session_id)
    if record is None or session is None:
        return None
    team = sessions.get_team(session_id, record.team)
    if team is None:
        return None
    return Participant(record, session, team)


@router.post(
    "/join",
    status_code=status.HTTP_200_OK,
    responses={
        201: {
            "model": JoinResponse,
            "description": "First join: the team's participant is created",
        },
        200: {
            "description": "The team had joined before: same participant, consent recorded again"
        },
        404: {"description": "No team has this code"},
        409: {"description": "The session has ended (code: session_stopped)"},
        422: {"description": "Invalid body, including consent that isn't true"},
    },
)
def join(
    body: JoinRequest,
    response: Response,
    clock: Annotated[Clock, Depends(get_clock)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> JoinResponse:
    """Join the team the code belongs to, recording the player's consent.

    Joining before the moderator starts the session is fine: teams join first, then the
    game starts. Only a stopped session refuses; the file's end-time doesn't.
    """
    now = clock().astimezone(UTC)
    found = sessions.find_team(body.code)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown code")
    session, team = found
    if session_phase(store.session_run(session.id)) == "stopped":
        store.record_blocked(session.id, team.name, "join", "session_stopped", now)
        raise ApiError(status.HTTP_409_CONFLICT, "session has ended", "session_stopped")
    outcome = store.join_team(session.id, team.name, now)
    logger.info(
        "Team joined session %s team %s participant %s first=%s",
        session.id,
        team.name,
        outcome.participant,
        "yes" if outcome.first else "no",
    )
    if outcome.first:
        response.status_code = status.HTTP_201_CREATED
    return JoinResponse(
        participant=outcome.participant,
        team=team.name,
        session=JoinedSession(
            id=session.id,
            name=session.name,
            location=session.location,
            start_time=session.start_time,
            end_time=session.end_time,
        ),
        checkpoints=len(session.checkpoints),
    )
