"""Pure maths for the referee evals: grading, confusion matrices, threshold sweeps, stats."""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Literal

from game_server.checks.visual import classify
from game_server.evals.manifest import Category, Expected
from game_server.referee import VisualCheckJudgement

Outcome = Literal["passed", "failed", "uncertain", "error"]
Grade = Literal["correct", "false_pass", "false_fail", "deferred", "error"]

EXPECTED_ORDER: tuple[Expected, ...] = ("pass", "fail", "unsure-ok")
OUTCOME_ORDER: tuple[Outcome, ...] = ("passed", "failed", "uncertain", "error")
# 0.50, 0.55, ... 0.95
SWEEP_THRESHOLDS: tuple[float, ...] = tuple(round(0.5 + 0.05 * step, 2) for step in range(10))


@dataclass(frozen=True)
class Observation:
    """One check's answer for one case in one run. `judged` is None on a referee error."""

    case_id: str
    category: Category
    rep: int
    check: str
    expected: Expected
    judged: VisualCheckJudgement | None


def outcome_at(observation: Observation, threshold: float) -> Outcome:
    """What production would conclude at this threshold (errors kept apart)."""
    if observation.judged is None:
        return "error"
    return classify(observation.judged, threshold)


def grade(expected: Expected, outcome: Outcome) -> Grade:
    """Compare an outcome with the label.

    - A pass the label doesn't allow is a **false pass** (the costly mistake).
    - A fail where the label is `pass` is a **false fail** (a player wrongly rejected).
    - `uncertain` where the label is definite is **deferred** to a moderator: safe, but work.
    - `unsure-ok` accepts `uncertain` and `failed`.
    - Referee errors are infrastructure, not model answers: never scored either way.
    """
    if outcome == "error":
        return "error"
    if outcome == "passed":
        return "correct" if expected == "pass" else "false_pass"
    if outcome == "failed":
        return "false_fail" if expected == "pass" else "correct"
    return "correct" if expected == "unsure-ok" else "deferred"


def confusion(
    observations: Iterable[Observation], threshold: float
) -> dict[Expected, dict[Outcome, int]]:
    """Counts of expected label (rows) versus outcome at `threshold` (columns)."""
    matrix: dict[Expected, dict[Outcome, int]] = {
        expected: dict.fromkeys(OUTCOME_ORDER, 0) for expected in EXPECTED_ORDER
    }
    for observation in observations:
        matrix[observation.expected][outcome_at(observation, threshold)] += 1
    return matrix


@dataclass(frozen=True)
class SweepRow:
    """How grades change with the threshold."""

    threshold: float
    correct: int
    false_pass: int
    false_fail: int
    deferred: int
    errors: int


def grades_at(observations: Iterable[Observation], threshold: float) -> Counter[Grade]:
    """How many observations get each grade at `threshold`."""
    return Counter(grade(o.expected, outcome_at(o, threshold)) for o in observations)


def sweep(
    observations: Sequence[Observation], thresholds: Sequence[float] = SWEEP_THRESHOLDS
) -> list[SweepRow]:
    """Grades at each threshold."""
    rows = []
    for threshold in thresholds:
        counts = grades_at(observations, threshold)
        rows.append(
            SweepRow(
                threshold,
                counts["correct"],
                counts["false_pass"],
                counts["false_fail"],
                counts["deferred"],
                counts["error"],
            )
        )
    return rows


def suggested_threshold(rows: Sequence[SweepRow]) -> float | None:
    """The lowest threshold with no false pass: the most automation that stays safe.

    None when every threshold still lets a false pass through (the model or prompt, not
    the threshold, needs work).
    """
    safe = [row.threshold for row in rows if row.false_pass == 0]
    return min(safe) if safe else None


def percentile(values: Sequence[float], percent: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


# USD per million tokens (input, output), from the published price list. Unknown models
# report no cost rather than a guess.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
}


def price_for(model: str) -> tuple[float, float] | None:
    """Prices for a model id, also matching dated snapshots (e.g. `...-20251001`)."""
    for known, prices in PRICES_PER_MTOK.items():
        if model == known or model.startswith(f"{known}-"):
            return prices
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated cost from recorded token counts; None when the price isn't known."""
    prices = price_for(model)
    if prices is None:
        return None
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
