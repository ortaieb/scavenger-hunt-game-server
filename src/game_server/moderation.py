"""Authorising the moderator's endpoints with the session's moderator code.

A stopgap until real accounts exist: each session's moderator code (from the sessions file)
is a shared secret presented as `Authorization: Bearer <code>`. Moderator routes live under
`/sessions/{session}/…` and depend on `require_moderator`.
"""

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, status

from game_server import errors
from game_server.errors import ApiError
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


def bearer_token(authorization: str | None) -> str | None:
    """The token of `Bearer <token>` (the scheme is case-insensitive), else None."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        return None
    return token


def require_moderator(
    session: UUID,
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    authorization: Annotated[str | None, Header()] = None,
) -> GameSession:
    """The session, if the request presents its moderator code; else 404 or 401.

    Unknown session: 404, as elsewhere. Missing header, another scheme, a wrong code or a
    session without a code: 401. The code works for its own session only, is compared in
    constant time, and is never logged.
    """
    found = sessions.get_session(session)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    expected = sessions.moderator_code(session)
    token = bearer_token(authorization)
    if expected is None or token is None:
        raise ModeratorUnauthorisedError
    if not secrets.compare_digest(normalise_join_code(token).encode(), expected.encode()):
        raise ModeratorUnauthorisedError
    return found
