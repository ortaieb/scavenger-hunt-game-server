"""Provisioning people: Clerk's signed user events, and `POST /me/provision`.

Both run the same upsert on the Clerk user id, so a person gets one `pid` whichever comes
first, and both have the `pid` and role written to Clerk, which copies them into every token.
Logs name a person by `pid` only: never a Clerk user id, an email or a name.
"""

import json
import logging
from datetime import UTC, timedelta
from functools import lru_cache
from math import ceil
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from svix.webhooks import Webhook, WebhookVerificationError

from game_server.auth import current_clerk_user
from game_server.clock import Clock, get_clock
from game_server.config import Settings, get_settings
from game_server.directory.clerk import ClerkError, ClerkUsers, get_clerk_users
from game_server.directory.events import handle_event
from game_server.directory.outbox import ClerkOutbox
from game_server.directory.repository import (
    DirectoryNotConfiguredError,
    DirectoryRepository,
    directory_database,
)
from game_server.errors import ApiError
from game_server.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

router = APIRouter()

INVALID_SIGNATURE = {"detail": "invalid signature", "code": "invalid_signature"}


class Provisioned(BaseModel):
    """The caller's person id."""

    pid: UUID


def directory_and_outbox(settings: Settings) -> tuple[DirectoryRepository, ClerkOutbox]:
    """The directory's repository and outbox, or `503 directory_unavailable`."""
    try:
        database = directory_database(settings)
    except DirectoryNotConfiguredError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the users directory isn't set up",
            "directory_unavailable",
        ) from None
    return DirectoryRepository(database), ClerkOutbox(database)


@lru_cache
def provision_rate_limiter(interval_seconds: int) -> RateLimiter:
    """The process-wide limiter for a given interval."""
    return RateLimiter(timedelta(seconds=interval_seconds))


def get_provision_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RateLimiter:
    """Dependency: one provisioning per Clerk user per configured interval."""
    return provision_rate_limiter(settings.provision_interval_seconds)


@router.post(
    "/internal/clerk-events",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        400: {"description": "The Svix signature doesn't check out (code: invalid_signature)"},
        503: {"description": "No webhook secret, or no directory: Svix retries later"},
    },
)
async def clerk_event(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> Response:
    """Apply a Clerk user event (`user.created`, `user.updated`, `user.deleted`).

    Reached only through the web app's pass-through. The Svix signature is checked here, with
    the directory's own secret: a bad one changes nothing. Handled events answer `204`, so Svix
    stops retrying; the same event twice changes nothing more.
    """
    secret = settings.clerk_webhook_secret
    if secret is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Clerk events aren't set up",
            "clerk_events_unavailable",
        )
    body = await request.body()
    try:
        Webhook(secret.get_secret_value()).verify(body, dict(request.headers))
        event = json.loads(body)
    except (WebhookVerificationError, ValueError):
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, INVALID_SIGNATURE["detail"], INVALID_SIGNATURE["code"]
        ) from None
    if isinstance(event, dict):
        directory, outbox = directory_and_outbox(settings)
        await run_in_threadpool(handle_event, event, directory, outbox, clock().astimezone(UTC))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/provision",
    responses={
        401: {"description": "No valid Clerk session token (code: unauthorised)"},
        409: {"description": "No primary email in Clerk, or being erased"},
        429: {"description": "Asked again too soon; see the Retry-After header"},
        503: {"description": "No directory, or Clerk can't be reached for a new person"},
    },
)
def provision(
    clerk_user: Annotated[str, Depends(current_clerk_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    clerk: Annotated[ClerkUsers | None, Depends(get_clerk_users)],
    limiter: Annotated[RateLimiter, Depends(get_provision_rate_limiter)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> Provisioned:
    """Make sure the caller has a person id, written to Clerk, and return it.

    The web app calls this after a `provisioning_required`, then takes a fresh token, which
    carries the `pid`. A new person's email comes from Clerk. The write to Clerk happens at
    once; if it fails, the outbox retries it and the `pid` is returned all the same.
    """
    now = clock().astimezone(UTC)
    wait = limiter.check(clerk_user, now)
    if wait is not None:
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "provisioned moments ago, try again shortly",
            "too_many_requests",
            headers={"Retry-After": str(ceil(wait.total_seconds()))},
        )
    directory, outbox = directory_and_outbox(settings)
    if clerk is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Clerk isn't set up", "clerk_unavailable"
        )
    pid = _person(clerk_user, directory, clerk)
    try:
        clerk.write_identity(clerk_user, pid, directory.role(pid))
    except ClerkError as exc:
        outbox.enqueue(pid, now)
        logger.warning("Clerk write for person %s failed (%s): queued", pid, exc)
    return Provisioned(pid=pid)


def _person(clerk_user: str, directory: DirectoryRepository, clerk: ClerkUsers) -> UUID:
    """The caller's pid, creating the person, with their email from Clerk, if they're new."""
    person = directory.find_person(clerk_user)
    if person is not None:
        if person.status == "erasing":
            raise ApiError(status.HTTP_409_CONFLICT, "this account is being erased", "erasing")
        return person.pid
    try:
        email = clerk.primary_email(clerk_user)
    except ClerkError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Clerk can't be reached", "clerk_unavailable"
        ) from None
    if email is None:
        raise ApiError(status.HTTP_409_CONFLICT, "no primary email address", "no_primary_email")
    pid = directory.get_or_create_person(clerk_user, email)
    logger.info("Person %s created by provisioning", pid)
    return pid
