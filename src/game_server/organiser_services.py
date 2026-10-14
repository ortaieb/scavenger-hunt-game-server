"""What the organiser does, as plain functions: start a design, edit and publish a draft, rule
on a photo, start and stop a session.

The HTTP routes and the MCP tools both call these, so they do the same thing. They raise domain
errors; each caller maps them to its own kind of response.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from game_server.clock import Clock, utc_iso
from game_server.config import Settings
from game_server.designer.review import (
    CheckpointEdit,
    CodeSource,
    DraftNotReadyError,
    DraftProblemsError,
    PublishRequest,
    apply_edit,
    build_session,
    check_accepted,
    issue_codes,
    readiness,
)
from game_server.designer_runners import DesignerRunner
from game_server.drafts import (
    DesignerBusyError,
    DraftCheckpoint,
    DraftRequest,
    DraftStore,
    LockedDraft,
    Problem,
)
from game_server.rulings import RecordedRuling, Ruling
from game_server.session_runs import SessionRun, session_phase
from game_server.sessions import GameSession, SessionPublishError, SessionRepository
from game_server.submissions import SubmissionStore

logger = logging.getLogger(__name__)

PUBLISH_ATTEMPTS = 3

__all__ = [
    "DesignerBusyError",
    "DesignerDisabledError",
    "DraftNotEditableError",
    "DraftNotReadyError",
    "DraftProblemsError",
    "RunOutcome",
    "SessionNotStartedError",
    "SessionStoppedError",
    "UnknownCheckpointError",
    "UnknownDraftError",
    "edit_checkpoint",
    "publish_draft",
    "rule",
    "start_design",
    "start_session",
    "stop_session",
]


class DesignerDisabledError(Exception):
    """The runner can't run (no agent credentials, say)."""


class UnknownDraftError(Exception):
    """No such draft."""


class DraftNotEditableError(Exception):
    """The draft isn't `ready`: only a ready draft can be edited."""


class UnknownCheckpointError(Exception):
    """The draft has no checkpoint at that position."""


def start_design(
    store: DraftStore, runner: DesignerRunner, clock: Clock, request: DraftRequest
) -> UUID:
    """Create a draft and start its run; the new draft's id.

    Raises `DesignerDisabledError` if the runner can't run, `DesignerBusyError` if another
    design is running.
    """
    if not runner.available():
        raise DesignerDisabledError("the hunt designer is not available")
    draft_id = uuid4()
    store.create(draft_id, request, runner.name, clock().astimezone(UTC))
    logger.info("Draft %s created status running runner %s", draft_id, runner.name)
    runner.start(draft_id, request)
    return draft_id


def edit_checkpoint(
    store: DraftStore, draft: UUID, position: int, edit: CheckpointEdit
) -> DraftCheckpoint:
    """Edit, accept or reject a checkpoint of a `ready` draft; the checkpoint as saved.

    Edits are held to the rules. Raises `UnknownDraftError`, `DraftNotEditableError`,
    `UnknownCheckpointError` or `DraftProblemsError`.
    """
    with store.locked(draft) as locked:
        if locked is None:
            raise UnknownDraftError(str(draft))
        if locked.draft.status != "ready":
            raise DraftNotEditableError("draft can't be edited")
        checkpoints = locked.draft.checkpoints
        found = next((c for c in checkpoints if c.position == position), None)
        if found is None:
            raise UnknownCheckpointError(str(position))
        edited = apply_edit(found, edit)
        locked.save_checkpoints([edited if c.position == position else c for c in checkpoints])
    return edited


def publish_draft(
    store: DraftStore,
    draft: UUID,
    request: PublishRequest,
    settings: Settings,
    sessions: SessionRepository,
    codes: CodeSource,
    clock: Clock,
) -> GameSession:
    """Publish the draft's accepted checkpoints as a session, playable at once, with fresh codes.

    Raises `UnknownDraftError`, `DraftNotReadyError` (saying why) or `DraftProblemsError`.
    """
    with store.locked(draft) as locked:
        if locked is None:
            raise UnknownDraftError(str(draft))
        session = _publish(locked, request, settings, sessions, codes)
        locked.mark_published(session.id, clock().astimezone(UTC))
    logger.info(
        "Draft %s published as session %s teams %d checkpoints %d",
        draft,
        session.id,
        len(session.teams),
        len(session.checkpoints),
    )
    return session


def _publish(
    locked: LockedDraft,
    request: PublishRequest,
    settings: Settings,
    sessions: SessionRepository,
    codes: CodeSource,
) -> GameSession:
    """Check the draft, then publish it, drawing new codes if a race took one."""
    accepted = readiness(locked.draft)
    check_accepted(locked.draft, accepted, settings.designer_min_spacing_m)
    for _ in range(PUBLISH_ATTEMPTS):
        join_codes, moderator_code = issue_codes(len(request.teams), codes, sessions.code_in_use)
        session = build_session(locked.draft, accepted, request, join_codes, moderator_code)
        try:
            sessions.publish_session(session, locked.draft.id)
        except SessionPublishError as exc:
            if all(p.endswith("code: already in use") for p in exc.problems):
                continue  # another publish took a code since it was drawn
            raise DraftProblemsError(
                [Problem(code="invalid_session", position=None, message=p) for p in exc.problems]
            ) from None
        return session
    raise RuntimeError(f"no unused codes after {PUBLISH_ATTEMPTS} attempts")


@dataclass(frozen=True)
class RunOutcome:
    """The session's run after a start or stop, whether that call changed it, and the time."""

    run: SessionRun | None
    changed: bool
    now: datetime


class SessionStoppedError(Exception):
    """The session has ended: it can't start again."""


class SessionNotStartedError(Exception):
    """The session hasn't started: there is nothing to stop."""


def start_session(store: SubmissionStore, session: GameSession, clock: Clock) -> RunOutcome:
    """Start a scheduled session: `scheduled` → `running`. A running one is left as it is
    (`changed` false). Raises `SessionStoppedError` for a stopped one."""
    now = clock().astimezone(UTC)
    change = store.start_run(session.id, now)
    if session_phase(change.run) == "stopped":
        raise SessionStoppedError(str(session.id))
    if change.changed:
        logger.info("Session %s phase running started_at %s", session.id, utc_iso(now))
    return RunOutcome(change.run, change.changed, now)


def stop_session(store: SubmissionStore, session: GameSession, clock: Clock) -> RunOutcome:
    """Finish a running session: `running` → `stopped`, for good. A stopped one is left as it
    is (`changed` false). Raises `SessionNotStartedError` for one that never started."""
    now = clock().astimezone(UTC)
    change = store.stop_run(session.id, now)
    if session_phase(change.run) == "scheduled":
        raise SessionNotStartedError(str(session.id))
    if change.changed:
        logger.info("Session %s phase stopped stopped_at %s", session.id, utc_iso(now))
    return RunOutcome(change.run, change.changed, now)


def rule(
    store: SubmissionStore,
    session: GameSession,
    submission: int,
    ruling: Ruling,
    note: str | None,
    clock: Clock,
) -> RecordedRuling | None:
    """Record the moderator's ruling; None if the submission isn't in the session.

    The note may describe the photo, so it's never logged.
    """
    recorded = store.rule(session.id, submission, ruling, note, clock().astimezone(UTC))
    if recorded is not None:
        logger.info(
            "Moderator ruling session %s submission %d %s verdict %s -> %s first=%s",
            session.id,
            submission,
            ruling,
            recorded.verdict,
            recorded.effective_verdict,
            "yes" if recorded.first else "no",
        )
    return recorded
