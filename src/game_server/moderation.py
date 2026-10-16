"""Authorising the moderator's endpoints: the session's moderator code, or a moderator's token.

Each session's moderator code (from the sessions file) is a shared secret presented as
`Authorization: Bearer <code>`. With accounts, a Clerk session token works too, for an admin or
for an organiser added as one of the session's moderators; the codes go later (#120).
Moderator routes live under `/sessions/{session}/…` and depend on `require_moderator`.
"""

import secrets
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
from game_server.bearer import bearer_token, is_session_token
from game_server.config import Settings, get_settings
from game_server.errors import ApiError
from game_server.moderators import SessionModerators, get_session_moderators
from game_server.sessions import (
    GameSession,
    SessionRepository,
    get_session_repository,
    normalise_join_code,
)

UNAUTHORISED_BODY = {"detail": "moderator code required", "code": "moderator_unauthorised"}


class ModeratorUnauthorisedError(ApiError):
    """`401`, the same body every time, so a caller can't tell why it was refused."""

    def __init__(self) -> None:
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            UNAUTHORISED_BODY["detail"],
            UNAUTHORISED_BODY["code"],
            headers={"WWW-Authenticate": "Bearer"},
        )


def install(app: FastAPI) -> None:
    """Register the error responses moderator routes use (`ApiError`, including the 401)."""
    errors.install(app)


@dataclass(frozen=True)
class ModeratorAccess:
    """The session a request may moderate, and the person moderating it (None for the code)."""

    session: GameSession
    pid: UUID | None


def moderator_access(
    session: UUID,
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    moderators: Annotated[SessionModerators, Depends(get_session_moderators)],
    authorization: Annotated[str | None, Header()] = None,
) -> ModeratorAccess:
    """The session, if the request presents its moderator code or a moderator's Clerk token.

    Unknown session: 404, as elsewhere. A token (a JWT: with accounts on) must be valid (else
    401) and belong to an admin, or to an organiser added as a moderator of this session (else
    `403 forbidden`); roles are read from the directory when there is one. Anything else is
    taken as a code: missing header, another scheme, a wrong code or a session without a code
    get 401. The code works for its own session only, is compared in constant time, and is
    never logged.
    """
    found = sessions.get_session(session)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    token = bearer_token(authorization)
    if token is not None and settings.auth_mode != "off" and is_session_token(token):
        person = current_person(settings, authorization)
        role = effective_role(person, settings)
        added = role in MODERATOR_ROLES and moderators.is_moderator(session, person.pid)
        if not (can(role, "moderate_any_session") or added):
            raise ForbiddenError
        return ModeratorAccess(found, person.pid)
    expected = sessions.moderator_code(session)
    if expected is None or token is None:
        raise ModeratorUnauthorisedError
    if not secrets.compare_digest(normalise_join_code(token).encode(), expected.encode()):
        raise ModeratorUnauthorisedError
    return ModeratorAccess(found, None)


def require_moderator(
    access: Annotated[ModeratorAccess, Depends(moderator_access)],
) -> GameSession:
    """The session, if the request may moderate it (see `moderator_access`)."""
    return access.session
