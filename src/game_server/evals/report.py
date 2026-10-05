"""Render a referee eval run as a Markdown report."""

from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from game_server.evals.manifest import CRITICAL_CATEGORIES, EvalCase
from game_server.evals.privacy import person_words
from game_server.evals.scoring import (
    EXPECTED_ORDER,
    OUTCOME_ORDER,
    Observation,
    confusion,
    grade,
    grades_at,
    outcome_at,
    percentile,
    suggested_threshold,
    sweep,
)
from game_server.pricing import cost_usd
from game_server.referee import RefereeJudgement, RefereeReport

CHECKS = tuple(RefereeJudgement.model_fields)


@dataclass(frozen=True)
class CaseRun:
    """One call to the referee for one case."""

    case: EvalCase
    rep: int
    report: RefereeReport
    # How many reference photos were sent with the photo.
    references: int = 0


@dataclass(frozen=True)
class RunInfo:
    """What was run, for the report header."""

    requested_model: str
    threshold: float
    runs: int
    started_at: datetime
    prompt_digest: str
    max_image_edge: int
    # GAME_SERVER_REFEREE_MAX_REFERENCES and GAME_SERVER_REFEREE_REFERENCE_MAX_EDGE.
    max_references: int
    reference_max_edge: int


def observations(case_runs: Sequence[CaseRun]) -> list[Observation]:
    """One observation per check per case run."""
    result = []
    for run in case_runs:
        judgement = run.report.judgement if run.report.status == "ok" else None
        for check in CHECKS:
            result.append(
                Observation(
                    case_id=run.case.id,
                    category=run.case.category,
                    rep=run.rep,
                    check=check,
                    expected=getattr(run.case.expected, check),
                    judged=getattr(judgement, check) if judgement else None,
                )
            )
    return result


def critical_false_passes(obs: Sequence[Observation], threshold: float) -> list[Observation]:
    """False passes on screen/print and injection cases: each one fails the run."""
    return [
        o
        for o in obs
        if o.category in CRITICAL_CATEGORIES
        and grade(o.expected, outcome_at(o, threshold)) == "false_pass"
    ]


@dataclass(frozen=True)
class Leak:
    """A reason that describes the person: where it is, and the listed words it uses."""

    model: str
    case_id: str
    rep: int
    check: str
    words: tuple[str, ...]


def _reasons(case_runs: Sequence[CaseRun]) -> Iterator[tuple[str, CaseRun, str, str]]:
    """Every reason the referee gave: the model that served it, its run, check and text."""
    for run in case_runs:
        judgement = run.report.judgement
        if run.report.status != "ok" or judgement is None:
            continue
        for check in CHECKS:
            reason: str = getattr(judgement, check).reason
            yield run.report.model or "n/a", run, check, reason


def privacy_leaks(case_runs: Sequence[CaseRun]) -> list[Leak]:
    """Every reason that uses a word describing the person. Any one fails privacy."""
    return [
        Leak(model, run.case.id, run.rep, check, words)
        for model, run, check, reason in _reasons(case_runs)
        if (words := person_words(reason))
    ]


def render(case_runs: Sequence[CaseRun], info: RunInfo) -> tuple[str, bool]:
    """The Markdown report, and whether the run failed (a critical false pass).

    A privacy leak is reported, but doesn't fail the run: see `privacy_leaks`.
    """
    obs = observations(case_runs)
    failures = critical_false_passes(obs, info.threshold)
    leaks = privacy_leaks(case_runs)
    sections = [
        _header(case_runs, info, failures, leaks),
        _errors(case_runs),
        *(_check_section(check, obs, info.threshold) for check in CHECKS),
        _critical(obs, info.threshold),
        _privacy(case_runs, leaks),
        _stability(obs, info),
        _cost_and_latency(case_runs),
        _per_case(case_runs, obs, info.threshold),
    ]
    return "\n\n".join(section for section in sections if section) + "\n", bool(failures)


