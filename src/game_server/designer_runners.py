"""What fills a draft: the hunt-designer agent, or a stub with a fixed hunt.

A runner's `start` returns at once; the run then updates the draft through the `DraftStore`.
`GAME_SERVER_DESIGNER_RUNNER` picks one: `agent` (the default) or `stub`, a fixed hunt for
local development and the web app's tests.

The agent runner lives as long as the app: its lifespan opens it on the app's event loop and
closes it at shutdown, which stops any run as `interrupted`. At startup, drafts a previous
process left `running` are failed the same way: their runs died with it.
"""

import asyncio
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Protocol
from uuid import UUID

import psycopg
from fastapi import Depends, Request

from game_server.clock import Clock, get_clock, utc_iso
from game_server.config import Settings, get_settings
from game_server.designer.agent import (
    AgentUnavailableError,
    DesignResult,
    HuntRun,
    MapData,
    api_key,
    bundled_cli,
    cli_version,
    run_agent,
)
from game_server.designer.osm import osm_client
from game_server.drafts import (
    BoundingBox,
    DraftArea,
    DraftChallenge,
    DraftCheckpoint,
    DraftPlace,
    DraftRequest,
    DraftResult,
    DraftStore,
    ProgressEntry,
    get_draft_store,
)
from game_server.models import Location

logger = logging.getLogger(__name__)

# How long shutdown waits for stopped runs to end: the SDK takes up to about 20 s to shut
# Claude Code down.
STOP_TIMEOUT_SECONDS = 30.0


class DesignerRunner(Protocol):
    """Starts a design run for a stored `running` draft."""

    @property
    def name(self) -> str:
        """Recorded with the draft: `stub` or `agent`."""
        ...

    def available(self) -> bool:
        """Whether it can run now; if not, the request is refused and nothing is stored."""
        ...

    def start(self, draft_id: UUID, request: DraftRequest) -> None:
        """Start the run and return at once."""
        ...


@dataclass(frozen=True)
class _Run:
    """A run in progress: the agent's context, and the task running it."""

    hunt: HuntRun
    future: Future[None]


