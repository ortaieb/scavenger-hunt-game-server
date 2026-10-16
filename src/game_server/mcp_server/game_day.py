"""The game-day tools: start a session, watch it and finish it, through the same services as
`/sessions/{session}/start`, `/stop` and `/overview`. They need an organiser's token
(see `auth`)."""

from datetime import UTC
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from game_server import organiser_services as services
from game_server.clock import utc_iso
from game_server.mcp_server.errors import tool_error
from game_server.mcp_server.services import McpServices
from game_server.overview import OverviewOut, build_overview
from game_server.session_control import (
    SESSION_NOT_STARTED,
    SESSION_STOPPED,
    SessionClock,
    session_clock,
)
from game_server.sessions import GameSession


class ClockResult(SessionClock):
    """The session clock, and whether this call changed it: `false` when the session was
    already in the state asked for."""

    changed: bool


def register_game_day_tools(server: MCPServer, run: McpServices) -> None:
    """Add the three game-day tools to `server`."""

    def session_of(session_id: UUID) -> GameSession:
        session = run.sessions().get_session(session_id)
        if session is None:
            raise tool_error("unknown_session", "unknown session")
        return session

    def result(session: GameSession, outcome: services.RunOutcome) -> ClockResult:
        clock = session_clock(session, outcome.run, utc_iso(outcome.now))
        return ClockResult(**clock.model_dump(), changed=outcome.changed)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
    def start_session(session_id: UUID) -> ClockResult:
        """Start a scheduled session now: players can join and submit photos from this moment.

        The planned window is only for display: the session runs from when it starts to when it
        stops. A session that is already running is left as it is (`changed` is false). A
        stopped session can't be started again (`session_stopped`).
        """
        session = session_of(session_id)
        try:
            return result(session, services.start_session(run.submissions(), session, run.clock))
        except services.SessionStoppedError:
            raise tool_error(SESSION_STOPPED[1], SESSION_STOPPED[0]) from None

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def stop_session(session_id: UUID) -> ClockResult:
        """Finish a running session, for good. Confirm with the organiser first: it can't be undone.

        Players can no longer join or submit photos, and the session can't be started again.
        Photos still waiting for a ruling can be ruled on afterwards; results are final once
        every one has been. A session that is already stopped is left as it is (`changed` is
        false). One that never started can't be stopped (`session_not_started`).
        """
        session = session_of(session_id)
        try:
            return result(session, services.stop_session(run.submissions(), session, run.clock))
        except services.SessionNotStartedError:
            raise tool_error(SESSION_NOT_STARTED[1], SESSION_NOT_STARTED[0]) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def session_overview(session_id: UUID) -> OverviewOut:
        """The moderator's view of a session: its clock, how many photos wait for a ruling, every
        team's standing and progress (to spot a team that is stuck), and the newest attempts to
        play outside the session.

        Names checkpoints, but shows no coordinates, clues, scenes, photos or codes.
        """
        session = session_of(session_id)
        return build_overview(run.submissions(), session, run.clock().astimezone(UTC))
