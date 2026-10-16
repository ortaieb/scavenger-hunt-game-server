"""Authorising the moderator's endpoints: a moderator's token."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, status

from game_server import errors
from game_server.auth import (
    MODERATOR_ROLES,
    ForbiddenError,
    can,
    current_person,
    effective_role,
)
from game_server.config import Settings, get_settings
from game_server.moderators import SessionModerators, get_session_moderators
from game_server.sessions import GameSession, SessionRepository, get_session_repository


def install(app: FastAPI) -> None:
    """Register the error responses moderator routes use (`ApiError`, including the 401)."""
    errors.install(app)


@dataclass(frozen=True)
class ModeratorAccess:
    """The session a request may moderate, and the moderator's pid."""

    session: GameSession
    pid: UUID


def moderator_access(
    session: UUID,
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    moderators: Annotated[SessionModerators, Depends(get_session_moderators)],
    authorization: Annotated[str | None, Header()] = None,
) -> ModeratorAccess:
    """The session, if a valid token belongs to an admin or a moderator of it.

    Unknown session: 404, as elsewhere. A token (a JWT) must be valid (else 401) and belong to
    an admin, or to an organiser added as a moderator of this session (else `403 forbidden`).
    Roles are read from the directory.
    """
    found = sessions.get_session(session)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    person = current_person(settings, authorization)
    role = effective_role(person, settings)
    added = role in MODERATOR_ROLES and moderators.is_moderator(session, person.pid)
    if not (can(role, "moderate_any_session") or added):
        raise ForbiddenError
    return ModeratorAccess(found, person.pid)


def require_moderator(
    access: Annotated[ModeratorAccess, Depends(moderator_access)],
) -> GameSession:
    """The session, if the request may moderate it (see `moderator_access`)."""
    return access.session