def _header(
    case_runs: Sequence[CaseRun],
    info: RunInfo,
    failures: list[Observation],
    leaks: list[Leak],
) -> str:
    cases = {run.case.id for run in case_runs}
    served = sorted({run.report.model for run in case_runs if run.report.model})
    mismatched = [m for m in served if not m.startswith(info.requested_model)]
    lines = [
        f"# Referee eval: {info.requested_model}",
        "",
        f"- Run: {info.started_at.isoformat(timespec='seconds')}",
        f"- Cases: {len(cases)}, runs per case: {info.runs}, calls: {len(case_runs)}",
        f"- Threshold (`GAME_SERVER_REFEREE_MIN_CONFIDENCE`): {info.threshold:.2f}",
        f"- Prompt digest: `{info.prompt_digest}`, max image edge: {info.max_image_edge} px",
        _references_line(case_runs, info),
        f"- Served by: {', '.join(served) or 'n/a'}",
    ]
    if mismatched:
        lines.append(f"- **Warning: served by a model other than requested: {mismatched}**")
    tested = any(run.case.category in CRITICAL_CATEGORIES for run in case_runs)
    if failures:
        verdict = f"**FAILED**: {len(failures)} false pass(es) on screen/print or injection cases."
    elif not tested:
        # Nothing to fail is not the same as passing: don't report OK for an untested risk.
        verdict = (
            "**NOT TESTED**: the set has no screen/print or injection cases, so this run "
            "says nothing about them. Add some before trusting a model or threshold."
        )
    else:
        verdict = "**OK**: no false pass on screen/print or injection cases."
    lines += ["", f"Result: {verdict}", "", _privacy_verdict(case_runs, leaks)]
    return "\n".join(lines)


def _privacy_verdict(case_runs: Sequence[CaseRun], leaks: list[Leak]) -> str:
    reasons = sum(1 for _ in _reasons(case_runs))
    if leaks:
        return f"Privacy: **FAIL**: {len(leaks)} of {reasons} reasons describe the person."
    if not reasons:
        return "Privacy: **NOT TESTED**: no call returned reasons."
    return f"Privacy: **OK**: none of {reasons} reasons describes the person."


def _references_line(case_runs: Sequence[CaseRun], info: RunInfo) -> str:
    if info.max_references == 0:
        return "- Reference photos: off (`GAME_SERVER_REFEREE_MAX_REFERENCES=0`)"
    cases = {run.case.id for run in case_runs}
    with_references = {run.case.id for run in case_runs if run.references}
    return (
        f"- Reference photos: up to {info.max_references} per photo, long edge "
        f"{info.reference_max_edge} px; sent with {len(with_references)} of {len(cases)} cases"
    )


def _errors(case_runs: Sequence[CaseRun]) -> str:
    codes = Counter(run.report.error_code for run in case_runs if run.report.status != "ok")
    if not codes:
        return ""
    listed = ", ".join(f"`{code}` x {count}" for code, count in sorted(codes.items(), key=str))
    return (
        "## Referee errors\n\n"
        f"{sum(codes.values())} call(s) produced no judgement: {listed}. They count as "
        "`error` below and are never scored as a pass or a fail (`refusal` included)."
    )


def _check_section(check: str, obs: Sequence[Observation], threshold: float) -> str:
    mine = [o for o in obs if o.check == check]
    rows = sweep(mine)
    lines = [f"## `{check}`", "", f"### Confusion matrix at {threshold:.2f}", ""]
    lines.append("| expected \\ outcome | " + " | ".join(OUTCOME_ORDER) + " |")
    lines.append("|---|" + "---|" * len(OUTCOME_ORDER))
    counts = confusion(mine, threshold)
    for expected in EXPECTED_ORDER:
        cells = " | ".join(str(counts[expected][outcome]) for outcome in OUTCOME_ORDER)
        lines.append(f"| {expected} | {cells} |")
    graded = grades_at(mine, threshold)
    lines += [
        "",
        f"At {threshold:.2f}: {graded['correct']} correct, **{graded['false_pass']} false "
        f"pass**, {graded['false_fail']} false fail, {graded['deferred']} deferred to a "
        f"moderator, {graded['error']} error.",
        "",
        "### Threshold sweep",
        "",
        "| threshold | correct | false pass | false fail | deferred | error |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.threshold:.2f} | {row.correct} | {row.false_pass} | {row.false_fail} "
            f"| {row.deferred} | {row.errors} |"
        )
    suggestion = suggested_threshold(rows)
    lines += [
        "",
        f"Lowest threshold with no false pass: **{suggestion:.2f}**"
        if suggestion is not None
        else "No threshold in the sweep avoids every false pass: the model or prompt needs work.",
    ]
    return "\n".join(lines)


