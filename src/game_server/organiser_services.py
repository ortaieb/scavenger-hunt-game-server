"""What the organiser does, as plain functions: start a design, publish a draft, rule on a photo.

The HTTP routes and the MCP tools both call these, so they do the same thing. They raise domain
errors; each caller maps them to its own kind of response.
"""

import logging
from datetime import UTC
from uuid import UUID, uuid4

from game_server.clock import Clock
from game_server.config import Settings
from game_server.designer.review import (
    CodeSource,
    DraftNotReadyError,
    DraftProblemsError,
    PublishRequest,
    build_session,
    check_accepted,
    issue_codes,
    readiness,
)
from game_server.designer_runners import DesignerRunner
from game_server.drafts import DesignerBusyError, DraftRequest, DraftStore, LockedDraft, Problem
from game_server.rulings import RecordedRuling, Ruling
from game_server.sessions import GameSession, SessionPublishError, SessionRepository
from game_server.submissions import SubmissionStore

logger = logging.getLogger(__name__)

PUBLISH_ATTEMPTS = 3

__all__ = [
    "DesignerBusyError",
    "DesignerDisabledError",
    "DraftNotReadyError",
    "DraftProblemsError",
    "UnknownDraftError",
    "publish_draft",
    "rule",
    "start_design",
]


class DesignerDisabledError(Exception):
    """The runner can't run (no agent credentials, say)."""


class UnknownDraftError(Exception):
    """No such draft."""


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
