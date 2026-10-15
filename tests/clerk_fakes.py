"""Stand-ins for Clerk: a fake of our own Clerk client, a settable clock, signed Svix events."""

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from svix.webhooks import Webhook

from game_server.directory.clerk import ClerkError, ClerkIdentity

WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"clerk-webhook-signing-key-32byte").decode()
OTHER_SECRET = "whsec_" + base64.b64encode(b"someone-elses-signing-key-32byte").decode()


class FakeClerk:
    """`ClerkUsers` in memory. `fail_reads` / `fail_writes` make the calls raise `ClerkError`."""

    def __init__(self) -> None:
        self.emails: dict[str, str] = {}
        self.users: dict[str, ClerkIdentity] = {}
        self.writes: list[tuple[str, UUID, str]] = []
        self.email_reads = 0
        self.fail_reads = False
        self.fail_writes = False

    def primary_email(self, user_id: str) -> str | None:
        if self.fail_reads:
            raise ClerkError("ConnectError")
        self.email_reads += 1
        return self.emails.get(user_id)

    def write_identity(self, user_id: str, pid: UUID, role: str) -> None:
        if self.fail_writes:
            raise ClerkError("ConnectError")
        self.writes.append((user_id, pid, role))
        self.users[user_id] = ClerkIdentity(user_id, str(pid), str(pid), role)

    def identities(self) -> Iterator[ClerkIdentity]:
        if self.fail_reads:
            raise ClerkError("ConnectError")
        yield from list(self.users.values())


class FakeClock:
    """A clock that stays where it's set."""

    def __init__(self, now: datetime = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def clerk_user(user_id: str = "user_a", email: str | None = "a@example.com") -> dict[str, Any]:
    """A Clerk user object, as events carry it."""
    addresses = [] if email is None else [{"id": "idn_1", "email_address": email}]
    return {
        "id": user_id,
        "primary_email_address_id": "idn_1" if email else None,
        "email_addresses": addresses,
        "first_name": "Ada",
    }


def signed(
    event: object, secret: str = WEBHOOK_SECRET, msg_id: str = "msg_1"
) -> tuple[str, dict[str, str]]:
    """The body and Svix headers of an event, signed as Clerk's webhook signs it."""
    body = json.dumps(event)
    at = datetime.now(UTC)
    return body, {
        "svix-id": msg_id,
        "svix-timestamp": str(int(at.timestamp())),
        "svix-signature": Webhook(secret).sign(msg_id, at, body),
        "content-type": "application/json",
    }
