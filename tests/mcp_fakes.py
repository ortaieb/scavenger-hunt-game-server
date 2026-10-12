"""The MCP tests' wiring: the tools on the test database, the stub designer runner and a fixed
clock, called in-process through `mcp.Client`."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp import Client
from mcp.types import CallToolResult
from test_designer_review import FixedCodes, file_sessions
from test_published_sessions import START

from game_server.config import Settings
from game_server.database import Database
from game_server.designer_runners import StubRunner
from game_server.drafts import DraftStore
from game_server.mcp_server.server import build_mcp_server
from game_server.mcp_server.services import McpServices
from game_server.published_sessions import PublishedSessionRows
from game_server.sessions import SessionRepository

NOW = START
STUB_DELAY_SECONDS = 60


def stub_services(
    database: Database,
    runner: StubRunner,
    repository: SessionRepository | None = None,
    codes: FixedCodes | None = None,
) -> McpServices:
    """Tools on the test database; sessions are the file's beside the published ones."""
    # `is None`: an empty repository is falsy.
    if repository is None:
        repository = file_sessions().beside(PublishedSessionRows(database))
    sessions = repository
    return McpServices(
        sessions=lambda: sessions,
        drafts=lambda: DraftStore(database),
        runner=lambda: runner,
        codes=codes or FixedCodes(),
        clock=lambda: NOW,
    )


@contextmanager
def stub_runner(database: Database) -> Iterator[StubRunner]:
    """A stub runner that waits until woken, and never outlives its test."""
    runner = StubRunner(DraftStore(database), lambda: NOW, STUB_DELAY_SECONDS)
    try:
        yield runner
    finally:
        runner.stop()


def call_tool(
    services: McpServices, settings: Settings, name: str, arguments: dict[str, Any] | None = None
) -> CallToolResult:
    """Call one tool in-process; a tool error comes back as a result, not an exception."""

    async def call() -> CallToolResult:
        async with Client(build_mcp_server(settings, services)) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(call())


def error_of(result: CallToolResult) -> dict[str, Any]:
    """The `{"code", "detail", ...}` body of a tool error."""
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]  # a tool error is one text block
    body: dict[str, Any] = json.loads(text[text.index("{") :])
    return body
