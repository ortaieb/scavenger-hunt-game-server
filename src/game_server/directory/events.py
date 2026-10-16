"""Clerk's user events, once their signature is checked: what each does to the directory.

Svix delivers events late, out of order or twice, so every handler is idempotent, and an event
for a person the directory doesn't know, or is erasing, changes nothing. `user.deleted` records
an erasure, which the erasure job carries out (see `game_server.erasure`).
"""

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from game_server.directory.erasures import ErasureRecords
from game_server.directory.outbox import ClerkOutbox
from game_server.directory.repository import DirectoryRepository

logger = logging.getLogger(__name__)


def primary_email(user: Mapping[str, Any]) -> str | None:
    """The primary email address of a Clerk user object, if it has one."""
    primary = user.get("primary_email_address_id")
    for address in user.get("email_addresses") or []:
        if isinstance(address, Mapping) and primary is not None and address.get("id") == primary:
            email = address.get("email_address")
            return email if isinstance(email, str) and email else None
    return None


def handle_event(
    event: Mapping[str, Any],
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    erasures: ErasureRecords,
    now: datetime,
) -> None:
    """Apply one Clerk event. Other event types, and malformed ones, are ignored."""
    data = event.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("id"), str):
        return
    kind = event.get("type")
    if kind == "user.created":
        _created(data, directory, outbox, now)
    elif kind == "user.updated":
        _updated(data, directory)
    elif kind == "user.deleted":
        _deleted(data["id"], directory, erasures, now)


def _created(
    user: Mapping[str, Any], directory: DirectoryRepository, outbox: ClerkOutbox, now: datetime
) -> None:
    email = primary_email(user)
    if email is None:
        logger.warning("Clerk user.created without a primary email: ignored")
        return
    known = directory.find_person(user["id"])
    if known is not None and known.status == "erasing":
        return
    pid = directory.get_or_create_person(user["id"], email)
    outbox.enqueue(pid, now)
    if known is None:
        logger.info("Person %s created from a Clerk event", pid)


def _updated(user: Mapping[str, Any], directory: DirectoryRepository) -> None:
    email = primary_email(user)
    person = directory.find_person(user["id"])
    if email is None or person is None or person.status != "active":
        return
    if directory.update_email(person.pid, email):
        logger.info("Person %s changed their email", person.pid)


def _deleted(
    user_id: str, directory: DirectoryRepository, erasures: ErasureRecords, now: datetime
) -> None:
    person = directory.find_person(user_id)
    if person is None:
        return
    erasures.request(person.pid, "clerk", do_not_contact=False, now=now)
    if directory.mark_erasing(person.pid):
        logger.info("Person %s deleted in Clerk: to be erased", person.pid)
