"""The organiser's review: edit, accept or reject checkpoints, then publish the hunt.

Every edit is held to the same rules as the agent (`rules.check_checkpoint`); publishing
checks the accepted checkpoints again as a whole draft (`rules.check_draft`), then builds a
session with generated codes for #84's `publish_session`.
"""

import secrets
from collections.abc import Callable, Sequence
from typing import Annotated, Protocol, Self
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from game_server.designer.osm import Place
from game_server.designer.rules import check_checkpoint, check_draft
from game_server.drafts import (
    Draft,
    DraftCheckpoint,
    DraftOriginal,
    DraftPlace,
    Problem,
    Review,
)
from game_server.sessions import Checkpoint, GameSession, Team, VisualChallenge

MIN_ACCEPTED = 3
MAX_TEAMS = 10
CODE_ATTEMPTS = 20
# No 0/O, 1/I/L: easy to read out and type on a phone.
UNAMBIGUOUS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
JOIN_WORDS = (
    "FOX",
    "OWL",
    "ELK",
    "YAK",
    "BEE",
    "EMU",
    "HEN",
    "RAM",
    "COD",
    "EEL",
    "BAT",
    "DOE",
    "JAY",
    "KOI",
    "APE",
    "ASP",
    "BUG",
    "CUB",
    "GNU",
    "PUG",
)


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class DraftProblemsError(Exception):
    """The draft, or an edit to it, breaks the rules: `422` with the problems."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        super().__init__(f"{len(problems)} draft problem(s)")
        self.problems = list(problems)


class DraftNotReadyError(Exception):
    """The draft can't be published (yet): `409 draft_not_ready`, saying why."""


# --- editing -----------------------------------------------------------------------------------


