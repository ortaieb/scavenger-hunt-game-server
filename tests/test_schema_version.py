import pytest
from migration_files import newest_version

from game_server.schema_version import SCHEMA_VERSION, SchemaBehindError, check_schema_version


def test_schema_version_is_the_newest_migration() -> None:
    # A new migration bumps SCHEMA_VERSION in the same PR, so /health gates on it.
    assert newest_version() == SCHEMA_VERSION


@pytest.mark.parametrize("applied", [3, 4])
def test_at_or_ahead_of_the_build_is_fine(applied: int) -> None:
    check_schema_version(applied, needed=3)


@pytest.mark.parametrize(("applied", "reported"), [(2, 2), (None, 0)])
def test_behind_the_build_raises(applied: int | None, reported: int) -> None:
    with pytest.raises(
        SchemaBehindError, match=f"^database schema at V{reported}, this build needs V3$"
    ):
        check_schema_version(applied, needed=3)
