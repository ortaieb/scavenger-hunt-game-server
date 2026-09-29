"""Run the referee eval: `uv run python -m game_server.evals.referee --eval-dir DIR`.

Calls the real referee (same prompt, image preparation and settings as production) on
every labelled test photo, then writes a Markdown report and the raw results to
`DIR/reports/`. Needs `GAME_SERVER_ANTHROPIC_API_KEY`; costs money; never run in CI.

Exit status: 0 when the run is fine, 1 when a screen/print or injection case got a false
pass, 2 for a setup problem (no key, bad manifest, missing photos).
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from game_server.config import Settings
from game_server.evals.manifest import (
    Manifest,
    ManifestError,
    coverage_warnings,
    load_manifest,
)
from game_server.evals.report import CaseRun, RunInfo, render
from game_server.referee import Referee, build_referee, system_prompt


@dataclass(frozen=True)
class EvalResult:
    """Where the run's outputs went, and whether it failed."""

    report_path: Path
    results_path: Path
    failed: bool


def run_cases(
    referee: Referee,
    manifest: Manifest,
    base: Path,
    runs: int,
    progress: Callable[[str], None] = lambda _: None,
) -> list[CaseRun]:
    """Judge every case `runs` times, in rounds, so repeats aren't back to back."""
    case_runs = []
    for rep in range(1, runs + 1):
        for index, case in enumerate(manifest.cases, start=1):
            report = referee.judge((base / case.image).read_bytes(), case.challenge)
            progress(
                f"run {rep}/{runs} case {index}/{len(manifest.cases)} {case.id}: {report.status}"
            )
            case_runs.append(CaseRun(case, rep, report))
    return case_runs


def write_results(case_runs: Sequence[CaseRun], path: Path) -> None:
    """Raw results, one JSON line per call, including the model's reasons for tracing.

    Kept beside the report in the private eval directory, never in the repo.
    """
    with path.open("w", encoding="utf-8") as file:
        for run in case_runs:
            report = run.report
            record = {
                "case": run.case.id,
                "rep": run.rep,
                "status": report.status,
                "error_code": report.error_code,
                "model": report.model,
                "request_id": report.request_id,
                "input_tokens": report.input_tokens,
                "output_tokens": report.output_tokens,
                "latency_ms": report.latency_ms,
                "judgement": report.judgement.model_dump() if report.judgement else None,
            }
            file.write(json.dumps(record) + "\n")


def run_eval(
    referee: Referee,
    manifest_path: Path,
    info: RunInfo,
    progress: Callable[[str], None] = lambda _: None,
) -> EvalResult:
    """Run the eval set and write the report and raw results next to the manifest."""
    manifest, base = load_manifest(manifest_path)
    for warning in coverage_warnings(manifest):
        progress(f"warning: {warning}")
    case_runs = run_cases(referee, manifest, base, info.runs, progress)
    reports = base / "reports"
    reports.mkdir(exist_ok=True)
    stem = f"{info.started_at.strftime('%Y%m%dT%H%M%SZ')}-{info.requested_model}"
    markdown, failed = render(case_runs, info)
    report_path = reports / f"{stem}.md"
    report_path.write_text(markdown, encoding="utf-8")
    results_path = reports / f"{stem}.jsonl"
    write_results(case_runs, results_path)
    return EvalResult(report_path, results_path, failed)


def _parse_args(argv: Sequence[str] | None, settings: Settings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--eval-dir", type=Path, required=True, help="Holds cases.json and photos")
    parser.add_argument("--manifest", default="cases.json", help="Manifest file in --eval-dir")
    parser.add_argument("--model", default=settings.referee_model)
    parser.add_argument("--runs", type=int, default=1, help="Repeats per case (>= 1)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=settings.referee_min_confidence,
        help="Confidence threshold for the confusion matrices (the sweep covers 0.50-0.95)",
    )
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if not 0 <= args.threshold <= 1:
        parser.error("--threshold must be between 0 and 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the exit status."""
    settings = Settings()
    args = _parse_args(argv, settings)
    key = settings.anthropic_api_key
    if key is None or not key.get_secret_value():
        print(
            "GAME_SERVER_ANTHROPIC_API_KEY is not set: the eval calls the real API.",
            file=sys.stderr,
        )
        return 2
    referee = build_referee(
        key.get_secret_value(),
        args.model,
        settings.referee_timeout_seconds,
        settings.referee_max_retries,
        settings.referee_max_image_edge,
    )
    info = RunInfo(
        requested_model=args.model,
        threshold=args.threshold,
        runs=args.runs,
        started_at=datetime.now(UTC),
        prompt_digest=hashlib.sha256(system_prompt().encode()).hexdigest()[:12],
        max_image_edge=settings.referee_max_image_edge,
    )

    def progress(message: str) -> None:
        print(message, file=sys.stderr)

    try:
        result = run_eval(referee, args.eval_dir / args.manifest, info, progress)
    except ManifestError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"report: {result.report_path}\nresults: {result.results_path}")
    if result.failed:
        print("FAILED: a screen/print or injection case got a false pass", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
