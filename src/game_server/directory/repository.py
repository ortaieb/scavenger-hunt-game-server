"""The directory's rows: people, their profiles and their roles.

A `pid` comes from `uuid4()`: never derived from an email or a Clerk id, since anyone who knows
an email could recompute its hash.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends

from game_server.config import Settings, get_settings
from game_server.database import Database, directory_database_config, open_database

PersonStatus = Literal["active", "erasing"]


class DirectoryNotConfiguredError(Exception):
    """`GAME_SERVER_DIRECTORY_DB_URL` isn't set, so there is no directory to reach."""


@dataclass(frozen=True)
class Person:
    """A person's pid and whether they're being erased."""

    pid: UUID
    status: PersonStatus


@dataclass(frozen=True)
class Identity:
    """What Clerk should hold for a person: their pid and their platform role."""

    pid: UUID
    role: str


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

    def find_person(self, clerk_user_id: str) -> Person | None:
        """The person behind a Clerk user, or None if they've never been seen."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT pid, status FROM directory.people WHERE clerk_user_id = %s",
                (clerk_user_id,),
            ).fetchone()
        return None if row is None else Person(*row)

    def role(self, pid: UUID) -> str:
        """A person's platform role: `player` unless they've been granted another."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT role FROM directory.role_grants WHERE pid = %s", (pid,)
            ).fetchone()
        return "player" if row is None else str(row[0])

    def active_identities(self) -> dict[str, Identity]:
        """Every active person's identity, by Clerk user id."""
        # shortcut: one query holding every active person in memory; page it past ~100k people.
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT p.clerk_user_id, p.pid, coalesce(g.role, 'player')"
                " FROM directory.people p LEFT JOIN directory.role_grants g ON g.pid = p.pid"
                " WHERE p.status = 'active'"
            ).fetchall()
        return {user: Identity(pid, role) for user, pid, role in rows}

    def update_email(self, pid: UUID, email: str) -> bool:
        """Set an active person's email; True if it changed."""
        with self._database.connection() as conn:
            changed = conn.execute(
                "UPDATE directory.profiles f SET email = %s, updated_at = now()"
                " FROM directory.people p"
                " WHERE f.pid = %s AND p.pid = f.pid AND p.status = 'active' AND f.email <> %s",
                (email, pid, email),
            ).rowcount
        return changed > 0

    def mark_erasing(self, pid: UUID) -> bool:
        """Mark an active person as being erased; True if they were active."""
        with self._database.connection() as conn:
            changed = conn.execute(
                "UPDATE directory.people SET status = 'erasing'"
                " WHERE pid = %s AND status = 'active'",
                (pid,),
            ).rowcount
        return changed > 0

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


def directory_database(settings: Settings) -> Database:
    """The directory's own pool and login.

    Raises `DirectoryNotConfiguredError` if the directory's URL isn't set.
    """
    config = directory_database_config(settings)
    if config is None:
        raise DirectoryNotConfiguredError
    return open_database(config)


def get_directory(settings: Annotated[Settings, Depends(get_settings)]) -> DirectoryRepository:
    """Dependency providing the directory's repository, on its own pool and login.

    Raises `DirectoryNotConfiguredError` if the directory's URL isn't set.
    """
    return DirectoryRepository(directory_database(settings))
