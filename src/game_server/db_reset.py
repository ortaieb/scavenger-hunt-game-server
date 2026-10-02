"""`python -m game_server.db_reset --yes`: drop the game server's tables and recreate them.

DESTRUCTIVE: for development, until schema changes are versioned migrations. Connects with
the server's own settings (env / `.env`), so TLS applies as it does to the server.
"""

import argparse
import sys
from collections.abc import Sequence
from importlib.resources import files

import psycopg
from psycopg import Connection

from game_server.config import get_settings
from game_server.database import database_config


def schema_script() -> bytes:
    """The SQL that drops and recreates every table, in one transaction."""
    return files("game_server").joinpath("schema.sql").read_bytes()


def reset_schema(conn: Connection[object]) -> None:
    """Drop the tables, if they exist, and create them from scratch. Every row is lost."""
    # bytes, not str: psycopg only runs literal or bytes queries (it guards against injection).
    conn.execute(schema_script())


def main(argv: Sequence[str] | None = None) -> int:
    """Reset the configured database's schema, if confirmed with `--yes`."""
    parser = argparse.ArgumentParser(
        prog="python -m game_server.db_reset",
        description="Drop the game server's tables (if they exist) and create them from "
        "scratch. Every submission, participant and arrival is deleted.",
    )
    parser.add_argument("--yes", action="store_true", help="confirm deleting all the data")
    args = parser.parse_args(argv)
    if not args.yes:
        parser.print_usage(sys.stderr)
        print("Refusing to delete all the data without --yes.", file=sys.stderr)
        return 2
    with psycopg.connect(database_config(get_settings()).conninfo, autocommit=True) as conn:
        reset_schema(conn)
        print(f"Recreated the schema in database {conn.info.dbname!r} on {conn.info.host}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
