"""The moderator starts and finishes a session: `POST /sessions/{session}/start` and `/stop`.

The sessions file's start-time and end-time are only the planned window: shown to players
and used for invitations, never enforced. A session can start early or late and run past its
planned end. Stopping is final.
"""

import logging
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from game_server.clock import Clock, get_clock, utc_iso
from game_server.errors import ApiError
from game_server.moderation import require_moderator
from game_server.session_runs import SessionPhase, SessionRun, session_phase
from game_server.sessions import GameSession
from game_server.submissions import SubmissionStore, get_submission_store

logger = logging.getLogger(__name__)

router = APIRouter()


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class SessionClock(BaseModel):
    """Where the session stands, with its planned window and the server's time.

    `planned-start`/`planned-end` are the file's times, for display only. `server-time` lets
    the app correct for a phone whose clock is off. Times are UTC to the second.
    """

    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)

    phase: SessionPhase
    planned_start: str
    planned_end: str
    started_at: str | None
    stopped_at: str | None
    server_time: str


def session_clock(session: GameSession, run: SessionRun | None, now_iso: str) -> SessionClock:
    """The clock for a session, given its run."""
    return SessionClock(
        phase=session_phase(run),
        planned_start=utc_iso(session.start_time),
        planned_end=utc_iso(session.end_time),
        started_at=utc_iso(run.started_at) if run and run.started_at else None,
        stopped_at=utc_iso(run.stopped_at) if run and run.stopped_at else None,
        server_time=now_iso,
    )


SESSION_STOPPED = ("session has ended", "session_stopped")
SESSION_NOT_STARTED = ("session hasn't started", "session_not_started")


@router.post(
    "/sessions/{session}/start",
    status_code=status.HTTP_200_OK,
    responses={
        201: {"model": SessionClock, "description": "Started now"},
        200: {"description": "Already running: the same clock"},
        401: {"description": "Moderator code required"},
        404: {"description": "Unknown session"},
        409: {"description": "The session has ended (code: session_stopped)"},
    },
)
def start_session(
    session: Annotated[GameSession, Depends(require_moderator)],
    response: Response,
    clock: Annotated[Clock, Depends(get_clock)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> SessionClock:
    """Start the session: `scheduled` → `running`. Moderator only."""
    now = clock().astimezone(UTC)
    change = store.start_run(session.id, now)
    if session_phase(change.run) == "stopped":
        raise ApiError(status.HTTP_409_CONFLICT, *SESSION_STOPPED)
    if change.changed:
        logger.info("Session %s phase running started_at %s", session.id, utc_iso(now))
        response.status_code = status.HTTP_201_CREATED
    return session_clock(session, change.run, utc_iso(now))


@router.post(
    "/sessions/{session}/stop",
    status_code=status.HTTP_200_OK,
    responses={
        201: {"model": SessionClock, "description": "Stopped now"},
        200: {"description": "Already stopped: the same clock"},
        401: {"description": "Moderator code required"},
        404: {"description": "Unknown session"},
        409: {"description": "The session hasn't started (code: session_not_started)"},
    },
)
def stop_session(
    session: Annotated[GameSession, Depends(require_moderator)],
    response: Response,
    clock: Annotated[Clock, Depends(get_clock)],
    store: Annotated[SubmissionStore, Depends(get_submission_store)],
) -> SessionClock:
    """Finish the session: `running` → `stopped`, for good. Moderator only."""
    now = clock().astimezone(UTC)
    change = store.stop_run(session.id, now)
    if session_phase(change.run) == "scheduled":
        raise ApiError(status.HTTP_409_CONFLICT, *SESSION_NOT_STARTED)
    if change.changed:
        logger.info("Session %s phase stopped stopped_at %s", session.id, utc_iso(now))
        response.status_code = status.HTTP_201_CREATED
    return session_clock(session, change.run, utc_iso(now))
