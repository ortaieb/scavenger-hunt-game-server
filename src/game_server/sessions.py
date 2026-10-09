"""Game sessions and checkpoints: from a moderator-authored JSON file, and published ones.

Sessions come from the sessions file, read once at startup, and from the database, where the
hunt designer publishes them (`publish_session`). Every lookup serves both, the same way.

Secrecy: a checkpoint's `location` is the answer to its clue. Nothing here may be returned
by an endpoint in a way that reveals checkpoint coordinates or distances to them.
"""

import copy
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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
from game_server.database import Database, get_database
from game_server.imaging import UndecodableImageError, open_upright
from game_server.models import Location
from game_server.published_sessions import AlreadyPublishedError, CodeKind, PublishedSessionRows
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


_Claim = tuple[str, CodeKind, str]  # normalised code, its kind, its path


def _code_claims(session: GameSession) -> list[_Claim]:
    """Every code the session uses, normalised, with its kind and path."""
    claims: list[_Claim] = []
    if session.moderator_code is not None:
        claims.append((normalise_join_code(session.moderator_code), "moderator", "moderator-code"))
    for index, team in enumerate(session.teams):
        claims.append((normalise_join_code(team.join_code), "join", f"teams[{index}].join-code"))
    return claims


def _repeated_codes(claims: Sequence[_Claim]) -> list[str]:
    """A code the session uses twice, at the later use, saying what it repeats."""
    first: dict[str, CodeKind] = {}
    problems = []
    for code, kind, path in claims:
        earlier = first.get(code)
        if earlier is None:
            first[code] = kind
        elif earlier == kind:
            problems.append(f"{path}: duplicate {kind} code")
        else:
            problems.append(f"{path}: same as a {earlier} code")
    return problems


def _no_reference_photos(session: GameSession) -> list[str]:
    """A published session has no reference photos: there are no files to resolve."""
    return [
        f"checkpoints[{index}].reference-photos: a published session has no reference photos"
        for index, checkpoint in enumerate(session.checkpoints)
        if checkpoint.reference_photos
    ]


def _require_unique(values: Iterable[object], what: str) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what}: {value}")
        seen.add(value)


class SessionsFileError(ValueError):
    """The sessions file could not be read or is invalid."""


