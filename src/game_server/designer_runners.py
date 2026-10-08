"""What fills a draft: the hunt-designer agent, or a stub with a fixed hunt.

A runner's `start` returns at once; the run then updates the draft through the `DraftStore`.
`GAME_SERVER_DESIGNER_RUNNER` picks one: `agent` (the default) or `stub`, which lets the
designer screen be built and tested against a running server before the agent exists.
"""

import logging
import threading
import time
from decimal import Decimal
from typing import Annotated, Protocol
from uuid import UUID

import psycopg
from fastapi import Depends

from game_server.clock import Clock, get_clock, utc_iso
from game_server.config import Settings, get_settings
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


class AgentRunner:
    """The hunt-designer agent. Not built yet (#86): never available."""

    name = "agent"

    def available(self) -> bool:
        """Never, until the agent lands."""
        return False

    def start(self, draft_id: UUID, request: DraftRequest) -> None:
        """Unreachable: the designer refuses before starting an unavailable runner."""
        raise RuntimeError("the hunt-designer agent is not available")


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
        "Draft %s finished status %s checkpoints %d cost_usd %s",
        draft_id,
        result.status,
        len(result.checkpoints),
        result.cost_usd,
    )


def get_designer_runner(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[DraftStore, Depends(get_draft_store)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> DesignerRunner:
    """Dependency providing the configured runner."""
    if settings.designer_runner == "stub":
        return StubRunner(store, clock, settings.designer_stub_delay_seconds)
    return AgentRunner()
