"""Authorising the organiser's endpoints (the hunt designer) with the organiser key.

A stopgap like the moderator code, until real accounts exist. The moderator code can't do it:
it belongs to a session, and a draft exists before any session does. The key is one shared
secret, `GAME_SERVER_ORGANISER_KEY`, presented as `Authorization: Bearer <key>`.
"""

import secrets
from typing import Annotated

from fastapi import Depends, Header, status

from game_server.config import Settings, get_settings
from game_server.errors import ApiError
from game_server.moderation import bearer_token

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


def require_organiser(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Pass if the request presents the organiser key; else the same 401, whatever the reason."""
    if not organiser_key_matches(settings, authorization):
        raise OrganiserUnauthorisedError
