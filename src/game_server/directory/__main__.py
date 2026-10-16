"""The directory's command line: `uv run python -m game_server.directory grant-admin ...`.

`grant-admin --clerk-user-id user_…` makes a person an admin: the first admin, since only an
admin can grant roles through the API. It's the only role change that bypasses the API. The
person must have signed in once (so the directory knows them). It needs
`GAME_SERVER_DIRECTORY_DB_URL`, and `GAME_SERVER_CLERK_SECRET_KEY` to tell Clerk at once (else
the write waits in the outbox).

Exit status: 0 when the person is an admin, 1 when there's no such person, 2 for a setup
problem.
"""

import argparse
import sys
from collections.abc import Sequence

from game_server.clock import utc_now
from game_server.config import Settings
from game_server.database import close_databases
from game_server.directory.clerk import get_clerk_users
from game_server.directory.outbox import ClerkOutbox
from game_server.directory.repository import (
    DirectoryNotConfiguredError,
    DirectoryRepository,
    directory_database,
)
from game_server.directory.roles import change_role
from game_server.logging_config import configure_logging


def grant_admin(settings: Settings, clerk_user_id: str) -> int:
    """Make the Clerk user's person an admin; the exit status."""
    try:
        database = directory_database(settings)
    except DirectoryNotConfiguredError:
        print("GAME_SERVER_DIRECTORY_DB_URL isn't set", file=sys.stderr)
        return 2
    directory = DirectoryRepository(database)
    person = directory.find_person(clerk_user_id)
    if person is None or person.status != "active":
        print("No such person: they must sign in once first", file=sys.stderr)
        return 1
    clerk = get_clerk_users(settings)
    change = change_role(
        directory, ClerkOutbox(database), clerk, person.pid, "admin", None, utc_now()
    )
    if change is None:
        print("No such person: they must sign in once first", file=sys.stderr)
        return 1
    print(f"Person {person.pid} is an admin{'' if change.changed else ' already'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    grant = commands.add_parser("grant-admin", help="Make a person an admin")
    grant.add_argument("--clerk-user-id", required=True, help="Their Clerk user id, user_…")
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        return grant_admin(settings, args.clerk_user_id)
    finally:
        close_databases()


if __name__ == "__main__":
    sys.exit(main())
