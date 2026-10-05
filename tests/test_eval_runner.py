"""The referee eval harness end to end, with fake referees (no API key, no network)."""

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anthropic
import pytest
from images import jpeg, scene
from pytest_mock import MockerFixture

from game_server.evals import referee as runner
from game_server.evals.manifest import ManifestError, load_manifest
from game_server.evals.report import RunInfo
from game_server.referee import (
    ClaudeReferee,
    ModelReply,
    PreparedReference,
    RefereeJudgement,
    RefereeReport,
    VisualCheckJudgement,
    prepare_image,
)
from game_server.sessions import VisualChallenge

Verdict = Literal["pass", "fail", "unsure"]
LABEL_TO_VERDICT: dict[str, Verdict] = {"pass": "pass", "fail": "fail", "unsure-ok": "unsure"}
MODEL = "claude-haiku-4-5"

CASES = [
    # id, category, expected scene, expected pose
    ("right-01", "right-place-right-pose", "pass", "pass"),
    ("wrong-pose-01", "right-place-wrong-pose", "pass", "fail"),
    ("screen-01", "screen-or-print", "fail", "pass"),
    ("injection-01", "injection", "fail", "pass"),
    ("dark-01", "dark-or-blurry", "unsure-ok", "unsure-ok"),
]


@pytest.fixture
def eval_dir(tmp_path: Path) -> Path:
    """A private eval directory: generated photos plus a manifest labelling them."""
    (tmp_path / "photos").mkdir()
    cases = []
    for index, (case_id, category, scene_label, pose_label) in enumerate(CASES):
        (tmp_path / "photos" / f"{case_id}.jpg").write_bytes(jpeg(scene(index, (64, 48))))
        cases.append(
            {
                "id": case_id,
                "image": f"photos/{case_id}.jpg",
                "place": f"place-{index % 3}",
                "category": category,
                # The scene text carries the case id so fakes can answer per case.
                "scene": f"scene for {case_id}",
                "pose": "Wave",
                "expected": {"scene_matches": scene_label, "pose_correct": pose_label},
            }
        )
    (tmp_path / "cases.json").write_text(json.dumps({"cases": cases}))
    return tmp_path


def info(runs: int = 1, threshold: float = 0.8, max_references: int = 2) -> RunInfo:
    return RunInfo(
        requested_model=MODEL,
        threshold=threshold,
        runs=runs,
        started_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        prompt_digest="abc123def456",
        max_image_edge=1568,
        max_references=max_references,
        reference_max_edge=768,
    )


def ok(scene_verdict: Verdict, pose_verdict: Verdict, confidence: float = 0.95) -> RefereeReport:
    return RefereeReport(
        status="ok",
        judgement=RefereeJudgement(
            scene_matches=VisualCheckJudgement(
                reason="SCENE-REASON", verdict=scene_verdict, confidence=confidence
            ),
            pose_correct=VisualCheckJudgement(
                reason="POSE-REASON", verdict=pose_verdict, confidence=confidence
            ),
        ),
        model=f"{MODEL}-20251001",
        request_id="req",
        input_tokens=1000,
        output_tokens=100,
        latency_ms=1500,
    )


def case_id(challenge: VisualChallenge) -> str:
    return challenge.scene.removeprefix("scene for ")


@dataclass
class ScriptedReferee:
    """Answers per case id; records every call."""

    answers: dict[str, RefereeReport]
    default: RefereeReport = field(default_factory=lambda: ok("unsure", "unsure"))
    calls: list[tuple[str, int]] = field(default_factory=list)
    # The positions of the reference photos sent, by case id.
    references: dict[str, list[int]] = field(default_factory=dict)

    def judge(
        self,
        image: bytes,
        challenge: VisualChallenge,
        references: Sequence[PreparedReference] = (),
    ) -> RefereeReport:
        self.calls.append((case_id(challenge), len(image)))
        self.references[case_id(challenge)] = [reference.position for reference in references]
        return self.answers.get(case_id(challenge), self.default)


def oracle() -> ScriptedReferee:
    return ScriptedReferee(
        {
            cid: ok(LABEL_TO_VERDICT[scene_label], LABEL_TO_VERDICT[pose_label])
            for cid, _, scene_label, pose_label in CASES
        }
    )


def run(eval_dir: Path, referee: Any, **run_info: Any) -> tuple[str, bool, Path]:
    result = runner.run_eval(referee, eval_dir / "cases.json", info(**run_info))
    return result.report_path.read_text(), result.failed, result.results_path


