"""Authorising the organiser's endpoints (the hunt designer) with a token."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header

from game_server.auth import ForbiddenError, can, current_person, effective_role
from game_server.config import Settings, get_settings


@dataclass(frozen=True)
class OrganiserAccess:
    """The organiser: the person's pid."""

    pid: UUID


def organiser_access(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> OrganiserAccess:
    """Pass if a valid token whose current role may design hunts.

    A token (a JWT) must be valid (else 401) and its person an organiser or admin (else
    `403 forbidden`).
    """
    person = current_person(settings, authorization)
    if not can(effective_role(person, settings), "design"):
        raise ForbiddenError
    return OrganiserAccess(person.pid)


def require_organiser(
    access: Annotated[OrganiserAccess, Depends(organiser_access)],
) -> None:
    """Pass if the request may organise (see `organiser_access`)."""
