"""The directory's rows: people and their profiles.

A `pid` comes from `uuid4()`: never derived from an email or a Clerk id, since anyone who knows
an email could recompute its hash.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends

from game_server.config import Settings, get_settings
from game_server.database import Database, directory_database_config, open_database


class DirectoryNotConfiguredError(Exception):
    """`GAME_SERVER_DIRECTORY_DB_URL` isn't set, so there is no directory to reach."""


@dataclass(frozen=True)
class Profile:
    """What the directory holds about a person's contact details and preferences."""

    pid: UUID
    email: str
    display_name: str | None
    home_area: str | None
    locale: str
    updated_at: datetime


class DirectoryRepository:
    """The `directory.people` and `directory.profiles` tables."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get_or_create_person(self, clerk_user_id: str, email: str) -> UUID:
        """The pid of a Clerk user, made on first sight, with a profile holding `email`.

        One pid per Clerk user, even for concurrent calls: the unique `clerk_user_id` decides,
        and a repeat call only marks the person seen. An existing profile is left as it is.
        """
        with self._database.transaction() as conn:
            row = conn.execute(
                "INSERT INTO directory.people (pid, clerk_user_id) VALUES (%s, %s)"
                " ON CONFLICT (clerk_user_id) DO UPDATE SET last_seen_at = now()"
                " RETURNING pid",
                (uuid4(), clerk_user_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("the upsert returned no row")
            pid: UUID = row[0]
            conn.execute(
                "INSERT INTO directory.profiles (pid, email) VALUES (%s, %s)"
                " ON CONFLICT (pid) DO NOTHING",
                (pid, email),
            )
        return pid

    def find_pid(self, clerk_user_id: str) -> UUID | None:
        """The pid of a Clerk user, or None if they've never been seen."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT pid FROM directory.people WHERE clerk_user_id = %s", (clerk_user_id,)
            ).fetchone()
        return None if row is None else row[0]

    def profile(self, pid: UUID) -> Profile | None:
        """A person's profile, or None if there is none."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT pid, email, display_name, home_area, locale, updated_at"
                " FROM directory.profiles WHERE pid = %s",
                (pid,),
            ).fetchone()
        return None if row is None else Profile(*row)

    def display_names(self, pids: Collection[UUID]) -> dict[UUID, str]:
        """The display names of those of these people who have one."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT pid, display_name FROM directory.profiles"
                " WHERE pid = ANY(%s) AND display_name IS NOT NULL",
                (list(pids),),
            ).fetchall()
        return dict(rows)


def get_directory(settings: Annotated[Settings, Depends(get_settings)]) -> DirectoryRepository:
    """Dependency providing the directory's repository, on its own pool and login.

    Raises `DirectoryNotConfiguredError` if the directory's URL isn't set.
    """
    config = directory_database_config(settings)
    if config is None:
        raise DirectoryNotConfiguredError
    return DirectoryRepository(open_database(config))