# --- known-good and known-bad referees --------------------------------------------


def test_oracle_referee_scores_perfectly(eval_dir: Path) -> None:
    report, failed, _ = run(eval_dir, oracle())

    assert failed is False
    assert "Result: **OK**" in report
    assert report.count("**0 false pass**") == 2
    assert "0 false fail" in report
    assert "Lowest threshold with no false pass: **0.50**" in report


def test_null_referee_passes_nothing(eval_dir: Path) -> None:
    report, failed, _ = run(eval_dir, ScriptedReferee({}))  # always unsure

    assert failed is False
    assert report.count("**0 false pass**") == 2
    assert "deferred to a moderator" in report


def test_always_pass_referee_fails_the_run_on_critical_cases(eval_dir: Path) -> None:
    report, failed, _ = run(eval_dir, ScriptedReferee({}, default=ok("pass", "pass", 0.99)))

    assert failed is True
    assert "Result: **FAILED**: 2 false pass(es)" in report
    assert (
        "| screen-01 | screen-or-print | 1 | scene_matches | fail | pass | 0.99 "
        "| passed **FALSE PASS** |" in report
    )
    assert "| injection-01 | injection |" in report
    assert "No threshold in the sweep avoids every false pass" in report


def test_critical_false_pass_below_threshold_does_not_fail_the_run(eval_dir: Path) -> None:
    referee = oracle()
    referee.answers["injection-01"] = ok("pass", "pass", 0.7)  # below 0.8: uncertain

    report, failed, _ = run(eval_dir, referee)

    assert failed is False
    assert "Lowest threshold with no false pass: **0.75**" in report


# --- errors, stability, served model ----------------------------------------------


def test_referee_errors_are_counted_apart_never_graded(eval_dir: Path) -> None:
    referee = oracle()
    referee.answers["right-01"] = RefereeReport(status="error", error_code="timeout", model=MODEL)
    referee.answers["dark-01"] = RefereeReport(status="error", error_code="refusal", model=MODEL)

    report, failed, _ = run(eval_dir, referee)

    assert failed is False
    assert "2 call(s) produced no judgement: `refusal` x 1, `timeout` x 1" in report
    assert "0 false fail" in report
    assert "deferred to a moderator, 2 error." in report


def test_runs_repeat_every_case_in_rounds(eval_dir: Path) -> None:
    referee = oracle()

    run(eval_dir, referee, runs=3)

    order = [cid for cid, _ in referee.calls]
    assert order == [cid for cid, *_ in CASES] * 3
    assert all(size > 0 for _, size in referee.calls)  # the real photo bytes were sent


def test_unstable_answers_are_reported(eval_dir: Path) -> None:
    answers = iter([ok("pass", "pass"), ok("fail", "pass")])

    @dataclass
    class FlipFlop:
        def judge(
            self,
            image: bytes,
            challenge: VisualChallenge,
            references: Sequence[PreparedReference] = (),
        ) -> RefereeReport:
            if case_id(challenge) == "right-01":
                return next(answers)
            return oracle().answers[case_id(challenge)]

    report, _, _ = run(eval_dir, FlipFlop(), runs=2)

    assert "## Stability across runs" in report
    assert "1 of 10 (case, check) pairs changed outcome across 2 runs" in report
    assert "- right-01 / scene_matches: failed, passed" in report


def test_served_model_mismatch_is_flagged(eval_dir: Path) -> None:
    served_by_sonnet = replace(ok("pass", "pass"), model="claude-sonnet-5")
    referee = ScriptedReferee({}, default=served_by_sonnet)

    report, _, _ = run(eval_dir, referee)

    assert "**Warning: served by a model other than requested: ['claude-sonnet-5']**" in report


# --- outputs ------------------------------------------------------------------------


def test_writes_report_and_raw_results_under_reports(eval_dir: Path) -> None:
    report, _, results_path = run(eval_dir, oracle())

    assert (eval_dir / "reports" / f"20260929T120000Z-{MODEL}.md").is_file()
    assert results_path == eval_dir / "reports" / f"20260929T120000Z-{MODEL}.jsonl"
    rows = [json.loads(line) for line in results_path.read_text().splitlines()]
    assert [row["case"] for row in rows] == [cid for cid, *_ in CASES]
    assert rows[0]["judgement"]["scene_matches"]["reason"] == "SCENE-REASON"
    assert "SCENE-REASON" not in report  # reasons stay in the raw results, not the report


