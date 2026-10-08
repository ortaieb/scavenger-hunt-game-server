"""The hunt-designer agent, on the Claude Agent SDK: given an area and a theme, it picks
checkpoints from map data, writes their clues and sets their challenges.

The referee is a workflow; the designer is an agent. It decides which searches to run and which
places fit the theme, how to order them into a walkable loop, and how to fix a draft the rules
send back. It works only through five tools on an in-process MCP server, `hunt`, each on one
run's context (`HuntRun`): the area, and the candidate places seen so far. The server, not the
model, holds every place's coordinates: a draft names its places by ref, and the accepted
draft's coordinates are copied from the candidates.

The agent runs in a narrow sandbox: no built-in tools (no shell, files or web), no settings,
memory or other MCP servers from the machine, an empty working directory, and limits on its
turns, spend and time. Map text reaches the model only inside tool results, as JSON data.

All Claude Agent SDK runs go through `_run_query`; tests replace it.
"""

import asyncio
import json
import logging
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

import anyio
import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    McpSdkServerConfig,
    Message,
    ResultError,
    ResultMessage,
    SdkMcpTool,
    ToolAnnotations,
    create_sdk_mcp_server,
    query,
    tool,
)
from pydantic import BaseModel, Field, ValidationError

from game_server.config import DEFAULT_DESIGNER_MIN_SPACING_M, Settings, get_settings
from game_server.designer.osm import (
    KINDS,
    Area,
    MapDataError,
    Place,
    documented,
    narrow_kinds,
    osm_client,
)
from game_server.designer.rules import (
    NODE_PROXIMITY_M,
    check_draft,
    default_proximity,
    route_legs,
)
from game_server.drafts import (
    DraftArea,
    DraftChallenge,
    DraftCheckpoint,
    DraftPlace,
    DraftRequest,
    Problem,
    RunErrorCode,
)
from game_server.geo import distance_m
from game_server.models import Location

logger = logging.getLogger(__name__)

SERVER_NAME = "hunt"
TOOL_NAMES = ("find_area", "find_places", "place_details", "measure_route", "submit_draft")
ALLOWED_TOOLS = tuple(f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES)
PLACES_SHOWN = 60
# The tags `find_places` shows for each place, each cut to TAG_PREVIEW characters;
# `place_details` shows them all, in full.
KEY_TAGS = (
    "inscription",
    "description",
    "memorial",
    "subject",
    "artist_name",
    "start_date",
    "material",
    "wikipedia",
)
TAG_PREVIEW = 80
# Claude Code keeps a larger result in a file the agent can't read (it has no file tools).
MAX_RESULT_CHARS = 100_000
# Where an unknown place's checkpoint is put for the rules, which leave it out of every
# distance rule: it never reaches a result, since `unknown_place` rejects the draft.
NOWHERE = Location(lat=0, long=0)
SELF_CHECK_TIMEOUT_SECONDS = 30.0
# The model Claude Code names on a message it made up itself, e.g. for an API error.
SYNTHETIC_MODEL = "<synthetic>"

Progress = Callable[[str, str], Awaitable[None]]
"""Told each tool call of a run: the tool (e.g. `find_places`) and its outcome for the
organiser (e.g. "58 candidate places"), never a clue or a scene."""


class MapData(Protocol):
    """Where the places come from: `OsmClient`, or a fake in tests."""

    def find_area(self, query: str) -> Area | None:
        """The area best matching `query`; None if nothing matches."""
        ...

    def find_places(self, area: Area, kinds: Sequence[str] | None = None) -> list[Place]:
        """The candidate places in the area, best-documented first."""
        ...


class AgentUnavailableError(Exception):
    """The bundled Claude Code binary can't be started."""


@dataclass(frozen=True)
class Route:
    """Metres from each checkpoint to the next, closing the loop back to the first."""

    legs_m: tuple[int, ...]
    loop_m: int


@dataclass(frozen=True)
class RunStats:
    """What the run cost; `subtype` is its result's (`success`, `error_max_turns`, ...)."""

    model: str
    turns: int
    cost_usd: Decimal
    duration_ms: int
    subtype: str | None


