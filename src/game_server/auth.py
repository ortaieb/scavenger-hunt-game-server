"""Who is calling: checking the Clerk session token a signed-in player's request carries.

The game-server decides verdicts and scores, so it checks each token itself rather than trusting
the web app, and from then on knows the caller only by `pid`, the users directory's random person
id, which the token carries. Verification is local: Clerk's PEM public key, no network call.

`current_person` and `current_clerk_user` are dependencies for routes to adopt. They always check
the token; whether a route requires one is for the route to decide from `Settings.auth_mode`.
Nothing here logs a token, a claim or a Clerk user id.
"""

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, Header, status

from game_server.config import Settings, get_settings
from game_server.errors import ApiError
from game_server.moderation import bearer_token

ROLES = ("player", "organiser", "admin")
LEEWAY_SECONDS = 5
REQUIRED_CLAIMS = ["exp", "nbf", "iat", "iss", "sub", "sid", "azp"]
UNAUTHORISED_BODY = {"detail": "sign-in required", "code": "unauthorised"}


class UnauthorisedError(ApiError):
    """`401`, the same body every time, so a caller can't tell why it was refused."""

    def __init__(self) -> None:
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            UNAUTHORISED_BODY["detail"],
            UNAUTHORISED_BODY["code"],
            headers={"WWW-Authenticate": "Bearer"},
        )


class ProvisioningRequiredError(ApiError):
    """`401`: a valid token with no person id yet, so the web app provisions one and retries."""

    def __init__(self) -> None:
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "no person yet: provision one",
            "provisioning_required",
            headers={"WWW-Authenticate": "Bearer"},
        )


@dataclass(frozen=True)
class Principal:
    """The signed-in caller: the person's id, their role and the Clerk session they used."""

    pid: UUID
    role: str
    session_id: str


def verify_session_token(token: str, settings: Settings) -> dict[str, Any]:
    """The token's claims, if it is a current Clerk session token for this server; else 401.

    RS256 only, whatever the token's own `alg` says. Its issuer is ours, its `azp` is one of the
    web app's origins (so a token minted for another site can't be replayed here) and it isn't
    `pending` (sign-in unfinished).
    """
    if not settings.clerk_jwt_key or not settings.clerk_issuer:
        raise UnauthorisedError  # not configured: nothing can be verified
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.clerk_jwt_key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError:
        raise UnauthorisedError from None
    if claims["azp"] not in settings.clerk_authorized_parties or claims.get("sts") == "pending":
        raise UnauthorisedError
    return claims


def _claims(authorization: str | None, settings: Settings) -> dict[str, Any]:
    token = bearer_token(authorization)
    if token is None:
        raise UnauthorisedError
    return verify_session_token(token, settings)


def _role(claims: dict[str, Any]) -> str:
    role = claims.get("role")
    return role if role in ROLES else "player"


def current_person(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """The caller, from `Authorization: Bearer <Clerk session token>`.

    A valid token without a usable `pid` (missing, empty or not a UUID) is
    `provisioning_required`; any other failure is the same `401`.
    """
    claims = _claims(authorization, settings)
    try:
        pid = UUID(claims["pid"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProvisioningRequiredError from None
    return Principal(pid=pid, role=_role(claims), session_id=claims["sid"])


def current_clerk_user(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """The Clerk user id (`sub`) of a valid token, `pid` or not. Only `/me/provision` uses it."""
    sub: str = _claims(authorization, settings)["sub"]
    return sub


def optional_person(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """The caller, when there is one: for routes that serve people and, still, anonymous teams.

    `None` with `GAME_SERVER_AUTH_MODE=off` (the header isn't even read), or when no token was
    sent. A token that was sent must check out, as for `current_person`. Refusing callers
    without one is for the routes that need it, from the mode.
    """
    if settings.auth_mode == "off" or authorization is None:
        return None
    return current_person(settings, authorization)
