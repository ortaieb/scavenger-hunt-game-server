"""The directory's rows: people, their profiles and their roles.

A `pid` comes from `uuid4()`: never derived from an email or a Clerk id, since anyone who knows
an email could recompute its hash.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends

from game_server.config import Settings, get_settings
from game_server.database import Database, directory_database_config, open_database

PersonStatus = Literal["active", "erasing"]
ConsentSource = Literal["join", "profile"]
PROFILE_FIELDS = frozenset({"display_name", "home_area", "locale"})


class DirectoryNotConfiguredError(Exception):
    """`GAME_SERVER_DIRECTORY_DB_URL` isn't set, so there is no directory to reach."""


@dataclass(frozen=True)
class Person:
    """A person's pid and whether they're being erased."""

    pid: UUID
    status: PersonStatus


@dataclass(frozen=True)
class RoleChange:
    """A role set on a person: what it was, what it is, and the Clerk user to tell."""

    pid: UUID
    from_role: str
    to_role: str
    clerk_user_id: str

    @property
    def changed(self) -> bool:
        """Whether the role is different from before."""
        return self.from_role != self.to_role


@dataclass(frozen=True)
class Listed:
    """A person as an admin screen lists them."""

    pid: UUID
    display_name: str | None
    role: str


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


@dataclass(frozen=True)
class Account:
    """When a person's account was made and when they were last seen."""

    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class ConsentRecord:
    """One consent row: a grant, withdrawn or not."""

    purpose: str
    granted_at: datetime
    withdrawn_at: datetime | None
    wording_version: str
    source: str


@dataclass(frozen=True)
class Consent:
    """A person's upcoming-hunts consent: in force or not, and since when.

    `since` is when the open grant was given, else when the latest one was withdrawn (None if
    they never granted one). `wording_version` is that of the grant `since` refers to.
    """

    granted: bool
    since: datetime | None
    wording_version: str | None


