"""The hunt tools: design, review and publish a hunt, through the same services as
`/designer/drafts`. Domain errors become tool errors carrying the API's codes."""

import time
from typing import Annotated, Literal
from uuid import UUID

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from game_server import organiser_services as services
from game_server.config import Settings
from game_server.designer.review import CheckpointEdit, PublishRequest
from game_server.designer.routes import (
    DRAFTS_LISTED,
    DraftListOut,
    DraftOut,
    PublicationOut,
    draft_list_out,
    draft_out,
)
from game_server.drafts import DraftCheckpoint, DraftRequest
from game_server.mcp_server.errors import tool_error
from game_server.mcp_server.services import McpServices

POLL_SECONDS = 0.5


class DesignStarted(BaseModel):
    """The new draft; follow it with `get_hunt_draft`."""

    id: UUID
    status: Literal["running"]
    designer_link: str | None


def _web_link(settings: Settings, path: str) -> str | None:
    if settings.web_app_url is None:
        return None
    return f"{settings.web_app_url.rstrip('/')}{path}"


def register_hunt_tools(server: MCPServer, settings: Settings, run: McpServices) -> None:
    """Add the five hunt tools to `server`."""

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
    def start_hunt_design(
        area: Annotated[
            str, Field(min_length=3, max_length=200, description="e.g. `Chiswick, London`")
        ],
        theme: Annotated[str, Field(min_length=3, max_length=200)],
        checkpoints: Annotated[int, Field(ge=3, le=8)] = 3,
        max_walk_km: Annotated[float, Field(ge=0.5, le=10)] = 3,
    ) -> DesignStarted:
        """Start designing a hunt for an area and theme; it runs for minutes in the background.

        It reads OpenStreetMap and calls the Claude API, costing up to $1 a run, and only one
        design runs at a time. Poll the draft with `get_hunt_draft` until it is `ready`.
        """
        try:
            request = DraftRequest.model_validate(
                {
                    "area": area,
                    "theme": theme,
                    "checkpoints": checkpoints,
                    "max-walk-km": max_walk_km,
                }
            )
        except ValidationError as exc:
            raise tool_error("invalid_request", str(exc.errors(include_input=False))) from None
        try:
            draft = services.start_design(run.drafts(), run.runner(), run.clock, request)
        except services.DesignerDisabledError as exc:
            raise tool_error("designer_disabled", str(exc)) from None
        except services.DesignerBusyError:
            raise tool_error("designer_busy", "a design is already running") from None
        return DesignStarted(
            id=draft,
            status="running",
            designer_link=_web_link(settings, f"/designer?draft={draft}"),
        )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def list_hunt_drafts() -> DraftListOut:
        """The newest 50 hunt drafts, newest first: status, area, theme, checkpoints and cost."""
        return draft_list_out(run.drafts().list(DRAFTS_LISTED))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def get_hunt_draft(
        draft_id: UUID,
        wait_seconds: Annotated[float, Field(ge=0, le=20)] = 0,
    ) -> DraftOut:
        """One hunt draft in full: checkpoints, route, problems, progress and cost.

        With `wait_seconds`, returns as soon as the draft's status changes, or at the timeout.
        A draft holds coordinates and scenes: the answers to its clues, for the organiser only.
        """
        store = run.drafts()
        found = await anyio.to_thread.run_sync(store.get, draft_id)
        if found is None:
            raise tool_error("unknown_draft", "unknown draft")
        deadline = time.monotonic() + wait_seconds
        while found.status == "running" and time.monotonic() < deadline:
            await anyio.sleep(min(POLL_SECONDS, max(deadline - time.monotonic(), 0)))
            latest = await anyio.to_thread.run_sync(store.get, draft_id)
            if latest is None:
                raise tool_error("unknown_draft", "unknown draft")
            found = latest
        return draft_out(found)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
    def edit_hunt_checkpoint(
        draft_id: UUID, position: int, edit: CheckpointEdit
    ) -> DraftCheckpoint:
        """Edit, accept or reject one checkpoint of a `ready` draft; returns it as saved.

        Set `review` to `accepted` or `rejected`; only accepted checkpoints are published.
        Edited text is held to the same rules as the designer's: a clue can't name its place.
        """
        try:
            return services.edit_checkpoint(run.drafts(), draft_id, position, edit)
        except services.UnknownDraftError:
            raise tool_error("unknown_draft", "unknown draft") from None
        except services.UnknownCheckpointError:
            raise tool_error("unknown_checkpoint", "unknown checkpoint") from None
        except services.DraftNotEditableError:
            raise tool_error("draft_not_editable", "draft can't be edited") from None
        except services.DraftProblemsError as exc:
            raise _problems(exc) from None

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def publish_hunt(draft_id: UUID, request: PublishRequest) -> PublicationOut:
        """Publish the draft's accepted checkpoints as a session, playable at once. Final.

        The result holds the session's join codes and its moderator code. They are credentials:
        give each to its holders only (a team's join code to that team, the moderator code to
        the moderator) and never repeat them elsewhere. No other tool returns them.
        """
        try:
            session = services.publish_draft(
                run.drafts(), draft_id, request, settings, run.sessions(), run.codes, run.clock
            )
        except services.UnknownDraftError:
            raise tool_error("unknown_draft", "unknown draft") from None
        except services.DraftNotReadyError as exc:
            raise tool_error("draft_not_ready", str(exc)) from None
        except services.DraftProblemsError as exc:
            raise _problems(exc) from None
        return PublicationOut.of(session)


def _problems(exc: services.DraftProblemsError) -> ToolError:
    problems = [problem.model_dump(mode="json", by_alias=True) for problem in exc.problems]
    return tool_error("draft_problems", "draft problems", problems=problems)
