"""The directory's command line: `uv run python -m game_server.directory grant-admin ...`.

`grant-admin --clerk-user-id user_…` makes a person an admin: the first admin, since only an
admin can grant roles through the API. It's the only role change that bypasses the API. The
person must have signed in once (so the directory knows them). It needs
`GAME_SERVER_DIRECTORY_DB_URL`, and `GAME_SERVER_CLERK_SECRET_KEY` to tell Clerk at once (else
the write waits in the outbox).

`erase --pid …` erases a person, for a request that came by email: the same job as
`DELETE /me`, run to the end here. `--do-not-contact` if they asked never to be contacted again.

`list-erasures` prints every erasure's pid, one a line (`<pid> do-not-contact` when they asked).
Save its output before a database restore: a restore loses erasures recorded after its backup.

`replay-erasures [--pids FILE]` erases again everyone erased before, after a restore and before
the database takes traffic: every erasure in the table, and every pid in FILE (the saved
`list-erasures` output). Unfinished erasures are finished too.

Exit status: 0 when done (the person is an admin, the erasure or replay is complete), 1 when
there's no such person or an erasure is still unfinished (it's retried), 2 for a setup problem.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from game_server.clock import utc_now
from game_server.config import Settings
from game_server.database import close_databases
from game_server.directory.clerk import get_clerk_users
from game_server.directory.erasures import ErasureRecords
from game_server.directory.outbox import ClerkOutbox
from game_server.directory.repository import (
    DirectoryNotConfiguredError,
    DirectoryRepository,
    directory_database,
)
from game_server.directory.roles import change_role
from game_server.erasure import erasure_job
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


def erase(settings: Settings, pid: UUID, do_not_contact: bool) -> int:
    """Erase the person to the end; the exit status."""
    try:
        job = erasure_job(settings, utc_now, get_clerk_users(settings))
    except DirectoryNotConfiguredError:
        print("GAME_SERVER_DIRECTORY_DB_URL isn't set", file=sys.stderr)
        return 2
    erasure = job.request(pid, "admin", do_not_contact)
    if job.run(pid):
        print(f"Person {pid} is erased")
        return 0
    print(
        f"The erasure of person {pid} (requested {erasure.requested_at.isoformat()})"
        " is unfinished: see the log; it's retried",
        file=sys.stderr,
    )
    return 1


def list_erasures(settings: Settings) -> int:
    """Print every erasure's pid; the exit status."""
    try:
        records = ErasureRecords(directory_database(settings))
    except DirectoryNotConfiguredError:
        print("GAME_SERVER_DIRECTORY_DB_URL isn't set", file=sys.stderr)
        return 2
    for erasure in [*records.completed(), *map(records.get, records.unfinished())]:
        if erasure is not None:
            print(f"{erasure.pid}{' do-not-contact' if erasure.do_not_contact else ''}")
    return 0


def read_pids(path: Path) -> dict[UUID, bool]:
    """The pids in a saved `list-erasures` output, and whether each asked not to be contacted.

    Raises `ValueError` for a line that isn't one.
    """
    pids: dict[UUID, bool] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "do-not-contact"):
            raise ValueError(f"not a list-erasures line: {line!r}")
        pid = UUID(parts[0])
        pids[pid] = pids.get(pid, False) or len(parts) == 2
    return pids


def replay_erasures(settings: Settings, pids_file: Path | None) -> int:
    """Erase again everyone erased before; the exit status."""
    try:
        job = erasure_job(settings, utc_now, get_clerk_users(settings))
        records = ErasureRecords(directory_database(settings))
        saved = read_pids(pids_file) if pids_file is not None else {}
    except DirectoryNotConfiguredError:
        print("GAME_SERVER_DIRECTORY_DB_URL isn't set", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"Can't read the pids file: {exc}", file=sys.stderr)
        return 2
    wanted = {erasure.pid: erasure.do_not_contact for erasure in records.completed()}
    for pid, do_not_contact in saved.items():
        wanted[pid] = wanted.get(pid, False) or do_not_contact
    replayed = sum(job.replay(pid, do_not_contact) for pid, do_not_contact in wanted.items())
    unfinished = records.unfinished()
    finished = sum(job.run(pid) for pid in unfinished)
    print(
        f"Replayed {replayed} of {len(wanted)} erasures; finished {finished} of {len(unfinished)}"
    )
    return 0 if replayed == len(wanted) and finished == len(unfinished) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    grant = commands.add_parser("grant-admin", help="Make a person an admin")
    grant.add_argument("--clerk-user-id", required=True, help="Their Clerk user id, user_…")
    erase_parser = commands.add_parser("erase", help="Erase a person (the right to be forgotten)")
    erase_parser.add_argument("--pid", required=True, type=UUID, help="Their pid")
    erase_parser.add_argument(
        "--do-not-contact", action="store_true", help="They asked never to be contacted again"
    )
    commands.add_parser("list-erasures", help="Print every erasure's pid, to save before a restore")
    replay = commands.add_parser("replay-erasures", help="Erase everyone again, after a restore")
    replay.add_argument("--pids", type=Path, help="A saved list-erasures output")
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        if args.command == "erase":
            return erase(settings, args.pid, args.do_not_contact)
        if args.command == "list-erasures":
            return list_erasures(settings)
        if args.command == "replay-erasures":
            return replay_erasures(settings, args.pids)
        return grant_admin(settings, args.clerk_user_id)
    finally:
        close_databases()


if __name__ == "__main__":
    sys.exit(main())
