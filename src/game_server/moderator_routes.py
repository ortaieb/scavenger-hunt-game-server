"""`POST /sessions/{session}/moderators` and `DELETE …/moderators/{pid}`: who moderates a session.

A moderator is added by email address: the users directory resolves it to a `pid`, and only the
`pid` is stored (and logged). Only organisers and admins can moderate. Admins can change any
session's moderators, an organiser those of the sessions they moderate; the organiser key works
too until it goes (#120).
"""

import logging
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from game_server.auth import (
    MODERATOR_ROLES,
    ForbiddenError,
    UnauthorisedError,
    can,
    current_person,
    effective_role,
)
from game_server.bearer import bearer_token, is_session_token
from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.directory.routes import directory_and_outbox
from game_server.errors import ApiError
from game_server.moderators import SessionModerators, get_session_moderators
from game_server.organiser import organiser_key_matches
from game_server.sessions import SessionRepository, get_session_repository

logger = logging.getLogger(__name__)

router = APIRouter()

RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "No valid token or organiser key (code: unauthorised)"},
    403: {"description": "Neither an admin nor a moderator of this session (code: forbidden)"},
    404: {"description": "Unknown session, or no such account or moderator"},
}


@dataclass(frozen=True)
class ModeratorManager:
    """Who is changing a session's moderators: a person, or None for the organiser key."""

    session: UUID
    pid: UUID | None


def moderator_manager(
    session: UUID,
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    moderators: Annotated[SessionModerators, Depends(get_session_moderators)],
    authorization: Annotated[str | None, Header()] = None,
) -> ModeratorManager:
    """The caller, if they may change the session's moderators; else 404, 401 or 403."""
    if sessions.get_session(session) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    token = bearer_token(authorization)
    if token is not None and settings.auth_mode != "off" and is_session_token(token):
        person = current_person(settings, authorization)
        role = effective_role(person, settings)
        moderates = role in MODERATOR_ROLES and moderators.is_moderator(session, person.pid)
        if not (can(role, "moderate_any_session") or moderates):
            raise ForbiddenError
        return ModeratorManager(session, person.pid)
    if organiser_key_matches(settings, authorization):
        return ModeratorManager(session, None)
    raise UnauthorisedError


class ModeratorIn(BaseModel):
    """The new moderator's email address, as on their account."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)


class ModeratorOut(BaseModel):
    """A session's moderator, by pid."""

    session: UUID
    pid: UUID


@router.post(
    "/sessions/{session}/moderators",
    status_code=status.HTTP_200_OK,
    responses={
        **RESPONSES,
        201: {"description": "Added"},
        200: {"description": "Was a moderator already"},
        409: {"description": "The account isn't an organiser or admin (code: not_an_organiser)"},
    },
)
def add_moderator(
    body: ModeratorIn,
    response: Response,
    manager: Annotated[ModeratorManager, Depends(moderator_manager)],
    settings: Annotated[Settings, Depends(get_settings)],
    moderators: Annotated[SessionModerators, Depends(get_session_moderators)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> ModeratorOut:
    """Make the account with this email address a moderator of the session."""
    directory, _ = directory_and_outbox(settings)
    pid = directory.find_pid_by_email(body.email)
    if pid is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "no account with that email", "no_account")
    if directory.role(pid) not in MODERATOR_ROLES:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "only an organiser or admin can moderate",
            "not_an_organiser",
        )
    if moderators.add(manager.session, pid, manager.pid, clock().astimezone(UTC)):
        response.status_code = status.HTTP_201_CREATED
        logger.info("Person %s moderates session %s", pid, manager.session)
    return ModeratorOut(session=manager.session, pid=pid)


@router.delete(
    "/sessions/{session}/moderators/{pid}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=RESPONSES,
)
def remove_moderator(
    pid: UUID,
    manager: Annotated[ModeratorManager, Depends(moderator_manager)],
    moderators: Annotated[SessionModerators, Depends(get_session_moderators)],
) -> Response:
    """Stop the person moderating the session."""
    if not moderators.remove(manager.session, pid):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not a moderator", "not_a_moderator")
    logger.info("Person %s no longer moderates session %s", pid, manager.session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
