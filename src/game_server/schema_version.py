"""The database schema version this build needs, checked by `GET /health`."""

# The newest migration's version in db/migrations: bump it in the same PR as a new migration
# (a test fails until you do).
SCHEMA_VERSION = 2


class SchemaBehindError(Exception):
    """The database hasn't been migrated to the version this build needs."""

    def __init__(self, applied: int, needed: int) -> None:
        super().__init__(f"database schema at V{applied}, this build needs V{needed}")
        self.applied = applied
        self.needed = needed


def check_schema_version(applied: int | None, needed: int = SCHEMA_VERSION) -> None:
    """Raise unless the database is at `needed` or ahead (a rollback); None means V0."""
    if (applied or 0) < needed:
        raise SchemaBehindError(applied or 0, needed)
