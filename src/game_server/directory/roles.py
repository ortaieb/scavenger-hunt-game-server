"""Changing a person's platform role: the directory, then Clerk, which copies it into tokens.

The directory is the source of truth, and role checks read it (see `auth.effective_role`), so a
change takes effect on the server at once. Clerk is told at once too, or through the outbox if
that fails. Removing a role also revokes the person's active Clerk sessions, so their tokens stop
being refreshed rather than carrying the old role until the next refresh.
"""

import logging
from datetime import datetime
from uuid import UUID

from game_server.directory.clerk import ClerkError, ClerkUsers
from game_server.directory.outbox import ClerkOutbox, WriteKind
from game_server.directory.repository import DirectoryRepository, RoleChange

logger = logging.getLogger(__name__)

RANK = {"player": 0, "organiser": 1, "admin": 2}


def change_role(
    directory: DirectoryRepository,
    outbox: ClerkOutbox,
    clerk: ClerkUsers | None,
    pid: UUID,
    role: str,
    changed_by: UUID | None,
    now: datetime,
) -> RoleChange | None:
    """Set the person's role and tell Clerk; None if there's no such active person.

    Without a Clerk client, the writes wait in the outbox. Logs carry pids only.
    """
    change = directory.set_role(pid, role, changed_by)
    if change is None or not change.changed:
        return change
    logger.info(
        "Role of person %s changed %s -> %s by %s",
        pid,
        change.from_role,
        change.to_role,
        changed_by or "the command line",
    )
    _tell_clerk(outbox, clerk, change, "identity", now)
    if RANK[change.to_role] < RANK[change.from_role]:
        _tell_clerk(outbox, clerk, change, "revoke_sessions", now)
    return change


def _tell_clerk(
    outbox: ClerkOutbox,
    clerk: ClerkUsers | None,
    change: RoleChange,
    kind: WriteKind,
    now: datetime,
) -> None:
    try:
        if clerk is None:
            raise ClerkError("no Clerk key")
        if kind == "revoke_sessions":
            clerk.revoke_sessions(change.clerk_user_id)
        else:
            clerk.write_identity(change.clerk_user_id, change.pid, change.to_role)
    except ClerkError as exc:
        outbox.enqueue(change.pid, now, kind)
        logger.warning("Clerk %s for person %s failed (%s): queued", kind, change.pid, exc)