def _critical(obs: Sequence[Observation], threshold: float) -> str:
    mine = [o for o in obs if o.category in CRITICAL_CATEGORIES]
    if not mine:
        return "## Screen/print and injection cases\n\nNone in this set: add some."
    lines = [
        "## Screen/print and injection cases",
        "",
        "| case | category | run | check | expected | verdict | confidence | outcome |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for o in sorted(mine, key=lambda o: (o.case_id, o.rep, o.check)):
        outcome = outcome_at(o, threshold)
        flag = " **FALSE PASS**" if grade(o.expected, outcome) == "false_pass" else ""
        verdict = o.judged.verdict if o.judged else "-"
        confidence = f"{o.judged.confidence:.2f}" if o.judged else "-"
        lines.append(
            f"| {o.case_id} | {o.category} | {o.rep} | {o.check} | {o.expected} "
            f"| {verdict} | {confidence} | {outcome}{flag} |"
        )
    return "\n".join(lines)


def _privacy(case_runs: Sequence[CaseRun], leaks: list[Leak]) -> str:
    reasons = Counter(model for model, *_ in _reasons(case_runs))
    if not reasons:
        return "## Privacy\n\nNo call returned reasons: nothing to check."
    lines = [
        "## Privacy",
        "",
        "Reasons that use a word describing the person (`game_server.evals.privacy`: whole "
        "words, any case). The prompt says never to; production doesn't filter them.",
        "",
        "| model | leaks | reasons | cases |",
        "|---|---|---|---|",
    ]
    for model in sorted(reasons):
        mine = [leak for leak in leaks if leak.model == model]
        cases = ", ".join(sorted({leak.case_id for leak in mine})) or "-"
        lines.append(f"| {model} | {len(mine)} | {reasons[model]} | {cases} |")
    if leaks:
        lines.append("")
    lines += [
        f"- {leak.case_id} / run {leak.rep} / {leak.check}: {', '.join(leak.words)}"
        for leak in sorted(leaks, key=lambda leak: (leak.case_id, leak.rep, leak.check))
    ]
    return "\n".join(lines)


def _stability(obs: Sequence[Observation], info: RunInfo) -> str:
    if info.runs < 2:
        return ""
    outcomes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for o in obs:
        outcomes[(o.case_id, o.check)].add(outcome_at(o, info.threshold))
    unstable = sorted(key for key, seen in outcomes.items() if len(seen) > 1)
    lines = [
        "## Stability across runs",
        "",
        f"{len(unstable)} of {len(outcomes)} (case, check) pairs changed outcome across "
        f"{info.runs} runs at {info.threshold:.2f}.",
    ]
    lines += [
        f"- {case_id} / {check}: {', '.join(sorted(outcomes[(case_id, check)]))}"
        for case_id, check in unstable
    ]
    return "\n".join(lines)


def _cost_and_latency(case_runs: Sequence[CaseRun]) -> str:
    input_tokens = sum(run.report.input_tokens or 0 for run in case_runs)
    output_tokens = sum(run.report.output_tokens or 0 for run in case_runs)
    costs = [
        cost_usd(run.report.model, run.report.input_tokens or 0, run.report.output_tokens or 0)
        for run in case_runs
        if run.report.model
    ]
    known = [c for c in costs if c is not None]
    calls = max(len(case_runs), 1)
    total = (
        f"${sum(known):.4f} (${sum(known) / calls:.4f} per photo)"
        if known and len(known) == len(costs)
        else "n/a (unknown price)"
    )
    latencies = [float(run.report.latency_ms) for run in case_runs if run.report.latency_ms]
    p50, p95 = percentile(latencies, 50), percentile(latencies, 95)
    references = sum(run.references for run in case_runs)
    return "\n".join(
        [
            "## Cost and latency",
            "",
            f"- Tokens: {input_tokens} in / {output_tokens} out "
            f"(per photo: {input_tokens // calls} in / {output_tokens // calls} out)",
            f"- Reference photos sent: {references} ({references / calls:.1f} per photo)",
            f"- Estimated cost: {total}, from recorded tokens and list prices",
            f"- Latency: p50 {p50:.0f} ms, p95 {p95:.0f} ms (includes retries)"
            if p50 is not None and p95 is not None
            else "- Latency: n/a",
        ]
    )


def _per_case(case_runs: Sequence[CaseRun], obs: Sequence[Observation], threshold: float) -> str:
    by_run = {(o.case_id, o.rep, o.check): o for o in obs}
    lines = [
        "## Per case",
        "",
        "| case | place | category | refs | run | scene_matches | pose_correct "
        "| tokens in/out | ms |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for run in case_runs:
        cells = []
        for check in CHECKS:
            o = by_run[(run.case.id, run.rep, check)]
            outcome = outcome_at(o, threshold)
            cells.append(f"{o.expected} → {outcome} ({grade(o.expected, outcome)})")
        report = run.report
        lines.append(
            f"| {run.case.id} | {run.case.place} | {run.case.category} | {run.references} "
            f"| {run.rep} | {cells[0]} | {cells[1]} "
            f"| {report.input_tokens}/{report.output_tokens} | {report.latency_ms} |"
        )
    return "\n".join(lines)
