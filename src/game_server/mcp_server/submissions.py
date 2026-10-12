"""The submission tools: find the photos that need a decision and see why the referee decided as
it did. Read-only, and without the photos: no image content or URL, no coordinates, no
participant ids, no codes. The organiser views a photo on the moderator screen."""

from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from game_server.clock import utc_iso
from game_server.config import Settings
from game_server.mcp_server.errors import tool_error
from game_server.mcp_server.services import McpServices
from game_server.models import VerdictStatus
from game_server.review import CheckpointOut, RefereeOut
from game_server.ruling import RulingOut
from game_server.rulings import Ruling
from game_server.sessions import GameSession
from game_server.submission_views import ListedSubmission, SubmissionDetail, SubmissionFilter
from game_server.traces import DEFAULT_LIMIT, MAX_LIMIT, MAX_SUBMISSION_ID, StoredCheckOut


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


class SubmissionRow(_KebabModel):
    """A submission, compactly. `verdict` is the referee's; `ruling` the moderator's latest."""

    submission: int
    team: str | None
    checkpoint: CheckpointOut
    attempt: int
    received_at: str
    verdict: VerdictStatus
    ruling: Ruling | None
    effective_verdict: VerdictStatus
    failed_checks: list[str]


class SubmissionList(_KebabModel):
    """A page of submissions, newest first; pass `next` as `before` for the next page."""

    submissions: list[SubmissionRow]
    next: int | None
    moderator_link: str | None


class SubmissionRead(_KebabModel):
    """A submission in full, but for the photo."""

    submission: int
    team: str | None
    checkpoint: CheckpointOut
    attempt: int
    received_at: str
    pose: str | None
    scene: str | None
    verdict: VerdictStatus
    effective_verdict: VerdictStatus
    checks: list[StoredCheckOut]
    referee: RefereeOut | None
    rulings: list[RulingOut]
    moderator_link: str | None


def register_submission_tools(server: MCPServer, settings: Settings, run: McpServices) -> None:
    """Add the two submission tools to `server`."""

    def session_of(session_id: UUID) -> GameSession:
        session = run.sessions().get_session(session_id)
        if session is None:
            raise tool_error("unknown_session", "unknown session")
        return session

    def link(session: UUID) -> str | None:
        if settings.web_app_url is None:
            return None
        return f"{settings.web_app_url.rstrip('/')}/moderator?session={session}"

    def checkpoint_out(session: UUID, sequence: int) -> CheckpointOut:
        checkpoint = run.sessions().get_checkpoint(session, sequence)
        return CheckpointOut(sequence=sequence, name=checkpoint.name if checkpoint else None)

    def row_out(session: UUID, listed: ListedSubmission) -> SubmissionRow:
        return SubmissionRow(
            submission=listed.id,
            team=listed.team,
            checkpoint=checkpoint_out(session, listed.checkpoint),
            attempt=listed.attempt,
            received_at=utc_iso(listed.received_at),
            verdict=listed.verdict,
            ruling=listed.ruling,
            effective_verdict=listed.effective_verdict,
            failed_checks=list(listed.failed_checks),
        )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def list_submissions(
        session_id: UUID,
        filter: Annotated[
            SubmissionFilter, Field(description="`needs_review` is the moderator's queue")
        ] = "needs_review",
        team: Annotated[str | None, Field(description="A team's name, ignoring case")] = None,
        checkpoint: Annotated[
            int | None, Field(ge=1, description="A checkpoint's sequence")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        before: Annotated[
            int | None,
            Field(ge=1, le=MAX_SUBMISSION_ID, description="A submission id: the page starts below"),
        ] = None,
    ) -> SubmissionList:
        """A session's submissions, newest first, as compact rows. No photos.

        `filter`: `needs_review` (the default) is every photo the referee couldn't decide and
        nobody has ruled on; `failed` and `passed` go by the effective verdict (what scoring
        uses, the moderator's ruling over the referee's verdict); `overridden` is every photo
        the moderator ruled on; `all` is everything. Each row has the referee's verdict, the
        latest ruling, the effective verdict and the codes of the checks that failed. Page with
        `before`: pass the previous page's `next` until it is null. Read a photo's reasons with
        `get_submission`; the organiser views the photo on the moderator screen.
        """
        session = session_of(session_id)
        page = run.submissions().list_submissions(
            session.id, filter, team, checkpoint, limit, before
        )
        return SubmissionList(
            submissions=[row_out(session.id, listed) for listed in page.items],
            next=page.next_before,
            moderator_link=link(session.id),
        )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def get_submission(
        session_id: UUID, submission_id: Annotated[int, Field(ge=1, le=MAX_SUBMISSION_ID)]
    ) -> SubmissionRead:
        """One submission: why the referee decided as it did, and every ruling on it. No photo.

        The pose the team was given and the scene it should show; each check with its outcome,
        confidence, the reason the team was told and the detail behind it; the referee's status
        and error code; and every ruling, oldest first (the last is in force). It does not
        include the referee's raw trace or prompt. The scene and the details are the answers to
        the clues: for the organiser only.
        """
        session = session_of(session_id)
        found = run.submissions().submission_detail(session.id, submission_id)
        if found is None:
            raise tool_error("unknown_submission", "unknown submission")
        return read_out(session, found)

    def read_out(session: GameSession, found: SubmissionDetail) -> SubmissionRead:
        checkpoint = run.sessions().get_checkpoint(session.id, found.checkpoint)
        return SubmissionRead(
            submission=found.id,
            team=found.team,
            checkpoint=checkpoint_out(session.id, found.checkpoint),
            attempt=found.attempt,
            received_at=utc_iso(found.received_at),
            pose=found.pose,
            scene=checkpoint.challenge.scene if checkpoint and checkpoint.challenge else None,
            verdict=found.verdict,
            effective_verdict=found.effective_verdict,
            checks=[
                StoredCheckOut(
                    check=c.check,
                    outcome=c.outcome,
                    confidence=c.confidence,
                    reason=c.reason,
                    detail=c.detail,
                )
                for c in found.checks
            ],
            referee=RefereeOut(status=found.referee.status, error_code=found.referee.error_code)
            if found.referee is not None
            else None,
            rulings=[RulingOut.of(ruling) for ruling in found.rulings],
            moderator_link=link(session.id),
        )
