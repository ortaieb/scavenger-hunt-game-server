"""A thin client for the Clerk Backend API: the few calls the directory makes.

Everything else talks to `ClerkUsers`, so tests replace this client and never the SDK. Errors
come out as `ClerkError`, whose message names only the kind of failure: the SDK's own messages
can quote a Clerk user id, which must not reach the logs.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Protocol
from uuid import UUID

import httpx
from clerk_backend_api import Clerk
from clerk_backend_api.models import ClerkBaseError, User
from clerk_backend_api.models.getsessionlistop import QueryParamStatus
from fastapi import Depends

from game_server.config import Settings, get_settings

TIMEOUT_MS = 10_000
PAGE_SIZE = 500  # the most Clerk lists at once
HTTP_NOT_FOUND = 404


class ClerkError(Exception):
    """A call to Clerk failed; the message is the failure's type, never its details."""


@dataclass(frozen=True)
class ClerkIdentity:
    """What a Clerk user holds of their identity: the external id and the metadata claims."""

    user_id: str
    external_id: str | None
    pid: object
    role: object


class ClerkUsers(Protocol):
    """The Clerk calls the directory makes."""

    def primary_email(self, user_id: str) -> str | None:
        """The user's primary email address, if they have one."""
        ...

    def write_identity(self, user_id: str, pid: UUID, role: str) -> None:
        """Set the user's external id to `pid`, and merge `pid` and `role` into their public
        metadata."""
        ...

    def identities(self) -> Iterator[ClerkIdentity]:
        """Every Clerk user's identity, page by page."""
        ...

    def revoke_sessions(self, user_id: str) -> int:
        """End every active session of the user, so their tokens can't be refreshed; how many."""
        ...

    def delete_user(self, user_id: str) -> None:
        """Delete the user and everything Clerk holds of them; a user already gone is fine."""
        ...


class ClerkApi:
    """`ClerkUsers` on Clerk's Python SDK, with the secret key."""

    def __init__(self, secret_key: str) -> None:
        self._clerk = Clerk(bearer_auth=secret_key, timeout_ms=TIMEOUT_MS)
        # httpx logs each request's URL at INFO, and Clerk's URLs name the user.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def primary_email(self, user_id: str) -> str | None:
        """The user's primary email address, if they have one."""
        try:
            user = self._clerk.users.get(user_id=user_id)
        except (ClerkBaseError, httpx.HTTPError) as exc:
            raise ClerkError(type(exc).__name__) from None
        return _primary_email(user)

    def write_identity(self, user_id: str, pid: UUID, role: str) -> None:
        """Set the user's external id to `pid` (Clerk keeps external ids unique), and merge `pid`
        and `role` into their public metadata (readable in the browser, writable only here)."""
        try:
            self._clerk.users.update(user_id=user_id, external_id=str(pid))
            self._clerk.users.update_metadata(
                user_id=user_id, public_metadata={"pid": str(pid), "role": role}
            )
        except (ClerkBaseError, httpx.HTTPError) as exc:
            raise ClerkError(type(exc).__name__) from None

    def identities(self) -> Iterator[ClerkIdentity]:
        """Every Clerk user's identity, page by page."""
        offset = 0
        while True:
            try:
                users = self._clerk.users.list(request={"limit": PAGE_SIZE, "offset": offset})
            except (ClerkBaseError, httpx.HTTPError) as exc:
                raise ClerkError(type(exc).__name__) from None
            for user in users:
                metadata = user.public_metadata or {}
                yield ClerkIdentity(
                    user.id, user.external_id, metadata.get("pid"), metadata.get("role")
                )
            if len(users) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    def revoke_sessions(self, user_id: str) -> int:
        """End every active session of the user, so their tokens can't be refreshed; how many."""
        try:
            sessions = self._clerk.sessions.list(
                user_id=user_id, status=QueryParamStatus.ACTIVE, limit=100
            )
            for session in sessions:
                self._clerk.sessions.revoke(session_id=session.id)
        except (ClerkBaseError, httpx.HTTPError) as exc:
            raise ClerkError(type(exc).__name__) from None
        return len(sessions)

    def delete_user(self, user_id: str) -> None:
        """Delete the user and everything Clerk holds of them; a user already gone is fine."""
        try:
            self._clerk.users.delete(user_id=user_id)
        except ClerkBaseError as exc:
            if exc.status_code == HTTP_NOT_FOUND:
                return
            raise ClerkError(type(exc).__name__) from None
        except httpx.HTTPError as exc:
            raise ClerkError(type(exc).__name__) from None


def _primary_email(user: User) -> str | None:
    for address in user.email_addresses:
        if address.id == user.primary_email_address_id:
            return address.email_address
    return None


@lru_cache
def clerk_api(secret_key: str) -> ClerkApi:
    """One client per key, for the process."""
    return ClerkApi(secret_key)


def get_clerk_users(settings: Annotated[Settings, Depends(get_settings)]) -> ClerkUsers | None:
    """Dependency providing the Clerk client, or None without `GAME_SERVER_CLERK_SECRET_KEY`."""
    if settings.clerk_secret_key is None:
        return None
    return clerk_api(settings.clerk_secret_key.get_secret_value())
