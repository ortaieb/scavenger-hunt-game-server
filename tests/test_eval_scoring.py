from typing import Literal

import pytest

from game_server.evals.manifest import Category, Expected
from game_server.evals.scoring import (
    SWEEP_THRESHOLDS,
    Observation,
    Outcome,
    SweepRow,
    confusion,
    grade,
    outcome_at,
    percentile,
    suggested_threshold,
    sweep,
)
from game_server.referee import VisualCheckJudgement

Verdict = Literal["pass", "fail", "unsure"]


def obs(
    expected: Expected,
    verdict: Verdict | None,
    confidence: float = 0.9,
    category: Category = "right-place-right-pose",
) -> Observation:
    judged = (
        VisualCheckJudgement(reason="r", verdict=verdict, confidence=confidence)
        if verdict
        else None
    )
    return Observation("case", category, 1, "scene_matches", expected, judged)


@pytest.mark.parametrize(
    ("expected", "outcome", "graded"),
    [
        ("pass", "passed", "correct"),
        ("pass", "failed", "false_fail"),
        ("pass", "uncertain", "deferred"),
        ("fail", "passed", "false_pass"),
        ("fail", "failed", "correct"),
        ("fail", "uncertain", "deferred"),
        ("unsure-ok", "passed", "false_pass"),
        ("unsure-ok", "failed", "correct"),
        ("unsure-ok", "uncertain", "correct"),
        ("pass", "error", "error"),
        ("fail", "error", "error"),
    ],
)
def test_grade(expected: Expected, outcome: Outcome, graded: str) -> None:
    assert grade(expected, outcome) == graded


@pytest.mark.parametrize(
    ("verdict", "confidence", "threshold", "outcome"),
    [
        ("pass", 0.8, 0.8, "passed"),
        ("pass", 0.79, 0.8, "uncertain"),
        ("fail", 0.9, 0.8, "failed"),
        ("unsure", 0.99, 0.5, "uncertain"),
        (None, 0.0, 0.8, "error"),
    ],
)
def test_outcome_at_mirrors_production(
    verdict: Verdict | None, confidence: float, threshold: float, outcome: str
) -> None:
    assert outcome_at(obs("pass", verdict, confidence), threshold) == outcome


def test_confusion_matrix_counts() -> None:
    observations = [
        obs("pass", "pass", 0.9),
        obs("pass", "pass", 0.6),  # below 0.8: uncertain
        obs("fail", "pass", 0.95),  # false pass
        obs("fail", "fail", 0.9),
        obs("unsure-ok", "unsure", 0.9),
        obs("pass", None),  # referee error
    ]

    matrix = confusion(observations, 0.8)

    assert matrix["pass"] == {"passed": 1, "failed": 0, "uncertain": 1, "error": 1}
    assert matrix["fail"] == {"passed": 1, "failed": 1, "uncertain": 0, "error": 0}
    assert matrix["unsure-ok"] == {"passed": 0, "failed": 0, "uncertain": 1, "error": 0}
    assert sum(sum(row.values()) for row in matrix.values()) == len(observations)


def test_sweep_thresholds_are_050_to_095() -> None:
    assert SWEEP_THRESHOLDS == (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)


def test_sweep_counts_change_with_threshold() -> None:
    observations = [
        obs("fail", "pass", 0.7),  # false pass until the threshold exceeds 0.7
        obs("pass", "pass", 0.85),  # correct until the threshold exceeds 0.85
        obs("pass", "fail", 0.6),  # false fail until the threshold exceeds 0.6
        obs("pass", None),
    ]

    rows = {row.threshold: row for row in sweep(observations)}

    assert rows[0.5] == SweepRow(0.5, correct=1, false_pass=1, false_fail=1, deferred=0, errors=1)
    assert rows[0.7] == SweepRow(0.7, correct=1, false_pass=1, false_fail=0, deferred=1, errors=1)
    assert rows[0.75] == SweepRow(0.75, correct=1, false_pass=0, false_fail=0, deferred=2, errors=1)
    assert rows[0.9] == SweepRow(0.9, correct=0, false_pass=0, false_fail=0, deferred=3, errors=1)


def test_suggested_threshold_is_lowest_without_false_pass() -> None:
    rows = sweep([obs("fail", "pass", 0.7), obs("pass", "pass", 0.95)])

    assert suggested_threshold(rows) == 0.75


def test_no_suggestion_when_every_threshold_lets_a_false_pass_through() -> None:
    rows = sweep([obs("fail", "pass", 0.99)])

    assert suggested_threshold(rows) is None


@pytest.mark.parametrize(
    ("values", "percent", "expected"),
    [
        ([], 50, None),
        ([7.0], 95, 7.0),
        ([1.0, 2.0, 3.0, 4.0], 50, 2.0),
        ([float(v) for v in range(1, 101)], 95, 95.0),
        ([5.0, 1.0, 3.0], 50, 3.0),  # unsorted input
    ],
)
def test_percentile_nearest_rank(
    values: list[float], percent: float, expected: float | None
) -> None:
    assert percentile(values, percent) == expected
