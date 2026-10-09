"""The designer's runners: the stub's fixed hunt, and the agent, with the SDK's agent loop
replayed from a script (no API calls)."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from claude_agent_sdk import CLINotFoundError, ResultError, ResultMessage
from designer_fakes import (
    AREA,
    CHURCH,
    DESIGN,
    HOUSE,
    MODEL,
    SEARCH,
    WAIT_SECONDS,
    FakeMaps,
    Script,
    replay_runs,
    result_message,
    submitted,
)
from pydantic import SecretStr
from pytest_mock import MockerFixture

from game_server.config import Settings
from game_server.database import Database
from game_server.designer.agent import AgentUnavailableError
from game_server.designer.review import candidate
from game_server.designer.rules import check_draft
from game_server.designer_runners import (
    AgentRunner,
    StubRunner,
    get_designer_runner,
    interrupt_left_running,
    stub_area,
    stub_checkpoints,
)
from game_server.drafts import Draft, DraftRequest, DraftResult, DraftStore
from game_server.sessions import Checkpoint, VisualChallenge

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
REQUEST = DraftRequest.model_validate({"area": "Chiswick", "theme": "Brewing"})


@pytest.fixture
def store(database: Database) -> DraftStore:
    return DraftStore(database)


def test_the_stub_hunt_passes_the_sessions_file_limits() -> None:
    for checkpoint in stub_checkpoints():
        Checkpoint(
            sequence=checkpoint.position,
            name=checkpoint.place.name,
            clue=checkpoint.clue,
            location=checkpoint.place.location,
            proximity=checkpoint.proximity,
            challenge=VisualChallenge(
                scene=checkpoint.challenge.scene, pose=checkpoint.challenge.pose
            ),
        )


def test_the_stub_hunt_has_three_distinct_places() -> None:
    checkpoints = stub_checkpoints()

    assert [c.position for c in checkpoints] == [1, 2, 3]
    assert len({c.place.location for c in checkpoints}) == 3
    assert len({c.place.osm for c in checkpoints}) == 3


def test_the_stub_finishes_after_its_delay(store: DraftStore) -> None:
    draft = uuid4()
    store.create(draft, REQUEST, "stub", T0)
    runner = StubRunner(store, lambda: T0, delay_seconds=0)

    runner.start(draft, REQUEST)
    runner.join()

    found = store.get(draft)
    assert found is not None
    assert (found.status, len(found.checkpoints), len(found.progress)) == ("ready", 3, 4)


def test_a_stopped_stub_writes_nothing(store: DraftStore) -> None:
    draft = uuid4()
    store.create(draft, REQUEST, "stub", T0)
    runner = StubRunner(store, lambda: T0, delay_seconds=60)

    runner.start(draft, REQUEST)
    runner.stop()

    found = store.get(draft)
    assert found is not None
    assert (found.status, found.progress) == ("running", ())


@pytest.mark.parametrize(
    ("runner", "name"), [("stub", "stub"), ("agent", "agent")], ids=["stub", "agent"]
)
def test_the_setting_picks_the_runner(store: DraftStore, runner: str, name: str) -> None:
    settings = Settings(designer_runner=runner)

    assert get_designer_runner(settings, store, lambda: T0, None).name == name


def test_the_agent_is_the_default() -> None:
    assert Settings().designer_runner == "agent"
    assert Settings().designer_stub_delay_seconds == 1


# --- the agent runner ----------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key=SecretStr("sk-ant-test-key"), designer_model=MODEL)


@pytest.fixture
def maps() -> FakeMaps:
    return FakeMaps()


@pytest.fixture
def agent(store: DraftStore, settings: Settings, maps: FakeMaps) -> AgentRunner:
    return AgentRunner(store, lambda: T0, settings, maps)


def started(store: DraftStore) -> UUID:
    draft = uuid4()
    store.create(draft, REQUEST, "agent", T0)
    return draft


def designed(agent: AgentRunner, store: DraftStore) -> Draft:
    """Start a run on a fresh draft, wait for it to end, and read the draft back."""
    draft = started(store)

    async def run() -> None:
        await agent.open()
        agent.start(draft, REQUEST)
        await agent.join()

    asyncio.run(run())
    found = store.get(draft)
    assert found is not None
    return found


def test_the_agent_runner_is_available_once_open_with_a_key(
    store: DraftStore, settings: Settings
) -> None:
    async def availability(runner: AgentRunner) -> tuple[bool, bool]:
        before = runner.available()
        await runner.open()
        return before, runner.available()

    with_key = AgentRunner(store, lambda: T0, settings)
    without = AgentRunner(
        store, lambda: T0, settings.model_copy(update={"anthropic_api_key": None})
    )

    assert asyncio.run(availability(with_key)) == (False, True)
    assert asyncio.run(availability(without)) == (False, False)


def test_opening_with_a_key_logs_that_claude_code_starts(
    agent: AgentRunner, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="game_server.designer_runners")

    asyncio.run(agent.open())

    assert "Hunt designer self-check: " in caplog.text
    assert "(Claude Code)" in caplog.text


def test_opening_logs_a_failed_self_check(
    agent: AgentRunner, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch(
        "game_server.designer_runners.cli_version",
        side_effect=AgentUnavailableError("claude exited with status 127"),
    )

    asyncio.run(agent.open())

    assert "Hunt designer self-check failed: claude exited with status 127" in caplog.text


def test_opening_without_a_key_checks_nothing(
    store: DraftStore, settings: Settings, mocker: MockerFixture
) -> None:
    check = mocker.patch("game_server.designer_runners.cli_version")
    runner = AgentRunner(store, lambda: T0, settings.model_copy(update={"anthropic_api_key": None}))

    asyncio.run(runner.open())

    check.assert_not_called()


def test_starting_an_unopened_agent_runner_is_an_error(agent: AgentRunner) -> None:
    with pytest.raises(RuntimeError, match="not open"):
        agent.start(uuid4(), REQUEST)


def test_a_good_run_makes_a_ready_draft_with_its_stats(
    mocker: MockerFixture, agent: AgentRunner, store: DraftStore
) -> None:
    replay_runs(mocker, DESIGN, end=result_message())

    draft = designed(agent, store)

    assert draft.status == "ready"
    assert draft.area == AREA.draft_area()
    assert [c.place.osm for c in draft.checkpoints] == ["node/1", "way/2", "relation/3"]
    assert {c.review for c in draft.checkpoints} == {"pending"}
    assert (draft.model, draft.turns, draft.cost_usd, draft.duration_ms) == (
        MODEL,
        7,
        Decimal("0.42"),
        61_000,
    )
    assert (draft.problems, draft.error_code, draft.finished_at) == ((), None, T0)


def test_each_tool_call_is_a_progress_step(
    mocker: MockerFixture, agent: AgentRunner, store: DraftStore
) -> None:
    replay_runs(mocker, DESIGN, end=result_message())

    draft = designed(agent, store)

    assert [(p.at, p.step, p.summary) for p in draft.progress] == [
        ("2026-10-08T09:00:00Z", "find_area", "Chiswick, London, England"),
        ("2026-10-08T09:00:00Z", "find_places", "3 candidate places"),
        ("2026-10-08T09:00:00Z", "submit_draft", "No problems"),
    ]


REJECTED: Script = [*SEARCH, ("submit_draft", {"checkpoints": [submitted(HOUSE, 1)]})]


@dataclass(frozen=True)
class Ending:
    """How a replayed run ends: a result, an exception, both, or never."""

    end: ResultMessage | None = None
    raises: Exception | None = None
    hang: bool = False


def limit_hit(subtype: str) -> Ending:
    """A run Claude Code stops at a limit: it reports the error result, then exits non-zero."""
    error = ResultError("stopped", data={"subtype": subtype, "is_error": True}, exit_code=1)
    return Ending(result_message(subtype, is_error=True), error)


@pytest.mark.parametrize(
    ("script", "ending", "code"),
    [
        pytest.param(SEARCH, limit_hit("error_max_turns"), "max_turns", id="max-turns"),
        pytest.param(SEARCH, limit_hit("error_max_budget_usd"), "max_budget", id="max-budget"),
        pytest.param(REJECTED, Ending(result_message()), "no_valid_draft", id="no-valid-draft"),
        pytest.param(
            [], Ending(raises=CLINotFoundError("not found")), "agent_unavailable", id="no-cli"
        ),
        pytest.param(
            SEARCH, Ending(result_message(is_error=True)), "agent_unavailable", id="api-error"
        ),
        pytest.param(REJECTED, Ending(hang=True), "deadline", id="deadline"),
    ],
)
def test_each_failure_fails_the_draft_with_its_code(
    mocker: MockerFixture,
    store: DraftStore,
    settings: Settings,
    maps: FakeMaps,
    script: Script,
    ending: Ending,
    code: str,
) -> None:
    replay_runs(mocker, script, end=ending.end, raises=ending.raises, hang=ending.hang)
    if ending.hang:  # only a run that never ends meets the deadline
        settings = settings.model_copy(update={"designer_deadline_seconds": 0.5})

    draft = designed(AgentRunner(store, lambda: T0, settings, maps), store)

    assert (draft.status, draft.error_code, draft.finished_at) == ("failed", code, T0)


def test_a_failed_run_keeps_its_last_submitted_draft_and_its_problems(
    mocker: MockerFixture, agent: AgentRunner, store: DraftStore
) -> None:
    last = {"checkpoints": [submitted(HOUSE, 1), submitted(CHURCH, 2)]}
    limit = limit_hit("error_max_turns")
    replay_runs(mocker, [*REJECTED, ("submit_draft", last)], end=limit.end, raises=limit.raises)

    draft = designed(agent, store)

    assert (draft.status, draft.error_code) == ("failed", "max_turns")
    assert draft.area == AREA.draft_area()
    assert [c.place.osm for c in draft.checkpoints] == ["node/1", "way/2"]
    assert [(p.code, p.position) for p in draft.problems] == [("wrong_count", None)]
    assert (draft.turns, draft.cost_usd) == (7, Decimal("0.42"))


def test_closing_interrupts_a_run_and_keeps_what_it_had(
    mocker: MockerFixture, agent: AgentRunner, store: DraftStore
) -> None:
    fake = replay_runs(mocker, REJECTED, hang=True)
    draft = started(store)

    async def close_midway() -> None:
        await agent.open()
        agent.start(draft, REQUEST)
        assert await asyncio.to_thread(fake.hanging.wait, WAIT_SECONDS)
        await agent.close()

    asyncio.run(close_midway())

    found = store.get(draft)
    assert found is not None
    assert (found.status, found.error_code) == ("failed", "interrupted")
    assert [c.place.osm for c in found.checkpoints] == ["node/1"]
    assert [p.code for p in found.problems] == ["wrong_count"]
    assert [p.step for p in found.progress] == ["find_area", "find_places", "submit_draft"]
    assert not agent.available()


def test_an_unexpected_error_fails_the_draft_without_logging_its_message(
    mocker: MockerFixture,
    agent: AgentRunner,
    store: DraftStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    mocker.patch(
        "game_server.designer_runners.run_agent",
        side_effect=ValueError("Clue 1: where the painter lived"),
    )

    draft = designed(agent, store)

    assert (draft.status, draft.error_code, draft.model) == ("failed", "agent_unavailable", MODEL)
    assert f"Draft {draft.id}: the design run failed (ValueError)" in caplog.text
    assert "painter" not in caplog.text


def test_a_progress_step_that_cant_be_written_doesnt_stop_the_run(
    mocker: MockerFixture,
    agent: AgentRunner,
    store: DraftStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    replay_runs(mocker, DESIGN, end=result_message())
    mocker.patch.object(
        DraftStore, "append_progress", side_effect=psycopg.OperationalError("server closed")
    )

    draft = designed(agent, store)

    assert (draft.status, draft.progress) == ("ready", ())
    assert "couldn't add a progress step (OperationalError)" in caplog.text


def test_each_finished_run_logs_one_line_with_its_error(
    mocker: MockerFixture,
    agent: AgentRunner,
    store: DraftStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="game_server.designer_runners")
    replay_runs(mocker, REJECTED, end=result_message())

    draft = designed(agent, store)

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Draft")]
    assert lines == [
        f"Draft {draft.id} finished status failed checkpoints 1 cost_usd 0.42 error no_valid_draft"
    ]


# --- drafts left running ---------------------------------------------------------------------


def test_drafts_left_running_are_interrupted(
    store: DraftStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="game_server.designer_runners")
    running, ready = uuid4(), uuid4()
    store.create(ready, REQUEST, "agent", T0)
    store.finish(ready, READY, T0)
    store.create(running, REQUEST, "agent", T0)
    later = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)

    asyncio.run(interrupt_left_running(store, lambda: later))

    interrupted, untouched = store.get(running), store.get(ready)
    assert interrupted is not None and untouched is not None
    assert (interrupted.status, interrupted.error_code) == ("failed", "interrupted")
    assert (interrupted.finished_at, interrupted.duration_ms) == (later, None)
    assert (untouched.status, untouched.finished_at) == ("ready", T0)
    lines = [r.getMessage() for r in caplog.records if r.name == "game_server.designer_runners"]
    assert lines == [f"Draft {running} finished status failed error interrupted"]


def test_without_the_database_drafts_left_running_are_left(
    store: DraftStore, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(
        DraftStore, "interrupt_running", side_effect=psycopg.OperationalError("refused")
    )

    asyncio.run(interrupt_left_running(store, lambda: T0))

    assert "Drafts left running not checked: database unavailable (OperationalError)" in (
        caplog.text
    )


READY = DraftResult(
    status="ready",
    area=None,
    checkpoints=stub_checkpoints(),
    problems=(),
    model=None,
    turns=0,
    cost_usd=Decimal(0),
    duration_ms=0,
)


def test_the_stub_hunt_passes_the_draft_rules() -> None:
    """So a stub draft can be reviewed and published end to end."""
    request = DraftRequest.model_validate({"area": "Chiswick", "theme": "Brewing"})
    checkpoints = stub_checkpoints()
    candidates = [candidate(c.place) for c in checkpoints]

    assert check_draft(checkpoints, candidates, request, stub_area(request)) == []