@dataclass(frozen=True)
class DesignResult:
    """A run's outcome: the area, the accepted checkpoints in route order and the route. On
    failure, an error code, with the last draft submitted (if any) and its problems, to show
    how close the run got. The stats either way."""

    area: DraftArea | None
    checkpoints: tuple[DraftCheckpoint, ...]
    route: Route | None
    stats: RunStats
    error_code: RunErrorCode | None = None
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True)
class AcceptedDraft:
    """A draft `submit_draft` accepted, and the area it was checked against."""

    area: DraftArea
    checkpoints: tuple[DraftCheckpoint, ...]


@dataclass(frozen=True)
class SubmittedDraft:
    """The last draft `submit_draft` checked, accepted or not, and its problems. Only its
    checkpoints at candidate places are kept: every place and location is the map data's."""

    area: DraftArea
    checkpoints: tuple[DraftCheckpoint, ...]
    problems: tuple[Problem, ...]


@dataclass(frozen=True)
class Reply:
    """A tool's answer to the model (JSON), what its log line says, and its progress step's
    summary for the organiser."""

    payload: Mapping[str, object]
    outcome: str
    summary: str
    results: int | None = None
    is_error: bool = False

    def sdk_result(self) -> dict[str, object]:
        """As an SDK tool returns it: one text block holding the JSON."""
        text = json.dumps(self.payload, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "is_error": self.is_error}


# What a tool's error says in the run's progress.
ERROR_SUMMARIES = {
    "area_not_found": "No area matches",
    "no_area": "No area yet",
    "invalid_input": "Invalid input",
    "invalid_kind": "A kind that isn't allowed",
    "unknown_place": "Not a candidate place",
    "map_unavailable": "The map data is unavailable",
    "map_timeout": "The map data timed out",
}


def _error(code: str, message: str) -> Reply:
    summary = ERROR_SUMMARIES.get(code, code)
    return Reply({"error": code, "message": message}, code, summary, is_error=True)


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# --- the tools' inputs --------------------------------------------------------------------


class FindAreaInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class FindPlacesInput(BaseModel):
    kinds: list[str] | None = None


class PlaceDetailsInput(BaseModel):
    ref: str


class MeasureRouteInput(BaseModel):
    refs: list[str] = Field(min_length=1)


class SubmittedCheckpoint(BaseModel):
    """A checkpoint as the model writes it. Anything else it adds (coordinates) is ignored."""

    ref: str
    clue: str
    scene: str
    pose: str
    rationale: str


class SubmitDraftInput(BaseModel):
    checkpoints: list[SubmittedCheckpoint]


