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


def require_organiser(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Pass if the request presents the organiser key; else 401.

    A missing header, another scheme, a wrong key, or no key configured: all the same 401.
    The key is compared in constant time and never logged.
    """
    token = bearer_token(authorization)
    if settings.organiser_key is None or token is None:
        raise OrganiserUnauthorisedError
    expected = settings.organiser_key.get_secret_value()
    if not secrets.compare_digest(token.strip().encode(), expected.encode()):
        raise OrganiserUnauthorisedError