class CheckpointEdit(BaseModel):
    """What the organiser changes about a checkpoint: at least one field."""

    model_config = ConfigDict(extra="forbid", alias_generator=_kebab)

    clue: str | None = None
    pose: str | None = None
    scene: str | None = None
    proximity: int | None = Field(default=None, strict=True)
    review: Review | None = None

    @model_validator(mode="after")
    def _something_and_no_nulls(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("change at least one of clue, pose, scene, proximity or review")
        nulls = [field for field in self.model_fields_set if getattr(self, field) is None]
        if nulls:
            raise ValueError(f"can't be null: {', '.join(sorted(nulls))}")
        return self


def candidate(place: DraftPlace) -> Place:
    """The checkpoint's place as the rules see it: its coordinates came from the run's
    candidates, so it is one."""
    return Place(osm=place.osm, name=place.name, kind=place.kind, location=place.location, tags={})


def apply_edit(checkpoint: DraftCheckpoint, edit: CheckpointEdit) -> DraftCheckpoint:
    """The checkpoint with the edit applied, the agent's original kept on a field's first
    edit. `DraftProblemsError` if edited text or proximity breaks a rule."""
    current = {
        "clue": checkpoint.clue,
        "scene": checkpoint.challenge.scene,
        "pose": checkpoint.challenge.pose,
        "proximity": checkpoint.proximity,
    }
    changed = {
        field: getattr(edit, field)
        for field in current
        if field in edit.model_fields_set and getattr(edit, field) != current[field]
    }
    edited = checkpoint
    if changed:
        original = checkpoint.original or DraftOriginal()
        kept = {f: current[f] for f in changed if getattr(original, f) is None}
        values = {**current, **changed}
        edited = checkpoint.model_copy(
            update={
                "clue": values["clue"],
                "challenge": checkpoint.challenge.model_copy(
                    update={"scene": values["scene"], "pose": values["pose"]}
                ),
                "proximity": values["proximity"],
                "edited": True,
                "original": original.model_copy(update=kept),
            }
        )
        problems = check_checkpoint(edited, candidate(edited.place))
        if problems:
            raise DraftProblemsError(problems)
    if edit.review is not None:
        edited = edited.model_copy(update={"review": edit.review})
    return edited


# --- publishing ----------------------------------------------------------------------------------

TeamName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class PublishRequest(BaseModel):
    """The session to make of the draft: its name, planned window and teams."""

    model_config = ConfigDict(extra="forbid", alias_generator=_kebab)

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    start_time: AwareDatetime
    end_time: AwareDatetime
    teams: list[TeamName] = Field(min_length=1, max_length=MAX_TEAMS)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.end_time <= self.start_time:
            raise ValueError("end-time must be after start-time")
        names = [name.casefold() for name in self.teams]
        if len(set(names)) != len(names):
            raise ValueError("team names must be unique, ignoring case")
        return self


def readiness(draft: Draft) -> list[DraftCheckpoint]:
    """The accepted checkpoints in route order; `DraftNotReadyError` saying why not."""
    if draft.status == "published":
        raise DraftNotReadyError("draft is already published")
    if draft.status != "ready":
        raise DraftNotReadyError(f"draft is {draft.status}, not ready")
    pending = [c.position for c in draft.checkpoints if c.review == "pending"]
    if pending:
        listed = ", ".join(map(str, pending))
        raise DraftNotReadyError(f"checkpoint(s) {listed} still pending review")
    accepted = sorted(
        (c for c in draft.checkpoints if c.review == "accepted"), key=lambda c: c.position
    )
    if len(accepted) < MIN_ACCEPTED:
        raise DraftNotReadyError(
            f"{len(accepted)} checkpoint(s) accepted; at least {MIN_ACCEPTED} are needed"
        )
    return accepted


def check_accepted(draft: Draft, accepted: Sequence[DraftCheckpoint], min_spacing_m: float) -> None:
    """The accepted checkpoints as a whole draft, in route order, without the rejected ones:
    `DraftProblemsError` if they break a rule."""
    if draft.area is None:  # a ready draft always has its area
        raise DraftNotReadyError("draft has no area")
    request = draft.request.model_copy(update={"checkpoints": len(accepted)})
    candidates = [candidate(c.place) for c in accepted]
    problems = check_draft(accepted, candidates, request, draft.area, min_spacing_m)
    if problems:
        raise DraftProblemsError(problems)


def build_session(
    draft: Draft,
    accepted: Sequence[DraftCheckpoint],
    request: PublishRequest,
    join_codes: Sequence[str],
    moderator_code: str,
    session_id: UUID | None = None,
) -> GameSession:
    """The session: checkpoints 1…n in route order; team i starts at checkpoint i (mod n)."""
    count = len(accepted)
    checkpoints = tuple(
        Checkpoint(
            sequence=sequence,
            name=checkpoint.place.name,
            clue=checkpoint.clue,
            location=checkpoint.place.location,
            proximity=checkpoint.proximity,
            challenge=VisualChallenge(
                scene=checkpoint.challenge.scene, pose=checkpoint.challenge.pose
            ),
        )
        for sequence, checkpoint in enumerate(accepted, start=1)
    )
    teams = tuple(
        Team(name=name, join_code=code, order=rotation(count, index))
        for index, (name, code) in enumerate(zip(request.teams, join_codes, strict=True))
    )
    area = draft.area.name if draft.area is not None else draft.request.area
    return GameSession(
        id=session_id or uuid4(),
        name=request.name,
        location=area,
        start_time=request.start_time,
        end_time=request.end_time,
        checkpoints=checkpoints,
        teams=teams,
        moderator_code=moderator_code,
    )


def rotation(count: int, index: int) -> tuple[int, ...]:
    """The route 1…count, rotated to start at checkpoint `index` (mod count), counting from 0."""
    return tuple((index + step) % count + 1 for step in range(count))


# --- codes -----------------------------------------------------------------------------------


class CodeSource(Protocol):
    """Draws new codes; tests replace it to force a clash."""

    def join_code(self) -> str:
        """A team's join code, e.g. `FOX-7Q2K`."""
        ...

    def moderator_code(self) -> str:
        """A moderator code, at least 12 characters."""
        ...


class SecretCodes:
    """Codes from `secrets`: a short word and four unambiguous characters for a team, and
    `MOD-` and twelve for the moderator."""

    def join_code(self) -> str:
        """`FOX-7Q2K`: 8 characters, about 18 million codes (20 words, 31 characters)."""
        return f"{secrets.choice(JOIN_WORDS)}-{_chars(4)}"

    def moderator_code(self) -> str:
        """`MOD-7Q2K-XJ4M-9PRT`: 12 random characters from 31."""
        return "MOD-" + "-".join(_chars(4) for _ in range(3))


def _chars(count: int) -> str:
    return "".join(secrets.choice(UNAMBIGUOUS) for _ in range(count))


def issue_codes(
    teams: int, source: CodeSource, in_use: Callable[[str], bool]
) -> tuple[list[str], str]:
    """A fresh join code per team and a moderator code: each unused and all different.

    A clash, with an existing code or another of these, draws again.
    """
    issued: set[str] = set()

    def draw(make: Callable[[], str]) -> str:
        for _ in range(CODE_ATTEMPTS):
            code = make()
            if code not in issued and not in_use(code):
                issued.add(code)
                return code
        raise RuntimeError(f"no unused code after {CODE_ATTEMPTS} attempts")

    joins = [draw(source.join_code) for _ in range(teams)]
    return joins, draw(source.moderator_code)


def publication_of(session: GameSession) -> tuple[list[tuple[str, str]], str | None]:
    """A published session's teams with their join codes, and its moderator code."""
    return [(team.name, team.join_code) for team in session.teams], session.moderator_code


def get_code_source() -> CodeSource:
    """Dependency providing the code source: `secrets`, unless a test replaces it."""
    return SecretCodes()