class AgentRunner:
    """The hunt-designer agent: each run is a task on the app's event loop.

    The agent's own work happens in Claude Code's process and in async tool calls; the tools'
    map requests and the runner's database writes run in threads, so players' requests are
    served while a design runs. One run at a time is the store's rule (`designer_busy`).
    """

    name = "agent"

    def __init__(
        self, store: DraftStore, clock: Clock, settings: Settings, maps: MapData | None = None
    ) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings
        self._maps = maps
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runs: dict[UUID, _Run] = {}
        self._lock = threading.Lock()

    async def open(self) -> None:
        """Run on the current event loop from now on. With a key, check that Claude Code
        starts, and log the outcome: the deploy's logs then show it (the image has no shell
        to run `--self-check` in)."""
        self._loop = asyncio.get_running_loop()
        if api_key(self._settings) is None:
            return
        try:
            version = await asyncio.to_thread(cli_version, bundled_cli())
        except AgentUnavailableError as exc:
            logger.warning("Hunt designer self-check failed: %s", exc)
        else:
            logger.info("Hunt designer self-check: %s", version)

    def available(self) -> bool:
        """Once open, with an Anthropic key."""
        return self._loop is not None and api_key(self._settings) is not None

    def start(self, draft_id: UUID, request: DraftRequest) -> None:
        """Start the run as a task on the app's event loop, from any thread."""
        loop = self._loop
        if loop is None:
            raise RuntimeError("the agent runner is not open")

        async def progress(step: str, summary: str) -> None:
            await self._progress(draft_id, step, summary)

        hunt = HuntRun(
            request,
            self._maps or osm_client(self._settings),
            min_spacing_m=self._settings.designer_min_spacing_m,
            on_progress=progress,
        )
        with self._lock:
            future = asyncio.run_coroutine_threadsafe(self._run(draft_id, hunt), loop)
            self._runs[draft_id] = _Run(hunt, future)
        future.add_done_callback(lambda _: self._forget(draft_id))

    async def join(self) -> None:
        """Wait for every run started so far to end."""
        futures = self._futures()
        if futures:
            await asyncio.wait(futures)

    async def close(self) -> None:
        """Start nothing more, and stop every run: each is shut down and its draft fails
        with `interrupted`, unless it has an accepted draft. Waits for them, but not forever:
        a run that outlives the wait is failed at the next startup."""
        self._loop = None
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            run.hunt.stop()
        futures = self._futures()
        if not futures:
            return
        _, pending = await asyncio.wait(futures, timeout=STOP_TIMEOUT_SECONDS)
        if pending:
            logger.warning(
                "%d design runs didn't stop within %s s", len(pending), STOP_TIMEOUT_SECONDS
            )

    def _futures(self) -> list[asyncio.Future[None]]:
        with self._lock:
            return [asyncio.wrap_future(run.future) for run in self._runs.values()]

    def _forget(self, draft_id: UUID) -> None:
        with self._lock:
            self._runs.pop(draft_id, None)

    async def _run(self, draft_id: UUID, hunt: HuntRun) -> None:
        started = time.monotonic()
        try:
            result = draft_result(await run_agent(hunt, self._settings))
        except Exception as exc:
            # A background task's error reaches nobody: fail the draft rather than leave it
            # running, which would block every design until a restart. Only the error's type
            # is logged: its message may quote a clue or a scene.
            logger.error("Draft %s: the design run failed (%s)", draft_id, type(exc).__name__)
            result = DraftResult(
                status="failed",
                area=None,
                checkpoints=(),
                problems=(),
                model=self._settings.designer_model,
                turns=0,
                cost_usd=Decimal(0),
                duration_ms=round((time.monotonic() - started) * 1000),
                error_code="agent_unavailable",
            )
        await self._finish(draft_id, result)

    async def _progress(self, draft_id: UUID, step: str, summary: str) -> None:
        entry = ProgressEntry(at=utc_iso(self._clock()), step=step, summary=summary)
        try:
            await asyncio.to_thread(self._store.append_progress, draft_id, entry)
        except psycopg.Error as exc:
            logger.warning(
                "Draft %s: couldn't add a progress step (%s)", draft_id, type(exc).__name__
            )

    async def _finish(self, draft_id: UUID, result: DraftResult) -> None:
        try:
            finished = await asyncio.to_thread(self._store.finish, draft_id, result, self._clock())
        except psycopg.Error:
            logger.exception("Draft %s: the design run couldn't write its result", draft_id)
            return
        if finished:
            log_finished(draft_id, result)


def draft_result(design: DesignResult) -> DraftResult:
    """A run's result as its draft keeps it: `ready`, or `failed` with its code and, if the
    run got that far, its last draft and that draft's problems."""
    stats = design.stats
    return DraftResult(
        status="ready" if design.error_code is None else "failed",
        area=design.area,
        checkpoints=design.checkpoints,
        problems=design.problems,
        model=stats.model,
        turns=stats.turns,
        cost_usd=stats.cost_usd,
        duration_ms=stats.duration_ms,
        error_code=design.error_code,
    )


async def interrupt_left_running(store: DraftStore, clock: Clock) -> None:
    """Fail the drafts a previous process left `running` with `interrupted`.

    If the database can't be reached, they stay `running` (and block new designs) until the
    next startup that can reach it.
    """
    try:
        drafts = await asyncio.to_thread(store.interrupt_running, clock())
    except psycopg.Error as exc:
        logger.warning(
            "Drafts left running not checked: database unavailable (%s)", type(exc).__name__
        )
        return
    for draft_id in drafts:
        logger.info("Draft %s finished status failed error interrupted", draft_id)


# The stub's fixed hunt: fictional places around a fixed point.
STUB_CENTRE = Location(lat=51.4900, long=-0.2600)
STUB_PLACES = (
    ("node/9000000001", "Stub Lantern Gate", "historic=memorial", 0.0012, -0.0020),
    ("node/9000000002", "Stub Riverside Bench", "leisure=park", -0.0025, 0.0015),
    ("node/9000000003", "Stub Brewers' Arch", "historic=building", 0.0018, 0.0030),
)
STUB_PROGRESS = (
    ("resolve_area", "Stub area around a fixed point"),
    ("find_places", "3 candidate places"),
    ("write_clues", "3 clues and challenges written"),
    ("check_draft", "No problems"),
)


