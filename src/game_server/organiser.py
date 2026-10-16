"""Authorising the organiser's endpoints (the hunt designer): the organiser key, or a token.

The key is one shared secret, `GAME_SERVER_ORGANISER_KEY`, presented as
`Authorization: Bearer <key>`. With accounts, a Clerk session token works too, for a person whose
role is `organiser` or `admin`; the key goes later (#120).
"""

import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, status

from game_server.auth import ForbiddenError, can, current_person, effective_role
from game_server.bearer import bearer_token, is_session_token
from game_server.config import Settings, get_settings
from game_server.errors import ApiError

UNAUTHORISED_BODY = {"detail": "organiser key required", "code": "organiser_unauthorised"}


class OrganiserUnauthorisedError(ApiError):
    """`401`, the same body every time, so a caller can't tell why it was refused."""

    def __init__(self) -> None:
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            UNAUTHORISED_BODY["detail"],
            UNAUTHORISED_BODY["code"],
            headers={"WWW-Authenticate": "Bearer"},
        )


def organiser_key_matches(settings: Settings, authorization: str | None) -> bool:
    """Whether an `Authorization` header presents the organiser key.

    False for a missing header, another scheme, a wrong key, or no key configured. The key is
    compared in constant time and never logged.
    """
    token = bearer_token(authorization)
    if settings.organiser_key is None or token is None:
        return False
    expected = settings.organiser_key.get_secret_value()
    return secrets.compare_digest(token.strip().encode(), expected.encode())


@dataclass(frozen=True)
class OrganiserAccess:
    """Who is organising: the person, or None for the organiser key."""

    pid: UUID | None


def organiser_access(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> OrganiserAccess:
    """Pass with the organiser key, or a valid token whose current role may design hunts.

    A token (a JWT, with accounts on) must be valid (else 401) and its person an organiser or
    admin (else `403 forbidden`). Anything else is taken as a key: the same 401 whatever the
    reason.
    """
    token = bearer_token(authorization)
    if token is not None and settings.auth_mode != "off" and is_session_token(token):
        person = current_person(settings, authorization)
        if not can(effective_role(person, settings), "design"):
            raise ForbiddenError
        return OrganiserAccess(person.pid)
    if not organiser_key_matches(settings, authorization):
        raise OrganiserUnauthorisedError
    return OrganiserAccess(None)


def require_organiser(
    access: Annotated[OrganiserAccess, Depends(organiser_access)],
) -> None:
    """Pass if the request may organise (see `organiser_access`)."""