class SessionPublishError(ValueError):
    """A session can't be published: one `path: problem` line per problem, no values."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        lines = [f"{len(problems)} validation error(s)", *(f"  {p}" for p in problems)]
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class _Indexed:
    """A published session, with the lookups the repository serves."""

    session: GameSession
    checkpoints: dict[int, Checkpoint]
    teams_by_name: dict[str, Team]
    # Credentials: kept out of the repr.
    join_codes: dict[str, Team] = field(repr=False)
    moderator_code: str | None = field(repr=False)

    @classmethod
    def of(cls, session: GameSession) -> "_Indexed":
        moderator = session.moderator_code
        return cls(
            session=session,
            checkpoints={checkpoint.sequence: checkpoint for checkpoint in session.checkpoints},
            teams_by_name={team.name.casefold(): team for team in session.teams},
            join_codes={normalise_join_code(team.join_code): team for team in session.teams},
            moderator_code=normalise_join_code(moderator) if moderator is not None else None,
        )


class SessionRepository:
    """Read-only lookup of game sessions and their checkpoints."""

    def __init__(
        self,
        sessions: Iterable[GameSession] = (),
        *,
        reference_dir: Path | None = None,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        published: PublishedSessionRows | None = None,
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
        self._rows = published
        self._published: dict[UUID, _Indexed] = {}
        self._published_codes: dict[str, UUID] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """The number of sessions in the file (published ones are counted where they live)."""
        return len(self._sessions)

    def __repr__(self) -> str:
        return f"SessionRepository({len(self._sessions)} from the file)"

    def beside(self, published: PublishedSessionRows) -> "SessionRepository":
        """These file sessions, and the published sessions in the database beside them."""
        both = copy.copy(self)
        both._rows = published
        both._published, both._published_codes = {}, {}
        both._lock = threading.Lock()
        return both

    def get_session(self, session_id: UUID) -> GameSession | None:
        """Return the session with this id, if any."""
        found = self._sessions.get(session_id)
        if found is not None:
            return found
        published = self._published_session(session_id)
        return published.session if published else None

    def get_checkpoint(self, session_id: UUID, sequence: int) -> Checkpoint | None:
        """Return the session's checkpoint with this sequence number, if any."""
        if session_id in self._sessions:
            return self._checkpoints[session_id].get(sequence)
        published = self._published_session(session_id)
        return published.checkpoints.get(sequence) if published else None

    def find_team(self, join_code: str) -> tuple[GameSession, Team] | None:
        """The session and team a join code belongs to (ignoring case and spaces), if any."""
        code = normalise_join_code(join_code)
        found = self._teams_by_code.get(code)
        if found is not None:
            return found
        published = self._published_by_code(code, "join")
        if published is None or code not in published.join_codes:
            return None
        return published.session, published.join_codes[code]

    def get_team(self, session_id: UUID, name: str) -> Team | None:
        """The session's team with this name (ignoring case), if any."""
        if session_id in self._sessions:
            return self._teams_by_name[session_id].get(name.casefold())
        published = self._published_session(session_id)
        return published.teams_by_name.get(name.casefold()) if published else None

    def moderator_code(self, session_id: UUID) -> str | None:
        """The session's moderator code, normalised; None if it can't be moderated."""
        if session_id in self._sessions:
            return self._moderator_codes.get(session_id)
        published = self._published_session(session_id)
        return published.moderator_code if published else None

    def reference_photos(self, session_id: UUID, sequence: int) -> tuple[Path, ...]:
        """The checkpoint's reference photos, resolved and checked at load, in file order.

        A published session has none: there are no files to resolve.
        """
        return self._reference_photos.get((session_id, sequence), ())

    def _published_session(self, session_id: UUID) -> _Indexed | None:
        """A published session, from memory or else one indexed query; never changes."""
        if self._rows is None:
            return None
        with self._lock:
            cached = self._published.get(session_id)
        if cached is not None:
            return cached
        document = self._rows.document(session_id)
        if document is None:
            return None
        return self._remember(GameSession.model_validate(document))

    def _published_by_code(self, code: str, kind: CodeKind) -> _Indexed | None:
        """The published session a normalised code belongs to, if any."""
        if self._rows is None:
            return None
        with self._lock:
            session_id = self._published_codes.get(code)
        if session_id is None:
            session_id = self._rows.session_for_code(code, kind)
        return self._published_session(session_id) if session_id is not None else None

    def _remember(self, session: GameSession) -> _Indexed:
        indexed = _Indexed.of(session)
        with self._lock:
            self._published[session.id] = indexed
            for code in indexed.join_codes:
                self._published_codes[code] = session.id
            if indexed.moderator_code is not None:
                self._published_codes[indexed.moderator_code] = session.id
        return indexed

    def publish_session(self, session: GameSession, draft: UUID | None = None) -> None:
        """Publish a session into the database, playable at once; final once published.

        Held to the sessions file's rules, with no reference photos (no files to resolve), an
        unused id, and codes unused by the file and by every published session. Raises
        `SessionPublishError`, each problem at its path and never with a value; then nothing
        is stored. The database refuses a taken code even if two publishes race.
        """
        if self._rows is None:
            raise RuntimeError("publishing needs the database: use a repository beside it")
        document = session.model_dump(mode="json", by_alias=True)
        try:
            checked = GameSession.model_validate(document)
        except ValidationError as exc:
            raise SessionPublishError(_error_lines(exc)) from None
        claims = _code_claims(checked)
        problems = _no_reference_photos(checked) + self._taken(checked.id, claims)
        if problems:
            raise SessionPublishError(problems)
        codes = [(code, kind) for code, kind, _ in claims]
        try:
            self._rows.insert(checked.id, document, draft, codes)
        except AlreadyPublishedError:
            # Lost a race: report what's taken now, at its path.
            raise SessionPublishError(
                self._taken(checked.id, claims) or ["(root): already in use"]
            ) from None
        self._remember(checked)

    def _taken(self, session_id: UUID, claims: Sequence[_Claim]) -> list[str]:
        """Problems with ids and codes: repeated in the session, or already in use."""
        problems = _repeated_codes(claims)
        if session_id in self._sessions or (
            self._rows is not None and self._rows.document(session_id) is not None
        ):
            problems.insert(0, "id: already in use")
        file_codes = set(self._teams_by_code) | set(self._moderator_codes.values())
        published = self._rows.codes_in_use({c for c, _, _ in claims}) if self._rows else set()
        problems += [
            f"{path}: already in use"
            for code, _, path in claims
            if code in file_codes or code in published
        ]
        return problems

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
    lines += [f"  {line}" for line in _error_lines(exc)]
    return "\n".join(lines)


def _error_lines(exc: ValidationError) -> list[str]:
    """`path: message` for each error, never with the input."""
    lines = []
    for error in exc.errors(include_url=False, include_input=False):
        loc, message = tuple(error["loc"]), error["msg"]
        located = error.get("ctx", {}).get("error")
        if isinstance(located, LocatedValueError):
            loc, message = loc + located.loc, located.message
        lines.append(f"{_path(loc) or '(root)'}: {message}")
    return lines


def _path(loc: Sequence[str | int]) -> str:
    """`[0].teams[1].order` from `(0, "teams", 1, "order")`; `teams[1]` from `("teams", 1)`."""
    path = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in loc)
    return path.removeprefix(".")


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


@lru_cache
def sessions_beside(file_sessions: SessionRepository, database: Database) -> SessionRepository:
    """The file's sessions and the database's published ones: one repository per process,
    so published sessions stay cached."""
    return file_sessions.beside(PublishedSessionRows(database))


def get_session_repository(
    settings: Annotated[Settings, Depends(get_settings)],
    database: Annotated[Database, Depends(get_database)],
) -> SessionRepository:
    """Dependency providing the sessions: the configured file's, and the published ones."""
    file_sessions = load_session_repository(settings.sessions_file, settings.max_image_bytes)
    return sessions_beside(file_sessions, database)
