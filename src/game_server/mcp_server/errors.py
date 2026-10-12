"""Tool errors: the API's error body, as JSON."""

import json

from mcp.server.mcpserver.exceptions import ToolError


def tool_error(code: str, detail: str, **extra: object) -> ToolError:
    """A tool error whose text is `{"code": ..., "detail": ...}`, the API's error body."""
    return ToolError(json.dumps({"code": code, "detail": detail, **extra}))
