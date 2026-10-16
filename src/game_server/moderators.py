"""`session_moderators`: who moderates a session, by pid, alongside its moderator code."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends

from game_server.config import Settings, get_settings
from game_server.database import Database, database_config, open_database


class SessionModerators:
    """The `session_moderators` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def is_moderator(self, session: UUID, pid: UUID) -> bool:
        """Whether the person was added as a moderator of the session."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM session_moderators WHERE session = %s AND pid = %s",
                (session, pid),
            ).fetchone()
        return row is not None

    def add(self, session: UUID, pid: UUID, added_by: UUID | None, now: datetime) -> bool:
        """Make the person a moderator of the session; False if they already were."""
        with self._database.connection() as conn:
            added = conn.execute(
                "INSERT INTO session_moderators (session, pid, added_by, added_at)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (session, pid) DO NOTHING",
                (session, pid, added_by, now),
            ).rowcount
        return added > 0

    def remove(self, session: UUID, pid: UUID) -> bool:
        """Stop the person moderating the session; False if they didn't."""
        with self._database.connection() as conn:
            removed = conn.execute(
                "DELETE FROM session_moderators WHERE session = %s AND pid = %s",
                (session, pid),
            ).rowcount
        return removed > 0


def get_session_moderators(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionModerators:
    """Dependency providing the moderators table, on the game's pool."""
    return SessionModerators(open_database(database_config(settings)))
