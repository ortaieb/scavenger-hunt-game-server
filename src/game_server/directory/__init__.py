"""The users directory: who the people behind the `pid`s are.

Identity lives in the `directory` schema, behind its own database login (`directory_app`), so
the game, which knows a person only by `pid`, can't read it. Game code never touches the
directory's tables: it calls `DirectoryRepository`.
"""

from game_server.directory.repository import (
    DirectoryNotConfiguredError,
    DirectoryRepository,
    Profile,
    get_directory,
)

__all__ = ["DirectoryNotConfiguredError", "DirectoryRepository", "Profile", "get_directory"]
