"""Game sessions and checkpoints, loaded read-only from a moderator-authored JSON file.

Secrecy: a checkpoint's `location` is the answer to its clue. Nothing here may be returned
by an endpoint in a way that reveals checkpoint coordinates or distances to them.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Annotated, Self
from uuid import UUID

from fastapi import Depends
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from game_server.config import DEFAULT_MAX_IMAGE_BYTES, Settings, get_settings
from game_server.imaging import UndecodableImageError, open_upright
from game_server.models import Location
from game_server.session_runs import SessionRun


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _SessionModel(BaseModel):
    """Base for session file models: immutable, strict about fields, kebab-case keys."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=_kebab,
        validate_by_name=True,
        validate_by_alias=True,
    )


class Window(_SessionModel):
    """A time range during which a checkpoint accepts submissions."""

    opens_at: AwareDatetime
    closes_at: AwareDatetime

    @model_validator(mode="after")
    def _opens_before_closes(self) -> Self:
        if self.opens_at >= self.closes_at:
            raise ValueError("opens-at must be before closes-at")
        return self


class VisualChallenge(_SessionModel):
    """What the referee checks in the photo.

    `scene` is server-only: it describes what should be visible behind the player, which
    is effectively the answer to the clue, so no endpoint may return it. `pose` is
    player-facing: the pose or action the player must show.
    """

    scene: str = Field(min_length=1, max_length=1000)
    pose: str = Field(min_length=1, max_length=200)


class Checkpoint(_SessionModel):
    """One place participants must find and photograph."""

    sequence: int = Field(ge=1)
    name: str = Field(min_length=1)
    clue: str = Field(min_length=1)
    location: Location
    proximity: int = Field(gt=0, description="Metres from `location` that count as arrived")
    window: Window | None = None
    # Absent: the referee's visual checks for this checkpoint are skipped.
    challenge: VisualChallenge | None = None
    # The moderator's own photos of the place, relative to the sessions file's directory.
    # Server-only (they show the answer); repr=False because a file name can describe it.
    reference_photos: tuple[Annotated[str, Field(min_length=1)], ...] = Field(
        default=(), max_length=5, repr=False
    )


class LocatedValueError(ValueError):
    """A validation error about a specific field inside the model being validated.

    `loc` is relative to that model (e.g. `("teams", 1, "order")`), so the error is reported
    at the exact path, `[0].teams[1].order: ...`, rather than at the model as a whole.
    """

    def __init__(self, loc: tuple[str | int, ...], message: str) -> None:
        super().__init__(message)
        self.loc = loc
        self.message = message


def normalise_join_code(code: str) -> str:
    """Join codes compare ignoring case and surrounding spaces."""
    return code.strip().upper()


def _strip(value: object) -> object:
    """Surrounding spaces don't count: `FOX-7Q2K ` is the code `FOX-7Q2K`."""
    return value.strip() if isinstance(value, str) else value


# A credential in the sessions file (a join code or a moderator code): 6-32 letters, digits
# or `-`, surrounding spaces stripped, compared ignoring case (see `normalise_join_code`).
Credential = Annotated[
    str,
    Field(min_length=6, max_length=32, pattern=r"^[A-Za-z0-9-]+$"),
    BeforeValidator(_strip),  # after the constraints, so it wraps them and errors read as strings
]


class Team(_SessionModel):
    """A team: plays as one participant, visiting the checkpoints in its own order."""

    name: str = Field(min_length=1, max_length=40)
    # A credential: never returned, never logged (hence repr=False).
    join_code: Credential = Field(repr=False)
    order: tuple[int, ...] = Field(description="Every checkpoint sequence, each once")


class GameSession(_SessionModel):
    """A game: when and where it runs, its checkpoints and the teams playing it."""

    id: UUID
    name: str = Field(min_length=1)
    location: str = Field(min_length=1, description="Description of the region")
    start_time: AwareDatetime
    end_time: AwareDatetime
    checkpoints: tuple[Checkpoint, ...] = Field(min_length=1)
    # Optional: without teams, nobody can join the session.
    teams: tuple[Team, ...] = ()
    # Optional: authorises the moderator's endpoints for this session. Without one, the
    # session can't be moderated. A credential like a join code (hence repr=False).
    moderator_code: Credential | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.end_time <= self.start_time:
            raise ValueError("end-time must be after start-time")
        _require_unique(
            (checkpoint.sequence for checkpoint in self.checkpoints),
            "checkpoint sequence",
        )
        for checkpoint in self.checkpoints:
            window = checkpoint.window
            if window and (window.opens_at < self.start_time or window.closes_at > self.end_time):
                raise ValueError(
                    f"checkpoint {checkpoint.sequence} window must be within the session's "
                    "start-time and end-time"
                )
        self._check_teams()
        return self

    def _check_teams(self) -> None:
        """Team names unique (ignoring case); each order covers every checkpoint once."""
        sequences = {checkpoint.sequence for checkpoint in self.checkpoints}
        names: set[str] = set()
        for index, team in enumerate(self.teams):
            if team.name.casefold() in names:
                raise LocatedValueError(("teams", index, "name"), "duplicate team name")
            names.add(team.name.casefold())
            problem = _order_problem(team.order, sequences)
            if problem:
                raise LocatedValueError(("teams", index, "order"), problem)


