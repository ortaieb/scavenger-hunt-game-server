"""The hunt designer's API: start a design, follow it, review the draft, publish it.

Organiser only. Given an area and a theme, a run picks checkpoints from map data, writes clues
and sets challenges; the organiser edits, accepts or rejects each checkpoint, then publishes the
accepted ones as a session, playable at once.
"""

import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from game_server.clock import Clock, get_clock, utc_iso
from game_server.config import Settings, get_settings
from game_server.designer.review import (
    CheckpointEdit,
    CodeSource,
    DraftNotReadyError,
    DraftProblemsError,
    PublishRequest,
    apply_edit,
    build_session,
    check_accepted,
    get_code_source,
    issue_codes,
    readiness,
)
from game_server.designer.rules import route_legs
from game_server.designer_runners import DesignerRunner, get_designer_runner
from game_server.drafts import (
    DesignerBusyError,
    Draft,
    DraftArea,
    DraftCheckpoint,
    DraftRequest,
    DraftStatus,
    DraftStore,
    LockedDraft,
    Problem,
    ProgressEntry,
    RunErrorCode,
    get_draft_store,
)
from game_server.errors import ApiError
from game_server.organiser import require_organiser
from game_server.sessions import (
    GameSession,
    SessionPublishError,
    SessionRepository,
    get_session_repository,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/designer/drafts",
    dependencies=[Depends(require_organiser)],
    responses={401: {"description": "Organiser key required (code: organiser_unauthorised)"}},
)

DRAFTS_LISTED = 50
ATTRIBUTION = "© OpenStreetMap contributors"


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


class DraftCreatedOut(_KebabModel):
    """The new draft's id; follow it at the `Location` given."""

    id: UUID
    status: Literal["running"]


class DraftListItemOut(_KebabModel):
    """A draft in the list."""

    id: UUID
    status: DraftStatus
    area: str
    theme: str
    created_at: str
    finished_at: str | None
    checkpoints: int
    cost_usd: float


class DraftListOut(_KebabModel):
    """The newest drafts, newest first."""

    drafts: list[DraftListItemOut]


class RouteOut(_KebabModel):
    """Each checkpoint to the next, closing the loop back to the first, in metres."""

    legs_m: list[int]
    loop_m: int


class RunErrorOut(_KebabModel):
    """Why the run failed."""

    code: RunErrorCode


class RunOut(_KebabModel):
    """The run behind the draft and what it cost."""

    runner: str
    model: str | None
    turns: int
    cost_usd: float
    duration_ms: int | None
    error: RunErrorOut | None


class PublishedOut(_KebabModel):
    """The session the draft became, and when."""

    session: UUID
    at: str


class DraftOut(_KebabModel):
    """A draft in full. Organiser only: it holds coordinates and scenes."""

    id: UUID
    status: DraftStatus
    request: DraftRequest
    area: DraftArea | None
    progress: list[ProgressEntry]
    checkpoints: list[DraftCheckpoint]
    route: RouteOut | None
    problems: list[Problem]
    run: RunOut
    attribution: str
    published: PublishedOut | None
    created_at: str
    finished_at: str | None


def route_of(checkpoints: Sequence[DraftCheckpoint]) -> RouteOut | None:
    """The walking legs between checkpoints in position order, back to the first.

    Each team plays its own rotation of the route, so the loop closes. None without
    checkpoints.
    """
    if not checkpoints:
        return None
    ordered = sorted(checkpoints, key=lambda checkpoint: checkpoint.position)
    legs = [round(leg) for leg in route_legs([c.place.location for c in ordered])]
    return RouteOut(legs_m=legs, loop_m=sum(legs))


def draft_out(draft: Draft) -> DraftOut:
    """The draft as returned."""
    published = None
    if draft.published_session is not None and draft.published_at is not None:
        published = PublishedOut(session=draft.published_session, at=utc_iso(draft.published_at))
    return DraftOut(
        id=draft.id,
        status=draft.status,
        request=draft.request,
        area=draft.area,
        progress=list(draft.progress),
        checkpoints=list(draft.checkpoints),
        route=route_of(draft.checkpoints),
        problems=list(draft.problems),
        run=RunOut(
            runner=draft.runner,
            model=draft.model,
            turns=draft.turns,
            cost_usd=float(draft.cost_usd),
            duration_ms=draft.duration_ms,
            error=RunErrorOut(code=draft.error_code) if draft.error_code else None,
        ),
        attribution=ATTRIBUTION,
        published=published,
        created_at=utc_iso(draft.created_at),
        finished_at=utc_iso(draft.finished_at) if draft.finished_at else None,
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        409: {"description": "Another design is running (code: designer_busy)"},
        503: {"description": "The hunt designer can't run (code: designer_disabled)"},
    },
)
def start_design(
    body: DraftRequest,
    response: Response,
    clock: Annotated[Clock, Depends(get_clock)],
    store: Annotated[DraftStore, Depends(get_draft_store)],
    runner: Annotated[DesignerRunner, Depends(get_designer_runner)],
) -> DraftCreatedOut:
    """Start designing a hunt; follow the draft at `Location`. One run at a time."""
    if not runner.available():
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the hunt designer is not available",
            "designer_disabled",
        )
    draft_id = uuid4()
    try:
        store.create(draft_id, body, runner.name, clock().astimezone(UTC))
    except DesignerBusyError:
        raise ApiError(
            status.HTTP_409_CONFLICT, "a design is already running", "designer_busy"
        ) from None
    logger.info("Draft %s created status running runner %s", draft_id, runner.name)
    runner.start(draft_id, body)
    response.headers["Location"] = f"/designer/drafts/{draft_id}"
    return DraftCreatedOut(id=draft_id, status="running")


