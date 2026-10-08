"""The hunt designer's drafts: the contract's models and their storage (`hunt_drafts`).

A draft is what the organiser asked for (`request`), the area the run resolved, the
checkpoints it proposes, its progress and problems, and what the run cost. It holds
coordinates and scenes, the answers to its clues, so only the organiser reads it.
JSON columns hold the models as the API shows them (kebab-case keys).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from game_server.database import Database, get_database
from game_server.models import Location

DraftStatus = Literal["running", "ready", "failed", "published"]
Review = Literal["pending", "accepted", "rejected"]
RunErrorCode = Literal[
    "max_turns", "max_budget", "deadline", "no_valid_draft", "agent_unavailable", "interrupted"
]


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


RequestText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]


class DraftRequest(BaseModel):
    """What the organiser asks the designer for."""

    model_config = ConfigDict(alias_generator=_kebab, extra="forbid")

    area: RequestText = Field(description="Where the hunt is, e.g. `Chiswick, London`")
    theme: RequestText
    checkpoints: int = Field(default=3, ge=3, le=8, strict=True)
    max_walk_km: float = Field(default=3, ge=0.5, le=10, strict=True)


class BoundingBox(_KebabModel):
    """The area's extent, in decimal degrees."""

    south: float
    west: float
    north: float
    east: float


class DraftArea(_KebabModel):
    """The area the run resolved; `clipped` if it was cut down to the size limit."""

    name: str
    bbox: BoundingBox
    clipped: bool


class ProgressEntry(_KebabModel):
    """One step of the run, for the organiser to follow; `at` is UTC to the second."""

    at: str
    step: str
    summary: str


class DraftPlace(_KebabModel):
    """A real place from the map data: its OpenStreetMap id, name, kind and location."""

    osm: str
    name: str
    kind: str
    location: Location


class DraftChallenge(_KebabModel):
    """What the photo must show (`scene`, the clue's answer) and the pose to strike."""

    scene: str
    pose: str


class DraftCheckpoint(_KebabModel):
    """A proposed checkpoint, with why the agent picked it and the organiser's review."""

    position: int
    place: DraftPlace
    clue: str
    challenge: DraftChallenge
    proximity: int
    rationale: str
    review: Review = "pending"
    edited: bool = False


class Problem(_KebabModel):
    """A rule the draft breaks; `position` is the checkpoint's, or None for the whole draft."""

    code: str
    position: int | None
    message: str


@dataclass(frozen=True)
class DraftResult:
    """What a finished run writes: `ready` with checkpoints, or `failed` with an error."""

    status: Literal["ready", "failed"]
    area: DraftArea | None
    checkpoints: Sequence[DraftCheckpoint]
    problems: Sequence[Problem]
    model: str | None
    turns: int
    cost_usd: Decimal
    duration_ms: int
    error_code: RunErrorCode | None = None


@dataclass(frozen=True)
class Draft:
    """A stored draft, read back."""

    id: UUID
    status: DraftStatus
    request: DraftRequest
    area: DraftArea | None
    checkpoints: tuple[DraftCheckpoint, ...]
    progress: tuple[ProgressEntry, ...]
    problems: tuple[Problem, ...]
    runner: str
    model: str | None
    turns: int
    cost_usd: Decimal
    duration_ms: int | None
    error_code: RunErrorCode | None
    published_session: UUID | None
    published_at: datetime | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class DraftSummary:
    """A draft in the list."""

    id: UUID
    status: DraftStatus
    area: str
    theme: str
    checkpoints: int
    cost_usd: Decimal
    created_at: datetime
    finished_at: datetime | None


class DesignerBusyError(Exception):
    """Another draft is still running: one run at a time."""


def _json(models: Sequence[BaseModel]) -> Jsonb:
    return Jsonb([model.model_dump(mode="json", by_alias=True) for model in models])


_COLUMNS = sql.SQL(", ").join(
    map(
        sql.Identifier,
        (
            *("id", "status", "request", "area", "checkpoints", "progress", "problems"),
            *("runner", "model", "turns", "cost_usd", "duration_ms", "error_code"),
            *("published_session", "published_at", "created_at", "finished_at"),
        ),
    )
)