def _order_problem(order: tuple[int, ...], sequences: set[int]) -> str | None:
    """Why `order` isn't every checkpoint sequence exactly once, or None if it is."""
    seen: set[int] = set()
    for sequence in order:
        if sequence not in sequences:
            return f"names unknown checkpoint {sequence}"
        if sequence in seen:
            return f"repeats checkpoint {sequence}"
        seen.add(sequence)
    missing = sorted(sequences - seen)
    if missing:
        return f"misses checkpoint(s) {', '.join(map(str, missing))}"
    return None


def _index_codes(
    sessions: Sequence[GameSession],
) -> tuple[dict[str, tuple[GameSession, Team]], dict[UUID, str]]:
    """Index join codes (to their team) and moderator codes (by session), normalised.

    Every code, of either kind, must be unique across the file: a moderator code can't
    repeat another, nor equal any join code. A clash is reported at the later entry's path,
    saying what it clashes with, never the code itself.
    """
    owners: dict[str, str] = {}  # normalised code -> "join code" or "moderator code"
    join_codes: dict[str, tuple[GameSession, Team]] = {}
    moderator_codes: dict[UUID, str] = {}
    problems = []

    def claim(code: str, kind: str, loc: tuple[str | int, ...]) -> bool:
        earlier = owners.get(code)
        if earlier is None:
            owners[code] = kind
            return True
        problem = f"duplicate {kind}" if earlier == kind else f"same as a {earlier}"
        problems.append(f"{_path(loc)}: {problem}")
        return False

    for session_index, session in enumerate(sessions):
        if session.moderator_code is not None:
            code = normalise_join_code(session.moderator_code)
            if claim(code, "moderator code", (session_index, "moderator-code")):
                moderator_codes[session.id] = code
        for team_index, team in enumerate(session.teams):
            code = normalise_join_code(team.join_code)
            if claim(code, "join code", (session_index, "teams", team_index, "join-code")):
                join_codes[code] = (session, team)
    if problems:
        lines = [f"{len(problems)} validation error(s)", *(f"  {p}" for p in problems)]
        raise ValueError("\n".join(lines))
    return join_codes, moderator_codes


# The end of an open-ended window: a running session that hasn't been stopped.
FOREVER = datetime.max.replace(tzinfo=UTC)

JPEG_MAGIC = b"\xff\xd8\xff"


def _resolve_reference_photos(
    sessions: Sequence[GameSession], base_dir: Path | None, max_bytes: int
) -> dict[tuple[UUID, int], tuple[Path, ...]]:
    """Resolve and check every reference photo; report every bad one by position.

    Errors never include the path: a file name can describe the place.
    """
    resolved: dict[tuple[UUID, int], tuple[Path, ...]] = {}
    problems = []
    for session_index, session in enumerate(sessions):
        for checkpoint_index, checkpoint in enumerate(session.checkpoints):
            paths = []
            for photo_index, entry in enumerate(checkpoint.reference_photos):
                path, problem = _check_reference_photo(entry, base_dir, max_bytes)
                if problem:
                    loc = (session_index, "checkpoints", checkpoint_index, "reference-photos")
                    problems.append(f"{_path((*loc, photo_index))}: {problem}")
                elif path:
                    paths.append(path)
            if paths:
                resolved[(session.id, checkpoint.sequence)] = tuple(paths)
    if problems:
        lines = [f"{len(problems)} validation error(s)", *(f"  {p}" for p in problems)]
        raise ValueError("\n".join(lines))
    return resolved


def _check_reference_photo(
    entry: str, base_dir: Path | None, max_bytes: int
) -> tuple[Path | None, str | None]:
    """The resolved path, or why the entry is unusable."""
    if base_dir is None:
        return None, "reference photos need the sessions file's directory"
    if PurePath(entry).is_absolute():
        return None, "must be a relative path"
    base = base_dir.resolve()
    path = (base / entry).resolve()  # follows symlinks, so a link can't lead out either
    if not path.is_relative_to(base):
        return None, "must stay inside the sessions file's directory"
    if not path.is_file():
        return None, "file not found"
    if path.stat().st_size > max_bytes:
        return None, f"file is larger than {max_bytes} bytes"
    data = path.read_bytes()
    if not data.startswith(JPEG_MAGIC):
        return None, "not a JPEG"
    try:
        with open_upright(data, "RGB", 64) as image:
            image.load()
    except UndecodableImageError:
        return None, "doesn't decode (corrupt, truncated or too many pixels)"
    return path, None


def _require_unique(values: Iterable[object], what: str) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what}: {value}")
        seen.add(value)


class SessionsFileError(ValueError):
    """The sessions file could not be read or is invalid."""