def stub_checkpoints() -> tuple[DraftCheckpoint, ...]:
    """The stub's three checkpoints. Their text is within the sessions file's limits."""
    return tuple(
        DraftCheckpoint(
            position=position,
            place=DraftPlace(
                osm=osm,
                name=name,
                kind=kind,
                location=Location(lat=STUB_CENTRE.lat + d_lat, long=STUB_CENTRE.long + d_long),
            ),
            clue=f"A stub clue: find the {name.removeprefix('Stub ').lower()}.",
            challenge=DraftChallenge(
                scene=f"The {name.removeprefix('Stub ').lower()}, seen from the path.",
                pose="Point at it with both hands",
            ),
            proximity=40,
            rationale="A fixed stub checkpoint, for building the designer screen.",
        )
        for position, (osm, name, kind, d_lat, d_long) in enumerate(STUB_PLACES, start=1)
    )


def stub_area(request: DraftRequest) -> DraftArea:
    """The stub's area: the request's name, around the fixed point."""
    return DraftArea(
        name=f"{request.area} (stub)",
        bbox=BoundingBox(
            south=STUB_CENTRE.lat - 0.01,
            west=STUB_CENTRE.long - 0.01,
            north=STUB_CENTRE.lat + 0.01,
            east=STUB_CENTRE.long + 0.01,
        ),
        clipped=False,
    )


class StubRunner:
    """After a delay, fills the draft with a fixed `ready` hunt of three checkpoints.

    Runs on a daemon thread. `wake` skips the delay; `stop` abandons waiting runs (tests use
    both, so a run never outlives its test's database).
    """

    name = "stub"

    def __init__(self, store: DraftStore, clock: Clock, delay_seconds: float) -> None:
        self._store = store
        self._clock = clock
        self._delay = delay_seconds
        self._wake = threading.Event()
        self._stopped = False
        self._threads: list[threading.Thread] = []

    def available(self) -> bool:
        """Always."""
        return True

    def start(self, draft_id: UUID, request: DraftRequest) -> None:
        """Fill the draft on a thread of its own."""
        thread = threading.Thread(
            target=self._run, args=(draft_id, request), name=f"stub-{draft_id}", daemon=True
        )
        self._threads.append(thread)
        thread.start()

    def wake(self) -> None:
        """Finish waiting runs now."""
        self._wake.set()

    def stop(self) -> None:
        """Abandon waiting runs, and wait for every run to end."""
        self._stopped = True
        self._wake.set()
        self.join()

    def join(self) -> None:
        """Wait for every run started so far to end."""
        for thread in self._threads:
            thread.join()

    def _run(self, draft_id: UUID, request: DraftRequest) -> None:
        started = time.monotonic()
        self._wake.wait(self._delay)
        if self._stopped:
            return
        try:
            for step, summary in STUB_PROGRESS:
                entry = ProgressEntry(at=utc_iso(self._clock()), step=step, summary=summary)
                self._store.append_progress(draft_id, entry)
            result = DraftResult(
                status="ready",
                area=stub_area(request),
                checkpoints=stub_checkpoints(),
                problems=(),
                model=None,
                turns=0,
                cost_usd=Decimal(0),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            if self._store.finish(draft_id, result, self._clock()):
                log_finished(draft_id, result)
        except psycopg.Error:
            logger.exception("Draft %s: the stub run couldn't write its result", draft_id)


def log_finished(draft_id: UUID, result: DraftResult) -> None:
    """One line per finished draft: never its clues, scenes or coordinates."""
    logger.info(
        "Draft %s finished status %s checkpoints %d cost_usd %s%s",
        draft_id,
        result.status,
        len(result.checkpoints),
        result.cost_usd,
        f" error {result.error_code}" if result.error_code else "",
    )


def get_agent_runner(request: Request) -> AgentRunner | None:
    """The app's agent runner, which its lifespan opens; None outside it."""
    runner = getattr(request.app.state, "agent_runner", None)
    return runner if isinstance(runner, AgentRunner) else None


def get_designer_runner(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[DraftStore, Depends(get_draft_store)],
    clock: Annotated[Clock, Depends(get_clock)],
    agent: Annotated[AgentRunner | None, Depends(get_agent_runner)],
) -> DesignerRunner:
    """Dependency providing the configured runner."""
    if settings.designer_runner == "stub":
        return StubRunner(store, clock, settings.designer_stub_delay_seconds)
    if agent is not None:
        return agent
    return AgentRunner(store, clock, settings)  # never opened, so never available
