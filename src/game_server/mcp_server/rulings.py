"""The override tool: rule on a submission from Claude, through the same service as the route.

The ruling is recorded beside the referee's verdict, never over it. The note may describe the
photo, so it's never logged."""

from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from game_server import organiser_services as services
from game_server.mcp_server.errors import tool_error
from game_server.mcp_server.services import McpServices
from game_server.ruling import NOTE_MAX_LENGTH, SubmissionRulingOut
from game_server.rulings import Ruling
from game_server.traces import MAX_SUBMISSION_ID


class OverrideResult(SubmissionRulingOut):
    """The ruling as `POST …/ruling` answers it, and whether it replaced an earlier one."""

    replaced: bool


def register_ruling_tool(server: MCPServer, run: McpServices) -> None:
    """Add `override_verdict` to `server`."""

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def override_verdict(
        session_id: UUID,
        submission_id: Annotated[int, Field(ge=1, le=MAX_SUBMISSION_ID)],
        ruling: Ruling,
        note: Annotated[str | None, Field(max_length=NOTE_MAX_LENGTH)] = None,
    ) -> OverrideResult:
        """Approve or reject a photo, whatever the referee decided. Reversible.

        The referee's verdict is never changed: the ruling is recorded beside it, and scoring
        follows the effective verdict (`approve` counts as a pass, `reject` as a fail). An
        approval completes the photo's checkpoint, and a later `reject` doesn't take that
        back: it changes the team's score, not its progress. Ruling again on the same photo
        replaces the ruling (`replaced` is true); the earlier one stays in the record. Allowed
        while the session runs and after it stops. The `note` is for the record: it is never
        logged, but don't put anything in it that you wouldn't want the moderator to read.
        """
        sessions = run.sessions()
        session = sessions.get_session(session_id)
        if session is None:
            raise tool_error("unknown_session", "unknown session")
        recorded = services.rule(run.submissions(), session, submission_id, ruling, note, run.clock)
        if recorded is None:
            raise tool_error("unknown_submission", "unknown submission")
        return OverrideResult(
            **SubmissionRulingOut.of(recorded).model_dump(), replaced=not recorded.first
        )
