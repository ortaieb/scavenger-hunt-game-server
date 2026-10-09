"""The designer's command line, with the agent itself replaced: no API calls."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from game_server.config import Settings
from game_server.designer.__main__ import main
from game_server.designer.agent import AgentUnavailableError, DesignResult, Route, RunStats
from game_server.drafts import (
    BoundingBox,
    DraftArea,
    DraftChallenge,
    DraftCheckpoint,
    DraftPlace,
    DraftRequest,
    Problem,
)
from game_server.models import Location

ARGS = ["--area", "Chiswick, London", "--theme", "Painters and brewers"]
STATS = RunStats(
    model="claude-test-model",
    turns=9,
    cost_usd=Decimal("0.3125"),
    duration_ms=95_000,
    subtype="success",
)
CHECKPOINTS = tuple(
    DraftCheckpoint(
        position=position,
        place=DraftPlace(
            osm=f"node/{position}",
            name=f"Place {position}",
            kind="historic=memorial",
            location=Location(lat=51.49 + position / 1000, long=-0.26),
        ),
        clue=f"Clue {position}",
        challenge=DraftChallenge(scene=f"Scene {position}", pose=f"Pose {position}"),
        proximity=30,
        rationale=f"Why {position}",
    )
    for position in (1, 2, 3)
)
ACCEPTED = DesignResult(
    area=DraftArea(
        name="Chiswick, London, England",
        bbox=BoundingBox(south=51.48, west=-0.275, north=51.5, east=-0.245),
        clipped=True,
    ),
    checkpoints=CHECKPOINTS,
    route=Route(legs_m=(111, 111, 222), loop_m=444),
    stats=STATS,
)
FAILED = DesignResult(
    area=None, checkpoints=(), route=None, stats=STATS, error_code="no_valid_draft"
)
TOO_CLOSE = Problem(
    code="too_close",
    position=2,
    message="Checkpoints 1 and 2 are 111 m apart; keep them at least 150 m apart",
)
PARTIAL = DesignResult(
    area=ACCEPTED.area,
    checkpoints=CHECKPOINTS,
    route=ACCEPTED.route,
    stats=STATS,
    error_code="max_turns",
    problems=(TOO_CLOSE,),
)


@pytest.fixture(autouse=True)
def quiet_logging(mocker: MockerFixture) -> MagicMock:
    """The CLI sends the app's logs to stderr; leave the test run's logging alone."""
    return mocker.patch("game_server.designer.__main__.configure_logging")


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_ANTHROPIC_API_KEY", "sk-ant-test-key")


def designer(mocker: MockerFixture, result: DesignResult) -> AsyncMock:
    return mocker.patch("game_server.designer.__main__.design_hunt", AsyncMock(return_value=result))


@pytest.mark.usefixtures("key")
def test_json_prints_the_draft_in_the_apis_checkpoint_shape(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    designer(mocker, ACCEPTED)

    status = main([*ARGS, "--json"])

    printed = json.loads(capsys.readouterr().out)
    assert status == 0
    assert printed["checkpoints"][0] == {
        "position": 1,
        "place": {
            "osm": "node/1",
            "name": "Place 1",
            "kind": "historic=memorial",
            "location": {"lat": 51.491, "long": -0.26},
        },
        "clue": "Clue 1",
        "challenge": {"scene": "Scene 1", "pose": "Pose 1"},
        "proximity": 30,
        "rationale": "Why 1",
        "review": "pending",
        "edited": False,
        "original": None,
    }
    assert [DraftCheckpoint.model_validate(c) for c in printed["checkpoints"]] == list(CHECKPOINTS)


@pytest.mark.usefixtures("key")
def test_json_prints_the_area_route_and_run(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    designer(mocker, ACCEPTED)

    main([*ARGS, "--json"])

    printed = json.loads(capsys.readouterr().out)
    assert printed["area"]["clipped"] is True
    assert printed["route"] == {"legs-m": [111, 111, 222], "loop-m": 444}
    assert printed["run"] == {
        "model": "claude-test-model",
        "turns": 9,
        "cost-usd": 0.3125,
        "duration-ms": 95_000,
        "subtype": "success",
        "error": None,
    }


@pytest.mark.usefixtures("key")
def test_json_prints_a_failed_runs_error(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    designer(mocker, FAILED)

    status = main([*ARGS, "--json"])

    printed = json.loads(capsys.readouterr().out)
    assert status == 1
    assert (printed["area"], printed["checkpoints"], printed["route"]) == (None, [], None)
    assert printed["problems"] == []
    assert printed["run"]["error"] == {"code": "no_valid_draft"}


@pytest.mark.usefixtures("key")
def test_json_prints_a_failed_runs_last_draft_and_its_problems(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    designer(mocker, PARTIAL)

    status = main([*ARGS, "--json"])

    printed = json.loads(capsys.readouterr().out)
    assert status == 1
    assert len(printed["checkpoints"]) == 3
    assert printed["problems"] == [
        {"code": "too_close", "position": 2, "message": TOO_CLOSE.message}
    ]
    assert printed["run"]["error"] == {"code": "max_turns"}


@pytest.mark.usefixtures("key")
def test_prints_the_draft_and_the_runs_stats(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    designer(mocker, ACCEPTED)

    status = main(ARGS)

    out = capsys.readouterr().out
    assert status == 0
    assert "Area: Chiswick, London, England (clipped)" in out
    assert "1. Place 1 (historic=memorial, node/1)" in out
    for text in ("Clue: Clue 3", "Scene: Scene 3", "Pose: Pose 3", "Why: Why 3"):
        assert text in out
    assert "Route: 111 m, 111 m, 222 m; 0.4 km round the loop" in out
    assert "Run: claude-test-model, 9 turns, $0.3125, 95.0 s, success" in out


@pytest.mark.usefixtures("key")
def test_prints_the_error_and_exits_1_when_the_run_fails(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    designer(mocker, FAILED)

    status = main(ARGS)

    out = capsys.readouterr().out
    assert status == 1
    assert "Error: no_valid_draft" in out
    assert "Run: claude-test-model, 9 turns" in out


@pytest.mark.usefixtures("key")
def test_prints_a_failed_runs_last_draft_and_its_problems(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    designer(mocker, PARTIAL)

    status = main(ARGS)

    out = capsys.readouterr().out
    assert status == 1
    assert "3. Place 3 (historic=memorial, node/3)" in out
    assert f"Problem: {TOO_CLOSE.message} (too_close)\nError: max_turns" in out


@pytest.mark.usefixtures("key")
def test_runs_the_request_as_given_with_the_settings(mocker: MockerFixture) -> None:
    design = designer(mocker, ACCEPTED)

    main([*ARGS, "--checkpoints", "5", "--max-walk-km", "4.5"])

    assert design.await_args is not None
    [request] = design.await_args.args
    assert request == DraftRequest.model_validate(
        {
            "area": "Chiswick, London",
            "theme": "Painters and brewers",
            "checkpoints": 5,
            "max-walk-km": 4.5,
        }
    )
    assert isinstance(design.await_args.kwargs["settings"], Settings)


@pytest.mark.usefixtures("key")
def test_reports_progress_on_stderr(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    design = designer(mocker, ACCEPTED)
    main(ARGS)
    capsys.readouterr()
    assert design.await_args is not None

    asyncio.run(design.await_args.kwargs["on_progress"]("find_places", "58 candidate places"))

    assert capsys.readouterr().err == "[find_places] 58 candidate places\n"


def test_without_a_key_exits_2_and_never_runs(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    design = designer(mocker, ACCEPTED)

    status = main(ARGS)

    assert status == 2
    assert "GAME_SERVER_ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    design.assert_not_awaited()


@pytest.mark.usefixtures("key")
def test_an_invalid_request_exits_2_without_echoing_it(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    design = designer(mocker, ACCEPTED)

    status = main(["--area", "Chiswick, London", "--theme", "Qz", "--checkpoints", "99"])

    err = capsys.readouterr().err
    assert status == 2
    assert "--theme: String should have at least 3 characters" in err
    assert "--checkpoints: Input should be less than or equal to 8" in err
    assert "Qz" not in err
    design.assert_not_awaited()


def test_area_and_theme_are_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["--area", "Chiswick, London"])

    assert exited.value.code == 2
    assert "--area and --theme are required" in capsys.readouterr().err


def test_self_check_prints_the_bundled_binarys_version(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    version = mocker.patch(
        "game_server.designer.__main__.cli_version", return_value="2.1.294 (Claude Code)"
    )

    status = main(["--self-check"])

    assert status == 0
    assert capsys.readouterr().out == "2.1.294 (Claude Code)\n"
    [binary] = version.call_args.args
    assert isinstance(binary, Path)
    assert binary.name == "claude"


def test_a_failed_self_check_exits_2(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch(
        "game_server.designer.__main__.cli_version",
        side_effect=AgentUnavailableError("claude didn't start"),
    )

    status = main(["--self-check"])

    assert status == 2
    assert "Self-check failed: claude didn't start" in capsys.readouterr().err
