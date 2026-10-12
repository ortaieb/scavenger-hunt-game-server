"""What the MCP tools run on: the app's stores and runner, provided per call."""

from collections.abc import Callable
from dataclasses import dataclass

from game_server.clock import Clock
from game_server.designer.review import CodeSource
from game_server.designer_runners import DesignerRunner
from game_server.drafts import DraftStore
from game_server.sessions import SessionRepository


@dataclass(frozen=True)
class McpServices:
    """The pieces the tools use. Providers are called per tool call, so the pool and the
    app's runner are looked up when needed, not when the server is built."""

    sessions: Callable[[], SessionRepository]
    drafts: Callable[[], DraftStore]
    runner: Callable[[], DesignerRunner]
    codes: CodeSource
    clock: Clock