class SessionRepository:
    """Read-only lookup of game sessions and their checkpoints."""

    def __init__(
        self,
        sessions: Iterable[GameSession] = (),
        *,
        reference_dir: Path | None = None,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        sessions = list(sessions)
        _require_unique((session.id for session in sessions), "session id")
        self._sessions = {session.id: session for session in sessions}
        self._checkpoints = {
            session.id: {checkpoint.sequence: checkpoint for checkpoint in session.checkpoints}
            for session in sessions
        }
        self._teams_by_code, self._moderator_codes = _index_codes(sessions)
        self._teams_by_name = {
            session.id: {team.name.casefold(): team for team in session.teams}
            for session in sessions
        }
        self._reference_photos = _resolve_reference_photos(sessions, reference_dir, max_image_bytes)

    def __len__(self) -> int:
        return len(self._sessions)

    def get_session(self, session_id: UUID) -> GameSession | None:
        """Return the session with this id, if any."""
        return self._sessions.get(session_id)

    def get_checkpoint(self, session_id: UUID, sequence: int) -> Checkpoint | None:
        """Return the session's checkpoint with this sequence number, if any."""
        return self._checkpoints.get(session_id, {}).get(sequence)

    def find_team(self, join_code: str) -> tuple[GameSession, Team] | None:
        """The session and team a join code belongs to (ignoring case and spaces), if any."""
        return self._teams_by_code.get(normalise_join_code(join_code))

    def get_team(self, session_id: UUID, name: str) -> Team | None:
        """The session's team with this name (ignoring case), if any."""
        return self._teams_by_name.get(session_id, {}).get(name.casefold())

    def moderator_code(self, session_id: UUID) -> str | None:
        """The session's moderator code, normalised; None if it can't be moderated."""
        return self._moderator_codes.get(session_id)

    def reference_photos(self, session_id: UUID, sequence: int) -> tuple[Path, ...]:
        """The checkpoint's reference photos, resolved and checked at load, in file order."""
        return self._reference_photos.get((session_id, sequence), ())

    @staticmethod
    def effective_window(
        run: SessionRun | None, checkpoint: Checkpoint
    ) -> tuple[datetime, datetime] | None:
        """When the checkpoint accepts submissions, or None if never (not started, or empty).

        The session's run, `[started_at, stopped_at]` (open-ended while running), intersected
        with the checkpoint's own `window`, which stays wall-clock: a late start leaves less
        time at a timed checkpoint. The file's start-time/end-time play no part.
        """
        if run is None or run.started_at is None:
            return None
        opens_at, closes_at = run.started_at, run.stopped_at or FOREVER
        if checkpoint.window:
            opens_at = max(opens_at, checkpoint.window.opens_at)
            closes_at = min(closes_at, checkpoint.window.closes_at)
        return (opens_at, closes_at) if opens_at <= closes_at else None


_SESSIONS_ADAPTER = TypeAdapter(list[GameSession])


def parse_sessions(
    raw: str | bytes,
    reference_dir: Path | None = None,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> SessionRepository:
    """Build a repository from the JSON text of a sessions file.

    `reference_dir` is the directory reference photos are resolved against (the sessions
    file's own). Raises `SessionsFileError` if the JSON is malformed, a session fails
    validation, two sessions share an id, or a reference photo is missing or bad.
    """
    try:
        sessions = _SESSIONS_ADAPTER.validate_json(raw)
    except ValidationError as exc:
        # `from None`: a chained ValidationError would print its input values in the traceback.
        raise SessionsFileError(_describe_errors(exc)) from None
    try:
        return SessionRepository(
            sessions, reference_dir=reference_dir, max_image_bytes=max_image_bytes
        )
    except ValueError as exc:  # never echoes a join code or a reference photo's path
        raise SessionsFileError(str(exc)) from exc


def _describe_errors(exc: ValidationError) -> str:
    """One `path: message` line per error, without echoing input values.

    Pydantic's default message includes the offending input, which here would put
    checkpoint coordinates into the server logs.
    """
    lines = [f"{exc.error_count()} validation error(s)"]
    for error in exc.errors(include_url=False, include_input=False):
        loc, message = tuple(error["loc"]), error["msg"]
        located = error.get("ctx", {}).get("error")
        if isinstance(located, LocatedValueError):
            loc, message = loc + located.loc, located.message
        lines.append(f"  {_path(loc) or '(root)'}: {message}")
    return "\n".join(lines)


def _path(loc: Sequence[str | int]) -> str:
    """`[0].teams[1].order` from `(0, "teams", 1, "order")`."""
    return "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in loc)


@lru_cache
def load_session_repository(
    path: Path | None, max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES
) -> SessionRepository:
    """Load sessions from `path`, or return an empty repository when no file is configured.

    Reference photos are resolved against the file's directory and checked now, so a
    broken seed stops the server before the game. Cached: read once, at startup.
    """
    if path is None:
        return SessionRepository()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SessionsFileError(f"cannot read sessions file {path}: {exc}") from exc
    try:
        return parse_sessions(raw, path.parent, max_image_bytes)
    except SessionsFileError as exc:
        raise SessionsFileError(f"invalid sessions file {path}: {exc}") from None


def get_session_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionRepository:
    """Dependency providing the sessions loaded from the configured file."""
    return load_session_repository(settings.sessions_file, settings.max_image_bytes)