def _kinds_allowed() -> str:
    return "; ".join(
        f"{key} (with heritage or wikidata)"
        if values is None
        else f"{key}: {', '.join(sorted(values))}"
        for key, values in KINDS.items()
    )


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it: its description and JSON Schema."""

    description: str
    schema: dict[str, object]
    large_result: bool = False


TOOL_SPECS: dict[str, ToolSpec] = {
    "find_area": ToolSpec(
        "Find the hunt's area by name, e.g. 'Chiswick, London'. Returns its name, its box and "
        "whether it was clipped to the size a walking hunt can cover. Call it first.",
        {
            "type": "object",
            "properties": {"query": _string("The area's name, with its town or city")},
            "required": ["query"],
        },
    ),
    "find_places": ToolSpec(
        "List candidate places in the area: named, public places of the allowed kinds. "
        f"Returns at most {PLACES_SHOWN}, best-documented first, then nearest the area's "
        "centre, each with the ref the other tools take. Kinds narrow the search, e.g. "
        '["historic", "tourism=artwork"]; allowed: ' + _kinds_allowed(),
        {
            "type": "object",
            "properties": {
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keys or key=value pairs from the allowed kinds; all if "
                    "left out",
                }
            },
        },
        large_result=True,
    ),
    "place_details": ToolSpec(
        "All the map tags of one candidate place.",
        {
            "type": "object",
            "properties": {"ref": _string("The place's ref, from find_places")},
            "required": ["ref"],
        },
        large_result=True,
    ),
    "measure_route": ToolSpec(
        "Measure a loop through candidate places, in the order given: the metres from each to "
        "the next, closing the loop back to the first, and the loop's total.",
        {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The places' refs, in route order",
                }
            },
            "required": ["refs"],
        },
    ),
    "submit_draft": ToolSpec(
        "Submit the hunt: its checkpoints in route order. The rules check it and return the "
        "problems to fix, or 'accepted'. Each checkpoint's place is a candidate's ref; its "
        "location comes from the map data.",
        {
            "type": "object",
            "properties": {
                "checkpoints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": _string("The place's ref, from find_places"),
                            "clue": _string("What the players read to find the place"),
                            "scene": _string(
                                "For the referee only: what the photo shows behind the player"
                            ),
                            "pose": _string("What the player does in the photo"),
                            "rationale": _string(
                                "For the organiser: why this place, for the theme and route"
                            ),
                        },
                        "required": ["ref", "clue", "scene", "pose", "rationale"],
                    },
                }
            },
            "required": ["checkpoints"],
        },
    ),
}


# --- one run's context, and its tools -----------------------------------------------------


async def _ignore_progress(step: str, summary: str) -> None:
    """The default `Progress`: tells nobody."""


class HuntRun:
    """One design run: the request, the area, the candidates seen so far, and the last
    draft submitted and the last accepted. Its tools, `call`ed by name, read and change it."""

    def __init__(
        self,
        request: DraftRequest,
        maps: MapData,
        *,
        min_spacing_m: float = DEFAULT_DESIGNER_MIN_SPACING_M,
        on_progress: Progress = _ignore_progress,
    ) -> None:
        self.request = request
        self.area: Area | None = None
        self.candidates: dict[str, Place] = {}
        self.accepted: AcceptedDraft | None = None
        self.submitted: SubmittedDraft | None = None
        self.stopped = False
        self._scope: anyio.CancelScope | None = None
        self._maps = maps
        self._min_spacing_m = min_spacing_m
        self._progress = on_progress
        self._tools: dict[str, Callable[[Mapping[str, object]], Awaitable[Reply]]] = {
            "find_area": self._find_area,
            "find_places": self._find_places,
            "place_details": self._place_details,
            "measure_route": self._measure_route,
            "submit_draft": self._submit_draft,
        }

    async def call(self, name: str, args: Mapping[str, object]) -> dict[str, object]:
        """Run the tool `name` as the agent does: one progress step and one log line, never
        its text."""
        reply = await self._tools[name](args)
        logger.info(
            "Designer tool %s outcome %s results %s",
            name,
            reply.outcome,
            "-" if reply.results is None else reply.results,
        )
        await self._progress(name, reply.summary)
        return reply.sdk_result()

    def stop(self) -> None:
        """Cut the run off, e.g. as the server shuts down: Claude Code is shut down and the
        run fails with `interrupted`, unless it has an accepted draft. Call it on the run's
        event loop."""
        self.stopped = True
        if self._scope is not None:
            self._scope.cancel()

    @contextmanager
    def limited(self, seconds: float) -> Iterator[anyio.CancelScope]:
        """The scope the agent runs in, cancelled `seconds` from now or by `stop`."""
        with anyio.move_on_after(seconds) as scope:
            self._scope = scope
            if self.stopped:
                scope.cancel()
            try:
                yield scope
            finally:
                self._scope = None

    def server(self) -> McpSdkServerConfig:
        """The in-process MCP server holding this run's tools."""
        # Any: the SDK's own tool types, whose input is whatever the tool's JSON Schema allows.
        tools: list[SdkMcpTool[Any]] = []
        for name in TOOL_NAMES:
            spec = TOOL_SPECS[name]
            annotations = (
                ToolAnnotations(maxResultSizeChars=MAX_RESULT_CHARS) if spec.large_result else None
            )
            tools.append(tool(name, spec.description, spec.schema, annotations)(self._sdk(name)))
        return create_sdk_mcp_server(SERVER_NAME, tools=tools)

    def _sdk(self, name: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def handle(args: dict[str, Any]) -> dict[str, Any]:
            return await self.call(name, args)

        return handle

    def _distance_from_centre(self, place: Place) -> int:
        return round(distance_m(self.area.centre, place.location)) if self.area else 0

    async def _find_area(self, args: Mapping[str, object]) -> Reply:
        try:
            wanted = FindAreaInput.model_validate(args).query
            area = await asyncio.to_thread(self._maps.find_area, wanted)
        except ValidationError as exc:
            return _invalid(exc)
        except MapDataError as exc:
            return _error(exc.code, "The map data can't be had right now; try again")
        if area is None:
            return _error("area_not_found", "No area matches; try a fuller name, with the city")
        self.area = area
        summary = area.name + (" (clipped)" if area.clipped else "")
        payload = area.draft_area().model_dump(mode="json", by_alias=True)
        return Reply(payload, "ok", summary, results=1)

    async def _find_places(self, args: Mapping[str, object]) -> Reply:
        if self.area is None:
            return _error("no_area", "Call find_area first")
        try:
            kinds = FindPlacesInput.model_validate(args).kinds or None
            narrow_kinds(kinds)
            found = await asyncio.to_thread(self._maps.find_places, self.area, kinds)
        except ValidationError as exc:
            return _invalid(exc)
        except ValueError as exc:  # narrow_kinds: a kind outside the allowlist
            return _error("invalid_kind", f"{exc}. Allowed: {_kinds_allowed()}")
        except MapDataError as exc:
            return _error(exc.code, "The map data can't be had right now; try again")
        best = sorted(
            found, key=lambda place: (not documented(place), self._distance_from_centre(place))
        )[:PLACES_SHOWN]
        self.candidates.update((place.osm, place) for place in best)
        places = [self._summary(place) for place in best]
        payload = {"found": len(found), "places": places}
        return Reply(payload, "ok", _counted(len(best), "candidate place"), results=len(best))

    def _summary(self, place: Place) -> dict[str, object]:
        """A place in brief: no coordinates, and its key tags cut short."""
        tags = {key: _preview(place.tags[key]) for key in KEY_TAGS if key in place.tags}
        return {
            "ref": place.osm,
            "name": place.name,
            "kind": place.kind,
            "from_centre_m": self._distance_from_centre(place),
            "tags": tags,
        }

    async def _place_details(self, args: Mapping[str, object]) -> Reply:
        try:
            ref = PlaceDetailsInput.model_validate(args).ref
        except ValidationError as exc:
            return _invalid(exc)
        place = self.candidates.get(ref)
        if place is None:
            return _error("unknown_place", "Not a candidate place; use a ref from find_places")
        payload = {**self._summary(place), "tags": dict(place.tags)}
        return Reply(payload, "ok", f"Details of {place.name}", results=1)

    async def _measure_route(self, args: Mapping[str, object]) -> Reply:
        try:
            refs = MeasureRouteInput.model_validate(args).refs
        except ValidationError as exc:
            return _invalid(exc)
        unknown = [ref for ref in refs if ref not in self.candidates]
        if unknown:
            return _error(
                "unknown_place", f"Not candidate places: {', '.join(unknown)}; use find_places"
            )
        route = route_of([self.candidates[ref].location for ref in refs])
        payload = {
            "legs_m": list(route.legs_m),
            "loop_m": route.loop_m,
            "max_loop_m": round(self.request.max_walk_km * 1000),
        }
        summary = f"A loop of {_counted(len(refs), 'place')}, {route.loop_m} m"
        return Reply(payload, "ok", summary, results=len(refs))

    async def _submit_draft(self, args: Mapping[str, object]) -> Reply:
        if self.area is None:
            return _error("no_area", "Call find_area first")
        try:
            submitted = SubmitDraftInput.model_validate(args).checkpoints
        except ValidationError as exc:
            return _invalid(exc)
        checkpoints = tuple(
            self._checkpoint(position, checkpoint)
            for position, checkpoint in enumerate(submitted, start=1)
        )
        problems = check_draft(
            checkpoints, self.candidates.values(), self.request, self.area, self._min_spacing_m
        )
        placed = tuple(c for c in checkpoints if c.place.osm in self.candidates)
        self.submitted = SubmittedDraft(self.area.draft_area(), placed, tuple(problems))
        if problems:
            codes = ", ".join(sorted({problem.code for problem in problems}))
            listed = [problem.model_dump(mode="json", by_alias=True) for problem in problems]
            payload = {"status": "rejected", "problems": listed}
            summary = f"{_counted(len(problems), 'problem')}: {codes}"
            return Reply(payload, "rejected", summary, results=len(problems))
        self.accepted = AcceptedDraft(self.area.draft_area(), checkpoints)
        payload = {"status": "accepted", "message": "The draft is accepted: you're done"}
        return Reply(payload, "accepted", "No problems", results=len(checkpoints))

    def _checkpoint(self, position: int, submitted: SubmittedCheckpoint) -> DraftCheckpoint:
        """The checkpoint, with its place and location from the candidate, never the model."""
        place = self.candidates.get(submitted.ref)
        return DraftCheckpoint(
            position=position,
            place=place.draft_place()
            if place is not None
            else DraftPlace(osm=submitted.ref, name="", kind="", location=NOWHERE),
            clue=submitted.clue,
            challenge=DraftChallenge(scene=submitted.scene, pose=submitted.pose),
            proximity=default_proximity(place) if place is not None else NODE_PROXIMITY_M,
            rationale=submitted.rationale,
        )


def _preview(value: str) -> str:
    return value if len(value) <= TAG_PREVIEW else value[: TAG_PREVIEW - 1] + "…"


def _invalid(exc: ValidationError) -> Reply:
    """The input's problems, by field: never the values."""
    found = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}"
        for error in exc.errors()
    )
    return _error("invalid_input", f"Invalid input: {found}")