def test_report_has_every_section(eval_dir: Path) -> None:
    report, _, _ = run(eval_dir, oracle())

    for heading in (
        "# Referee eval: claude-haiku-4-5",
        "## `scene_matches`",
        "## `pose_correct`",
        "### Confusion matrix at 0.80",
        "### Threshold sweep",
        "## Screen/print and injection cases",
        "## Cost and latency",
        "## Per case",
    ):
        assert heading in report
    assert "- Tokens: 5000 in / 500 out (per photo: 1000 in / 100 out)" in report
    assert "- Estimated cost: $0.0075 ($0.0015 per photo)" in report
    assert "p50 1500 ms, p95 1500 ms" in report
    assert "Prompt digest: `abc123def456`" in report


# --- command line --------------------------------------------------------------------


def test_cli_without_a_key_exits_2(
    eval_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GAME_SERVER_ANTHROPIC_API_KEY", raising=False)

    assert runner.main(["--eval-dir", str(eval_dir)]) == 2
    assert "GAME_SERVER_ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_SERVER_ANTHROPIC_API_KEY", "sk-test")


@pytest.mark.usefixtures("with_key")
def test_cli_bad_manifest_exits_2_before_any_call(
    tmp_path: Path, mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    referee = ScriptedReferee({})
    mocker.patch.object(runner, "build_referee", return_value=referee)

    assert runner.main(["--eval-dir", str(tmp_path)]) == 2
    assert "cannot read manifest" in capsys.readouterr().err
    assert referee.calls == []


@pytest.mark.usefixtures("with_key")
def test_cli_uses_the_production_referee_with_the_chosen_model(
    eval_dir: Path, mocker: MockerFixture
) -> None:
    build = mocker.patch.object(runner, "build_referee", return_value=oracle())

    status = runner.main(["--eval-dir", str(eval_dir), "--model", "claude-sonnet-5", "--runs", "2"])

    assert status == 0
    key, model, *_ = build.call_args.args
    assert (key, model) == ("sk-test", "claude-sonnet-5")
    assert len(list((eval_dir / "reports").glob("*-claude-sonnet-5.md"))) == 1


@pytest.mark.usefixtures("with_key")
def test_cli_exits_1_on_a_critical_false_pass(eval_dir: Path, mocker: MockerFixture) -> None:
    always_pass = ScriptedReferee({}, default=ok("pass", "pass", 0.99))
    mocker.patch.object(runner, "build_referee", return_value=always_pass)

    assert runner.main(["--eval-dir", str(eval_dir)]) == 1


@pytest.mark.parametrize("args", [["--runs", "0"], ["--threshold", "1.5"]])
def test_cli_rejects_bad_arguments(eval_dir: Path, args: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        runner.main(["--eval-dir", str(eval_dir), *args])

    assert excinfo.value.code == 2


def test_example_manifest_validates_through_the_loader() -> None:
    example = Path(__file__).parent.parent / "evals" / "referee" / "cases.example.json"

    manifest, _ = load_manifest(example, check_images=False)

    assert {case.category for case in manifest.cases} >= {"screen-or-print", "injection"}


def test_set_without_critical_cases_is_not_reported_as_ok(eval_dir: Path) -> None:
    manifest = json.loads((eval_dir / "cases.json").read_text())
    manifest["cases"] = [
        case
        for case in manifest["cases"]
        if case["category"] not in {"screen-or-print", "injection"}
    ]
    (eval_dir / "cases.json").write_text(json.dumps(manifest))

    report, failed, _ = run(eval_dir, oracle())

    assert failed is False  # untested is a warning, not a failed run
    assert "Result: **NOT TESTED**: the set has no screen/print or injection cases" in report
    assert "**OK**" not in report


# --- reference photos ---------------------------------------------------------------------


@pytest.fixture
def with_references(eval_dir: Path) -> Path:
    """place-0 has three reference photos; its injection case opts out with `[]`."""
    (eval_dir / "reference").mkdir()
    for index in range(3):
        photo = jpeg(scene(60 + index, (1200, 900)))
        (eval_dir / "reference" / f"place-0-{index}.jpg").write_bytes(photo)
    manifest = json.loads((eval_dir / "cases.json").read_text())
    photos = [f"reference/place-0-{index}.jpg" for index in range(3)]
    manifest["places"] = {"place-0": {"reference_photos": photos}}
    for case in manifest["cases"]:
        if case["id"] == "injection-01":
            assert case["place"] == "place-0"
            case["reference_photos"] = []
    (eval_dir / "cases.json").write_text(json.dumps(manifest))
    return eval_dir


def test_references_are_capped_and_sent_per_case(with_references: Path) -> None:
    referee = oracle()

    run(with_references, referee, max_references=2)

    assert referee.references == {
        "right-01": [0, 1],  # place-0's first two: the third is past the cap
        "wrong-pose-01": [],  # place-1 has none
        "screen-01": [],
        "injection-01": [],  # its own [] replaces its place's
        "dark-01": [],
    }


def test_the_harness_sends_references_as_production_does(
    with_references: Path, mocker: MockerFixture
) -> None:
    reply = ModelReply("end_turn", ok_output(), MODEL, "req", 2500, 100)
    wrapper = mocker.patch("game_server.referee._create_structured_message", return_value=reply)
    sdk = anthropic.Anthropic(api_key="test-key-not-used")
    referee = ClaudeReferee(sdk, MODEL, max_image_edge=1568)

    run(with_references, referee, max_references=2)

    contents = {
        case_id_of(call.kwargs["content"]): call.kwargs["content"]
        for call in wrapper.call_args_list
    }
    sent = contents["right-01"]
    assert [block["text"] for block in sent[0:6:2]] == [
        "Reference photo 1 of 2: the checkpoint, photographed by the organiser",
        "Reference photo 2 of 2: the checkpoint, photographed by the organiser",
        "The player's photo",
    ]
    for block, index in zip(sent[1:4:2], range(2), strict=True):
        reference = (with_references / "reference" / f"place-0-{index}.jpg").read_bytes()
        assert base64.b64decode(block["source"]["data"]) == prepare_image(reference, 768).jpeg
    assert len(contents["injection-01"]) == 2  # the player's photo and the text: as without


def ok_output() -> str:
    return json.dumps(
        {
            check: {"reason": "x", "verdict": "pass", "confidence": 0.9}
            for check in ("scene_matches", "pose_correct")
        }
    )


def case_id_of(content: list[Any]) -> str:
    """The case a request was for, from its `<scene>` (which carries the case id)."""
    text: str = content[-1]["text"]
    return text.split("\n")[1].removeprefix("scene for ")


def test_a_bad_reference_photo_stops_the_run_before_any_call(with_references: Path) -> None:
    (with_references / "reference" / "place-0-1.jpg").write_bytes(b"\xff\xd8\xffjunk")
    referee = oracle()

    with pytest.raises(ManifestError, match=r"^case right-01: reference-photos\[1\]: doesn't"):
        run(with_references, referee, max_references=2)

    assert referee.calls == []


def test_report_and_results_show_the_references_sent(with_references: Path) -> None:
    report, _, results_path = run(with_references, oracle(), max_references=2)

    assert "- Reference photos: up to 2 per photo, long edge 768 px; sent with 1 of 5 cases" in (
        report
    )
    assert "- Reference photos sent: 2 (0.4 per photo)" in report
    assert "| right-01 | place-0 | right-place-right-pose | 2 | 1 |" in report
    rows = {row["case"]: row for row in map(json.loads, results_path.read_text().splitlines())}
    assert (rows["right-01"]["references"], rows["injection-01"]["references"]) == (2, 0)


def test_max_references_zero_sends_none_and_says_so(with_references: Path) -> None:
    referee = oracle()

    report, _, _ = run(with_references, referee, max_references=0)

    assert all(positions == [] for positions in referee.references.values())
    assert "- Reference photos: off (`GAME_SERVER_REFEREE_MAX_REFERENCES=0`)" in report


@pytest.mark.usefixtures("with_key")
def test_cli_takes_the_reference_settings(
    with_references: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GAME_SERVER_REFEREE_MAX_REFERENCES", "1")
    monkeypatch.setenv("GAME_SERVER_REFEREE_REFERENCE_MAX_EDGE", "320")
    referee = oracle()
    mocker.patch.object(runner, "build_referee", return_value=referee)

    assert runner.main(["--eval-dir", str(with_references)]) == 0

    assert referee.references["right-01"] == [0]
    [report] = (with_references / "reports").glob("*.md")
    assert "up to 1 per photo, long edge 320 px" in report.read_text()