@router.get("")
def list_drafts(store: Annotated[DraftStore, Depends(get_draft_store)]) -> DraftListOut:
    """The newest drafts, newest first (at most 50)."""
    return DraftListOut(
        drafts=[
            DraftListItemOut(
                id=draft.id,
                status=draft.status,
                area=draft.area,
                theme=draft.theme,
                created_at=utc_iso(draft.created_at),
                finished_at=utc_iso(draft.finished_at) if draft.finished_at else None,
                checkpoints=draft.checkpoints,
                cost_usd=float(draft.cost_usd),
            )
            for draft in store.list(DRAFTS_LISTED)
        ]
    )


@router.get("/{draft}", responses={404: {"description": "Unknown draft"}})
def read_draft(draft: UUID, store: Annotated[DraftStore, Depends(get_draft_store)]) -> DraftOut:
    """One draft, in full."""
    found = store.get(draft)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown draft")
    return draft_out(found)


# --- review and publish -------------------------------------------------------------------

PUBLISH_ATTEMPTS = 3


def install(app: FastAPI) -> None:
    """Register the `422 draft problems` response the review routes use."""
    app.add_exception_handler(DraftProblemsError, draft_problems)  # type: ignore[arg-type]  # Starlette types handlers on the base Exception


async def draft_problems(_request: Request, exc: DraftProblemsError) -> JSONResponse:
    """`422 {"detail": "draft problems", "problems": [...]}`."""
    problems = [problem.model_dump(mode="json", by_alias=True) for problem in exc.problems]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": "draft problems", "problems": problems},
    )


def _unknown_draft() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "unknown draft")


@router.patch(
    "/{draft}/checkpoints/{position}",
    responses={
        404: {"description": "Unknown draft or checkpoint"},
        409: {"description": "The draft isn't ready (code: draft_not_editable)"},
        422: {"description": "Invalid body, or the edit breaks a rule: draft problems"},
    },
)
def edit_checkpoint(
    draft: UUID,
    position: int,
    body: CheckpointEdit,
    store: Annotated[DraftStore, Depends(get_draft_store)],
) -> DraftCheckpoint:
    """Edit, accept or reject a checkpoint of a `ready` draft. Edits are held to the rules."""
    with store.locked(draft) as locked:
        if locked is None:
            raise _unknown_draft()
        if locked.draft.status != "ready":
            raise ApiError(status.HTTP_409_CONFLICT, "draft can't be edited", "draft_not_editable")
        checkpoints = locked.draft.checkpoints
        found = next((c for c in checkpoints if c.position == position), None)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown checkpoint")
        edited = apply_edit(found, body)
        locked.save_checkpoints([edited if c.position == position else c for c in checkpoints])
    return edited


class TeamCodeOut(_KebabModel):
    """A team and its join code."""

    name: str
    join_code: str


class PublicationOut(_KebabModel):
    """The published session and its codes: credentials, for the organiser only."""

    session: UUID
    name: str
    moderator_code: str | None
    teams: list[TeamCodeOut]

    @classmethod
    def of(cls, session: GameSession) -> "PublicationOut":
        """The publication of a session."""
        return cls(
            session=session.id,
            name=session.name,
            moderator_code=session.moderator_code,
            teams=[TeamCodeOut(name=t.name, join_code=t.join_code) for t in session.teams],
        )


@router.post(
    "/{draft}/publish",
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Unknown draft"},
        409: {"description": "The draft can't be published yet (code: draft_not_ready)"},
        422: {"description": "Invalid body, or the accepted checkpoints break a rule"},
    },
)
def publish_draft(
    draft: UUID,
    body: PublishRequest,
    clock: Annotated[Clock, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[DraftStore, Depends(get_draft_store)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
    codes: Annotated[CodeSource, Depends(get_code_source)],
) -> PublicationOut:
    """Publish the accepted checkpoints as a session, playable at once, with fresh codes."""
    with store.locked(draft) as locked:
        if locked is None:
            raise _unknown_draft()
        session = _publish(locked, body, settings, sessions, codes)
        locked.mark_published(session.id, clock().astimezone(UTC))
    logger.info(
        "Draft %s published as session %s teams %d checkpoints %d",
        draft,
        session.id,
        len(session.teams),
        len(session.checkpoints),
    )
    return PublicationOut.of(session)


def _publish(
    locked: LockedDraft,
    body: PublishRequest,
    settings: Settings,
    sessions: SessionRepository,
    codes: CodeSource,
) -> GameSession:
    """Check the draft, then publish it, drawing new codes if a race took one."""
    try:
        accepted = readiness(locked.draft)
        check_accepted(locked.draft, accepted, settings.designer_min_spacing_m)
    except DraftNotReadyError as exc:
        raise ApiError(status.HTTP_409_CONFLICT, str(exc), "draft_not_ready") from None
    for _ in range(PUBLISH_ATTEMPTS):
        join_codes, moderator_code = issue_codes(len(body.teams), codes, sessions.code_in_use)
        session = build_session(locked.draft, accepted, body, join_codes, moderator_code)
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


@router.get(
    "/{draft}/publication",
    responses={404: {"description": "Unknown draft, or not published"}},
)
def read_publication(
    draft: UUID,
    store: Annotated[DraftStore, Depends(get_draft_store)],
    sessions: Annotated[SessionRepository, Depends(get_session_repository)],
) -> PublicationOut:
    """A published draft's session and codes, to show them again."""
    found = store.get(draft)
    if found is None:
        raise _unknown_draft()
    session_id = found.published_session if found.status == "published" else None
    session = sessions.get_session(session_id) if session_id is not None else None
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not published")
    return PublicationOut.of(session)