def route_of(locations: Sequence[Location]) -> Route:
    """The loop through the locations, in metres."""
    legs = tuple(round(leg) for leg in route_legs(locations))
    return Route(legs_m=legs, loop_m=sum(legs))


# --- the agent ----------------------------------------------------------------------------


def system_prompt() -> str:
    """The designer's system prompt, shipped with the package."""
    return files("game_server").joinpath("designer_prompt.md").read_text(encoding="utf-8")


def user_prompt(request: DraftRequest) -> str:
    """The run's request, as the organiser made it. Never any map text."""
    return (
        "Design a scavenger hunt.\n\n"
        f"Area: {request.area}\n"
        f"Theme: {request.theme}\n"
        f"Checkpoints: {request.checkpoints}\n"
        f"Longest walk: {request.max_walk_km:g} km, round the whole loop\n"
    )


def api_key(settings: Settings) -> str | None:
    """The Anthropic key, or None when it's unset or empty."""
    key = settings.anthropic_api_key
    return (key.get_secret_value() or None) if key is not None else None


def agent_options(
    settings: Settings, server: McpSdkServerConfig, cwd: Path, config_dir: Path
) -> ClaudeAgentOptions:
    """The sandbox: only the `hunt` tools, nothing from the machine, and the limits."""
    return ClaudeAgentOptions(
        tools=[],  # no built-in tools: no shell, files, web search or web fetch
        allowed_tools=list(ALLOWED_TOOLS),
        mcp_servers={SERVER_NAME: server},
        strict_mcp_config=True,
        permission_mode="dontAsk",
        setting_sources=[],
        skills=[],  # none shown to the model, which has no Skill tool to use them
        system_prompt=system_prompt(),
        # The organiser's text goes to the model as written: no `@file` expansion.
        verbatim_prompts=True,
        model=settings.designer_model,
        max_turns=settings.designer_max_turns,
        max_budget_usd=settings.designer_max_budget_usd,
        cwd=cwd,
        env={
            "ANTHROPIC_API_KEY": api_key(settings) or "",
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        },
    )