class DirectoryRepository:
    """The `directory.people`, `directory.profiles` and `directory.contact_consents` tables."""

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

    def set_role(self, pid: UUID, role: str, changed_by: UUID | None) -> RoleChange | None:
        """Give an active person a platform role, logged in `role_changes` with who did it;
        None if there's no such active person. `player` removes their grant. Setting the role
        they already have changes and logs nothing."""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT p.clerk_user_id, coalesce(g.role, 'player')"
                " FROM directory.people p LEFT JOIN directory.role_grants g ON g.pid = p.pid"
                " WHERE p.pid = %s AND p.status = 'active' FOR UPDATE OF p",
                (pid,),
            ).fetchone()
            if row is None:
                return None
            change = RoleChange(pid, str(row[1]), role, str(row[0]))
            if not change.changed:
                return change
            if role == "player":
                conn.execute("DELETE FROM directory.role_grants WHERE pid = %s", (pid,))
            else:
                conn.execute(
                    "INSERT INTO directory.role_grants (pid, role, granted_by, granted_at)"
                    " VALUES (%s, %s, %s, now()) ON CONFLICT (pid) DO UPDATE"
                    " SET role = EXCLUDED.role, granted_by = EXCLUDED.granted_by,"
                    " granted_at = EXCLUDED.granted_at",
                    (pid, role, changed_by),
                )
            conn.execute(
                "INSERT INTO directory.role_changes (pid, from_role, to_role, changed_by)"
                " VALUES (%s, %s, %s, %s)",
                (pid, change.from_role, role, changed_by),
            )
        return change

    def people_with_roles(self, roles: Collection[str]) -> list[Listed]:
        """Active people whose role is one of `roles`, with their display names."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT p.pid, f.display_name, coalesce(g.role, 'player') AS role"
                " FROM directory.people p"
                " LEFT JOIN directory.role_grants g ON g.pid = p.pid"
                " LEFT JOIN directory.profiles f ON f.pid = p.pid"
                " WHERE p.status = 'active' AND coalesce(g.role, 'player') = ANY(%s)"
                " ORDER BY role, f.display_name NULLS LAST, p.pid",
                (list(roles),),
            ).fetchall()
        return [Listed(*row) for row in rows]

    def find_pid_by_email(self, email: str) -> UUID | None:
        """The active person with this email address (ignoring case), if exactly one has it."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT p.pid FROM directory.people p JOIN directory.profiles f ON f.pid = p.pid"
                " WHERE p.status = 'active' AND lower(f.email) = lower(%s) LIMIT 2",
                (email.strip(),),
            ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

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

    def status(self, pid: UUID) -> PersonStatus | None:
        """The person's status, or None if the directory doesn't have them."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT status FROM directory.people WHERE pid = %s", (pid,)
            ).fetchone()
        return None if row is None else row[0]

    def account(self, pid: UUID) -> Account | None:
        """When the person's account was made and last seen, or None if there is none."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT created_at, last_seen_at FROM directory.people WHERE pid = %s", (pid,)
            ).fetchone()
        return None if row is None else Account(*row)

    def consent_history(self, pid: UUID) -> list[ConsentRecord]:
        """Every consent the person gave, oldest first, withdrawn or not."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT purpose, granted_at, withdrawn_at, wording_version, source"
                " FROM directory.contact_consents WHERE pid = %s ORDER BY id",
                (pid,),
            ).fetchall()
        return [ConsentRecord(*row) for row in rows]

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

    def update_profile(self, pid: UUID, changes: Mapping[str, str | None], now: datetime) -> bool:
        """Set the given profile fields (`display_name`, `home_area`, `locale`; None clears the
        first two) on an active person, leaving the others; False if there is no such person."""
        unknown = set(changes) - PROFILE_FIELDS
        if unknown:
            raise ValueError(f"not profile fields: {sorted(unknown)}")
        with self._database.connection() as conn:
            return (
                conn.execute(
                    "UPDATE directory.profiles f SET"
                    " display_name = CASE WHEN %(set_name)s THEN %(name)s ELSE f.display_name END,"
                    " home_area = CASE WHEN %(set_area)s THEN %(area)s ELSE f.home_area END,"
                    " locale = CASE WHEN %(set_locale)s THEN %(locale)s ELSE f.locale END,"
                    " updated_at = %(now)s"
                    " FROM directory.people p"
                    " WHERE f.pid = %(pid)s AND p.pid = f.pid AND p.status = 'active'",
                    {
                        "set_name": "display_name" in changes,
                        "name": changes.get("display_name"),
                        "set_area": "home_area" in changes,
                        "area": changes.get("home_area"),
                        "set_locale": "locale" in changes,
                        "locale": changes.get("locale"),
                        "now": now,
                        "pid": pid,
                    },
                ).rowcount
                > 0
            )

    def upcoming_hunts_consent(self, pid: UUID) -> Consent:
        """Whether the person's upcoming-hunts consent is in force, and since when."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT withdrawn_at IS NULL, coalesce(withdrawn_at, granted_at), wording_version"
                " FROM directory.contact_consents"
                " WHERE pid = %s AND purpose = 'upcoming_hunts'"
                " ORDER BY id DESC LIMIT 1",
                (pid,),
            ).fetchone()
        return Consent(False, None, None) if row is None else Consent(*row)

    def grant_upcoming_hunts(
        self, pid: UUID, wording_version: str, source: ConsentSource, now: datetime
    ) -> bool:
        """Record a grant for an active person; False if one is already in force (nothing is
        added) or there is no such person."""
        with self._database.connection() as conn:
            return (
                conn.execute(
                    "INSERT INTO directory.contact_consents"
                    " (pid, purpose, granted_at, wording_version, source)"
                    " SELECT p.pid, 'upcoming_hunts', %s, %s, %s FROM directory.people p"
                    " WHERE p.pid = %s AND p.status = 'active'"
                    " ON CONFLICT (pid, purpose) WHERE withdrawn_at IS NULL DO NOTHING",
                    (now, wording_version, source, pid),
                ).rowcount
                > 0
            )

    def withdraw_upcoming_hunts(self, pid: UUID, now: datetime) -> bool:
        """End the grant in force, keeping its row; False if none was in force."""
        with self._database.connection() as conn:
            return (
                conn.execute(
                    "UPDATE directory.contact_consents SET withdrawn_at = %s"
                    " WHERE pid = %s AND purpose = 'upcoming_hunts' AND withdrawn_at IS NULL",
                    (now, pid),
                ).rowcount
                > 0
            )

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


def optional_directory(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DirectoryRepository | None:
    """Dependency providing the directory's repository, or None when there isn't one."""
    try:
        return get_directory(settings)
    except DirectoryNotConfiguredError:
        return None