def _draft(row: TupleRow) -> Draft:
    (id_, status, request, area, checkpoints, progress, problems, *rest) = row
    (runner, model, turns, cost, duration, error, session, published_at, created, finished) = rest
    return Draft(
        id=id_,
        status=status,
        request=DraftRequest.model_validate(request),
        area=DraftArea.model_validate(area) if area is not None else None,
        checkpoints=tuple(DraftCheckpoint.model_validate(c) for c in checkpoints),
        progress=tuple(ProgressEntry.model_validate(p) for p in progress),
        problems=tuple(Problem.model_validate(p) for p in problems),
        runner=runner,
        model=model,
        turns=turns,
        cost_usd=cost,
        duration_ms=duration,
        error_code=error,
        published_session=session,
        published_at=published_at,
        created_at=created,
        finished_at=finished,
    )


class DraftStore:
    """The hunt drafts, in the external PostgreSQL database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, draft_id: UUID, request: DraftRequest, runner: str, now: datetime) -> None:
        """Store a new `running` draft; `DesignerBusyError` if another is still running."""
        try:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO hunt_drafts (id, status, request, runner, created_at)"
                    " VALUES (%s, 'running', %s, %s, %s)",
                    (draft_id, Jsonb(request.model_dump(mode="json", by_alias=True)), runner, now),
                )
        except pg_errors.UniqueViolation as exc:  # hunt_drafts_one_running
            raise DesignerBusyError from exc

    def get(self, draft_id: UUID) -> Draft | None:
        """The draft, or None if there's no such draft."""
        with self._database.connection() as conn:
            row = conn.execute(
                sql.SQL("SELECT {} FROM hunt_drafts WHERE id = %s").format(_COLUMNS),
                (draft_id,),
            ).fetchone()
        return _draft(row) if row is not None else None

    def list(self, limit: int) -> list[DraftSummary]:
        """The newest `limit` drafts, newest first."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT id, status, request->>'area', request->>'theme',"
                " jsonb_array_length(checkpoints), cost_usd, created_at, finished_at"
                " FROM hunt_drafts ORDER BY created_at DESC, id LIMIT %s",
                (limit,),
            ).fetchall()
        return [DraftSummary(*row) for row in rows]

    def append_progress(self, draft_id: UUID, entry: ProgressEntry) -> None:
        """Add a step to a running draft's progress."""
        with self._database.transaction() as conn:
            conn.execute(
                "UPDATE hunt_drafts SET progress = progress || %s"
                " WHERE id = %s AND status = 'running'",
                (_json([entry]), draft_id),
            )

    def finish(self, draft_id: UUID, result: DraftResult, now: datetime) -> bool:
        """Write a run's result to its draft, if it's still running; True if it was."""
        with self._database.transaction() as conn:
            updated = conn.execute(
                "UPDATE hunt_drafts SET status = %s, area = %s, checkpoints = %s,"
                " problems = %s, model = %s, turns = %s, cost_usd = %s, duration_ms = %s,"
                " error_code = %s, finished_at = %s"
                " WHERE id = %s AND status = 'running'",
                (
                    result.status,
                    Jsonb(result.area.model_dump(mode="json", by_alias=True))
                    if result.area
                    else None,
                    _json(result.checkpoints),
                    _json(result.problems),
                    result.model,
                    result.turns,
                    result.cost_usd,
                    result.duration_ms,
                    result.error_code,
                    now,
                    draft_id,
                ),
            )
            return updated.rowcount == 1

    def interrupt_running(self, now: datetime) -> tuple[UUID, ...]:
        """Fail every `running` draft with `interrupted`: their runs died with the process
        that started them. The drafts' ids."""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "UPDATE hunt_drafts SET status = 'failed', error_code = 'interrupted',"
                " finished_at = %s WHERE status = 'running' RETURNING id",
                (now,),
            ).fetchall()
        return tuple(row[0] for row in rows)


def get_draft_store(database: Annotated[Database, Depends(get_database)]) -> DraftStore:
    """Dependency providing the draft store on the configured database's pool."""
    return DraftStore(database)