def _run_query(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    """The Claude Agent SDK's `query`: the only call into the SDK's agent loop."""
    return query(prompt=prompt, options=options)


@dataclass
class _Stream:
    """What a run's messages said: the model that answered, the turns seen, and the result;
    or that the run was cut off before it had one."""

    model: str | None = None
    messages: set[str] = field(default_factory=set)
    result: ResultMessage | None = None
    error_subtype: str | None = None
    cut_off: RunErrorCode | None = None


async def _read(prompt: str, options: ClaudeAgentOptions, stream: _Stream) -> None:
    """Run the agent into `stream`; an SDK failure ends it, and only the result says why."""
    try:
        async for message in _run_query(prompt, options):
            if isinstance(message, AssistantMessage) and message.model != SYNTHETIC_MODEL:
                stream.model = stream.model or message.model
                if message.message_id is not None:  # one API response may be several messages
                    stream.messages.add(message.message_id)
            elif isinstance(message, ResultMessage):
                stream.result = message
    except ResultError as exc:  # the CLI exits non-zero after an error result
        stream.error_subtype = exc.subtype
    except ClaudeSDKError as exc:  # Claude Code not found, didn't start, or broke off
        logger.warning("Designer agent unavailable: %s", type(exc).__name__)


def error_code(stream: _Stream, accepted: AcceptedDraft | None) -> RunErrorCode | None:
    """None when a draft was accepted, whatever ended the run after; else why there's none."""
    if accepted is not None:
        return None
    if stream.cut_off is not None:
        return stream.cut_off
    subtype = stream.result.subtype if stream.result is not None else stream.error_subtype
    if subtype == "error_max_turns":
        return "max_turns"
    if subtype == "error_max_budget_usd":
        return "max_budget"
    if stream.result is None or stream.result.is_error:
        return "agent_unavailable"
    return "no_valid_draft"


def _result(run: HuntRun, stream: _Stream, settings: Settings, started: float) -> DesignResult:
    result = stream.result
    stats = RunStats(
        model=stream.model or settings.designer_model,
        turns=result.num_turns if result is not None else len(stream.messages),
        # A run cut off before its result never learns its cost.
        cost_usd=Decimal(str(result.total_cost_usd or 0)) if result is not None else Decimal(0),
        duration_ms=result.duration_ms
        if result is not None
        else round((time.monotonic() - started) * 1000),
        subtype=result.subtype if result is not None else stream.error_subtype,
    )
    code = error_code(stream, run.accepted)
    logger.info(
        "Designer run outcome %s subtype %s turns %d cost_usd %s duration_ms %d",
        code or "accepted",
        stats.subtype or "-",
        stats.turns,
        stats.cost_usd,
        stats.duration_ms,
    )
    if code is None and run.accepted is not None:
        checkpoints = run.accepted.checkpoints
        return DesignResult(run.accepted.area, checkpoints, _route(checkpoints), stats)
    submitted = run.submitted
    if submitted is None:
        area = run.area.draft_area() if run.area is not None else None
        return DesignResult(area, (), None, stats, error_code=code)
    checkpoints = submitted.checkpoints
    return DesignResult(
        submitted.area, checkpoints, _route(checkpoints), stats, code, submitted.problems
    )


def _route(checkpoints: Sequence[DraftCheckpoint]) -> Route | None:
    return route_of([c.place.location for c in checkpoints]) if checkpoints else None


async def run_agent(run: HuntRun, settings: Settings) -> DesignResult:
    """Let the agent design the run's hunt; the last draft it got accepted is the result.

    Past the deadline, or when the run is stopped, the agent is cut off: the SDK shuts
    Claude Code down, and the run ends as it stands.
    """
    started = time.monotonic()
    stream = _Stream()
    if api_key(settings) is None:
        return _result(run, stream, settings, started)
    with (
        TemporaryDirectory(prefix="designer-cwd-") as cwd,
        TemporaryDirectory(prefix="designer-config-") as config_dir,
    ):
        options = agent_options(settings, run.server(), Path(cwd), Path(config_dir))
        with run.limited(settings.designer_deadline_seconds) as limit:
            await _read(user_prompt(run.request), options, stream)
    if limit.cancel_called and stream.result is None:
        stream.cut_off = "interrupted" if run.stopped else "deadline"
    return _result(run, stream, settings, started)


async def design_hunt(
    request: DraftRequest,
    *,
    on_progress: Progress,
    settings: Settings | None = None,
    maps: MapData | None = None,
) -> DesignResult:
    """Design a hunt for the request, on the OpenStreetMap data by default."""
    settings = settings or get_settings()
    run = HuntRun(
        request,
        maps or osm_client(settings),
        min_spacing_m=settings.designer_min_spacing_m,
        on_progress=on_progress,
    )
    return await run_agent(run, settings)


# --- the self-check -----------------------------------------------------------------------


def bundled_cli() -> Path:
    """The Claude Code binary in the SDK's platform wheel."""
    name = "claude.exe" if sys.platform == "win32" else "claude"
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / name


def cli_version(binary: Path, timeout_seconds: float = SELF_CHECK_TIMEOUT_SECONDS) -> str:
    """Start the binary and return the version it reports; `AgentUnavailableError` if it can't."""
    try:
        # S603: the binary is the SDK's own, run with a fixed argument and no shell.
        done = subprocess.run(  # noqa: S603
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentUnavailableError(f"{binary} didn't start: {exc}") from exc
    if done.returncode != 0:
        raise AgentUnavailableError(f"{binary} exited with status {done.returncode}")
    return done.stdout.strip()
