"""The hunt designer's API: start a design, follow it, read the draft. Organiser only.

Given an area and a theme, a run picks checkpoints from map data, writes clues and sets
challenges for the organiser to review. Reviewing and publishing come later (#87).
"""

import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from game_server.clock import Clock, get_clock, utc_iso
from game_server.designer_runners import DesignerRunner, get_designer_runner
from game_server.drafts import (
    DesignerBusyError,
    Draft,
    DraftArea,
    DraftCheckpoint,
    DraftRequest,
    DraftStatus,
    DraftStore,
    Problem,
    ProgressEntry,
    RunErrorCode,
    get_draft_store,
)
from game_server.errors import ApiError
from game_server.geo import distance_m
from game_server.organiser import require_organiser

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
    places = [checkpoint.place.location for checkpoint in ordered]
    legs = [round(distance_m(a, b)) for a, b in zip(places, [*places[1:], places[0]], strict=True)]
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
