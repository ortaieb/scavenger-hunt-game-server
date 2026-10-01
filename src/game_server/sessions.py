"""Game sessions and checkpoints, loaded read-only from a moderator-authored JSON file.

Secrecy: a checkpoint's `location` is the answer to its clue. Nothing here may be returned
by an endpoint in a way that reveals checkpoint coordinates or distances to them.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self
from uuid import UUID

from fastapi import Depends
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from game_server.config import Settings, get_settings
from game_server.models import Location


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


class Team(_SessionModel):
    """A team: plays as one participant, visiting the checkpoints in its own order."""

    name: str = Field(min_length=1, max_length=40)
    # A credential: never returned, never logged (hence repr=False).
    join_code: str = Field(min_length=6, max_length=32, pattern=r"^[A-Za-z0-9-]+$", repr=False)
    order: tuple[int, ...] = Field(description="Every checkpoint sequence, each once")

    @field_validator("join_code", mode="before")
    @classmethod
    def _strip_code(cls, value: object) -> object:
        """Surrounding spaces don't count: `FOX-7Q2K ` is the code `FOX-7Q2K`."""
        return value.strip() if isinstance(value, str) else value


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


def _index_join_codes(sessions: Sequence[GameSession]) -> dict[str, tuple[GameSession, Team]]:
    """Map each normalised join code to its team; codes must be unique across the file.

    A duplicate is reported by the later team's path, never by the code itself.
    """
    index: dict[str, tuple[GameSession, Team]] = {}
    problems = []
    for session_index, session in enumerate(sessions):
        for team_index, team in enumerate(session.teams):
            code = normalise_join_code(team.join_code)
            if code in index:
                path = _path((session_index, "teams", team_index, "join-code"))
                problems.append(f"{path}: duplicate join code")
            else:
                index[code] = (session, team)
    if problems:
        raise ValueError(
            "\n".join([f"{len(problems)} validation error(s)", *(f"  {p}" for p in problems)])
        )
    return index


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

    def __init__(self, sessions: Iterable[GameSession] = ()) -> None:
        sessions = list(sessions)
        _require_unique((session.id for session in sessions), "session id")
        self._sessions = {session.id: session for session in sessions}
        self._checkpoints = {
            session.id: {checkpoint.sequence: checkpoint for checkpoint in session.checkpoints}
            for session in sessions
        }
        self._teams_by_code = _index_join_codes(sessions)
        self._teams_by_name = {
            session.id: {team.name.casefold(): team for team in session.teams}
            for session in sessions
        }

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

    @staticmethod
    def effective_window(session: GameSession, checkpoint: Checkpoint) -> tuple[datetime, datetime]:
        """Return the checkpoint's own window if set, otherwise the session's start/end."""
        if checkpoint.window:
            return checkpoint.window.opens_at, checkpoint.window.closes_at
        return session.start_time, session.end_time


_SESSIONS_ADAPTER = TypeAdapter(list[GameSession])


def parse_sessions(raw: str | bytes) -> SessionRepository:
    """Build a repository from the JSON text of a sessions file.

    Raises `SessionsFileError` if the JSON is malformed, a session fails validation
    or two sessions share an id.
    """
    try:
        sessions = _SESSIONS_ADAPTER.validate_json(raw)
    except ValidationError as exc:
        # `from None`: a chained ValidationError would print its input values in the traceback.
        raise SessionsFileError(_describe_errors(exc)) from None
    try:
        return SessionRepository(sessions)
    except ValueError as exc:  # duplicate session ids or join codes; never echoes a code
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
def load_session_repository(path: Path | None) -> SessionRepository:
    """Load sessions from `path`, or return an empty repository when no file is configured.

    Cached per path: the file is read once, at startup.
    """
    if path is None:
        return SessionRepository()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SessionsFileError(f"cannot read sessions file {path}: {exc}") from exc
    try:
        return parse_sessions(raw)
    except SessionsFileError as exc:
        raise SessionsFileError(f"invalid sessions file {path}: {exc}") from None


def get_session_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionRepository:
    """Dependency providing the sessions loaded from the configured file."""
    return load_session_repository(settings.sessions_file)
