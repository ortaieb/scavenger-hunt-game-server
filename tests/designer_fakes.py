"""Fakes for the hunt designer's tests: map data from memory, and the SDK's agent loop
(`_run_query`) replaced by a script of tool calls played through the run's real tools."""

import asyncio
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pytest_mock import MockerFixture

from game_server.config import Settings
from game_server.designer import agent
from game_server.designer.agent import DesignResult, HuntRun
from game_server.designer.osm import Area, MapDataError, Place
from game_server.drafts import BoundingBox
from game_server.models import Location

CENTRE = Location(lat=51.4900, long=-0.2600)
AREA = Area(
    name="Chiswick, London, England",
    bbox=BoundingBox(south=51.4800, west=-0.2750, north=51.5000, east=-0.2450),
    centre=CENTRE,
    clipped=False,
)
HOUSE = Place(
    osm="node/1",
    name="Hogarth's House",
    kind="historic=building",
    location=Location(lat=51.4900, long=-0.2600),
    tags={"name": "Hogarth's House", "wikidata": "Q5878384", "start_date": "1700"},
)
CHURCH = Place(
    osm="way/2",
    name="Saint Nicholas Church",
    kind="amenity=place_of_worship",
    location=Location(lat=51.4936, long=-0.2600),  # 400 m north
    tags={"name": "Saint Nicholas Church"},
)
GATE = Place(
    osm="relation/3",
    name="Fuller's Brewery Gate",
    kind="historic=city_gate",
    location=Location(lat=51.4918, long=-0.2545),  # 430 m from each
    tags={"name": "Fuller's Brewery Gate", "inscription": "Griffin Brewery, 1845"},
)
PLACES = [HOUSE, CHURCH, GATE]
MODEL = "claude-test-model"
# Long enough for any test, short enough that a stuck one fails rather than hangs.
WAIT_SECONDS = 10.0


@dataclass
class FakeMaps:
    """Map data from memory, recording what was asked. With a `gate`, `find_places` blocks
    (as a slow request does) until the gate opens."""

    area: Area | None = AREA
    places: list[Place] = field(default_factory=lambda: list(PLACES))
    error: MapDataError | None = None
    asked: list[tuple[str, object]] = field(default_factory=list)
    gate: threading.Event | None = None
    waiting: threading.Event = field(default_factory=threading.Event)

    def find_area(self, query: str) -> Area | None:
        self.asked.append(("find_area", query))
        if self.error is not None:
            raise self.error
        return self.area

    def find_places(self, area: Area, kinds: Sequence[str] | None = None) -> list[Place]:
        self.asked.append(("find_places", kinds))
        if self.gate is not None:
            self.waiting.set()
            self.gate.wait(WAIT_SECONDS)
        if self.error is not None:
            raise self.error
        return list(self.places)


def submitted(of: Place, position: int, **changes: object) -> dict[str, object]:
    """A checkpoint as the model submits it."""
    values: dict[str, object] = {
        "ref": of.osm,
        "clue": f"Clue {position}: where the painter lived by the busy road.",
        "scene": f"Scene {position}: a brick front behind black railings.",
        "pose": "Point at the door with both hands",
        "rationale": "It fits the theme and keeps the loop short.",
    }
    values.update(changes)
    return values


def clean_draft() -> dict[str, list[dict[str, object]]]:
    """A draft of the three places that passes the rules."""
    return {"checkpoints": [submitted(place, i) for i, place in enumerate(PLACES, start=1)]}


def result_message(
    subtype: str = "success", *, is_error: bool = False, cost: float | None = 0.42
) -> ResultMessage:
    """The result Claude Code ends a run with: 7 turns, 61 s."""
    return ResultMessage(
        subtype=subtype,
        duration_ms=61_000,
        duration_api_ms=58_000,
        is_error=is_error,
        num_turns=7,
        session_id="session-1",
        total_cost_usd=cost,
    )


Script = Sequence[tuple[str, Mapping[str, object]]]
SEARCH: Script = [("find_area", {"query": "Chiswick, London"}), ("find_places", {})]
DESIGN: Script = [*SEARCH, ("submit_draft", clean_draft())]


@dataclass
class Replay:
    """Stands in for `_run_query`: plays the tool calls through the run's tools, then ends with
    the result, an exception, or both, as the SDK does; or, with `hang`, waits until it's
    cancelled, and says so in `hanging`."""

    run: HuntRun | None
    script: Script
    end: ResultMessage | None = None
    raises: Exception | None = None
    hang: bool = False
    prompts: list[str] = field(default_factory=list)
    tool_results: list[dict[str, object]] = field(default_factory=list)
    hanging: threading.Event = field(default_factory=threading.Event)

    async def __call__(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        assert self.run is not None, "no run to replay the script on"
        self.prompts.append(prompt)
        for number, (name, args) in enumerate(self.script):
            use = ToolUseBlock(id=f"tool-{number}", name=f"mcp__hunt__{name}", input=dict(args))
            yield AssistantMessage(content=[use], model=MODEL, message_id=f"msg-{number}")
            result = await self.run.call(name, args)
            self.tool_results.append(result)
            content = result["content"]
            assert isinstance(content, list)
            yield UserMessage(content=[ToolResultBlock(use.id, content, False)])
        if self.hang:
            self.hanging.set()
            await asyncio.Event().wait()
        if self.end is not None:
            yield self.end
        if self.raises is not None:
            raise self.raises


def replay_runs(
    mocker: MockerFixture,
    script: Script,
    *,
    end: ResultMessage | None = None,
    raises: Exception | None = None,
    hang: bool = False,
) -> Replay:
    """Replay the script in every run the designer's runner starts, through the real
    `run_agent`: only the SDK's agent loop is replaced."""
    fake = Replay(None, script, end, raises, hang)
    run_agent = agent.run_agent

    async def replayed(run: HuntRun, settings: Settings) -> DesignResult:
        fake.run = run
        return await run_agent(run, settings)

    mocker.patch("game_server.designer_runners.run_agent", replayed)
    mocker.patch("game_server.designer.agent._run_query", fake)
    return fake
