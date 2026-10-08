"""Design a hunt from the command line: `uv run python -m game_server.designer --area ...`.

Runs the hunt-designer agent as the API will, with the same prompt, tools, sandbox and limits,
so its prompt can be tuned without the server or the web app. It needs only
`GAME_SERVER_ANTHROPIC_API_KEY` and network access: no database and no running server. Each run
calls the real API and costs money.

Prints the draft, or the error, and the run's stats; `--json` prints them in the API's draft
shape. Progress goes to stderr. `--self-check` only starts the bundled Claude Code binary and
prints its version.

Exit status: 0 when a draft is accepted, 1 when the run ends without one, 2 for a setup
problem (no key, an invalid request, or a failed self-check).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from game_server.config import Settings
from game_server.designer.agent import (
    AgentUnavailableError,
    DesignResult,
    api_key,
    bundled_cli,
    cli_version,
    design_hunt,
)
from game_server.drafts import DraftArea, DraftCheckpoint, DraftRequest, RunErrorCode
from game_server.logging_config import configure_logging


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, validate_by_name=True)


class RouteOut(_KebabModel):
    """Each checkpoint to the next, closing the loop back to the first, in metres."""

    legs_m: list[int]
    loop_m: int


class RunErrorOut(_KebabModel):
    code: RunErrorCode


class RunOut(_KebabModel):
    """The run and what it cost."""

    model: str
    turns: int
    cost_usd: float
    duration_ms: int
    subtype: str | None
    error: RunErrorOut | None


class DesignOut(_KebabModel):
    """The draft as `--json` prints it: the area, checkpoints and route in the API's shape."""

    area: DraftArea | None
    checkpoints: list[DraftCheckpoint]
    route: RouteOut | None
    run: RunOut


def design_out(result: DesignResult) -> DesignOut:
    """The result in the API's draft shape."""
    stats = result.stats
    route = result.route
    return DesignOut(
        area=result.area,
        checkpoints=list(result.checkpoints),
        route=RouteOut(legs_m=list(route.legs_m), loop_m=route.loop_m) if route else None,
        run=RunOut(
            model=stats.model,
            turns=stats.turns,
            cost_usd=float(stats.cost_usd),
            duration_ms=stats.duration_ms,
            subtype=stats.subtype,
            error=RunErrorOut(code=result.error_code) if result.error_code else None,
        ),
    )


def render(result: DesignResult) -> str:
    """The result for a person to read."""
    lines = []
    if result.area is not None:
        clipped = " (clipped)" if result.area.clipped else ""
        lines += [f"Area: {result.area.name}{clipped}", ""]
    for checkpoint in result.checkpoints:
        place = checkpoint.place
        lines += [
            f"{checkpoint.position}. {place.name} ({place.kind}, {place.osm})",
            f"   Clue: {checkpoint.clue}",
            f"   Scene: {checkpoint.challenge.scene}",
            f"   Pose: {checkpoint.challenge.pose}",
            f"   Proximity: {checkpoint.proximity} m",
            f"   Why: {checkpoint.rationale}",
            "",
        ]
    if result.route is not None:
        legs = ", ".join(f"{leg} m" for leg in result.route.legs_m)
        lines += [f"Route: {legs}; {result.route.loop_m / 1000:.1f} km round the loop", ""]
    if result.error_code is not None:
        lines.append(f"Error: {result.error_code}")
    stats = result.stats
    lines.append(
        f"Run: {stats.model}, {stats.turns} turns, ${stats.cost_usd:.4f}, "
        f"{stats.duration_ms / 1000:.1f} s, {stats.subtype or 'no result'}"
    )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--area", help='Where the hunt is, e.g. "Chiswick, London"')
    parser.add_argument("--theme", help="What the hunt is about")
    parser.add_argument("--checkpoints", type=int, default=3, help="How many (3-8; default 3)")
    parser.add_argument(
        "--max-walk-km", type=float, default=3, help="The longest walk (0.5-10; default 3)"
    )
    parser.add_argument("--json", action="store_true", help="Print the draft as JSON")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Start the bundled Claude Code binary, print its version and exit",
    )
    args = parser.parse_args(argv)
    if not args.self_check and (args.area is None or args.theme is None):
        parser.error("--area and --theme are required")
    return args


def _request(args: argparse.Namespace) -> DraftRequest | None:
    """The request, or None after printing what's wrong with it (never the values)."""
    try:
        return DraftRequest.model_validate(
            {
                "area": args.area,
                "theme": args.theme,
                "checkpoints": args.checkpoints,
                "max-walk-km": args.max_walk_km,
            }
        )
    except ValidationError as exc:
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            print(f"--{field}: {error['msg']}", file=sys.stderr)
        return None


def self_check() -> int:
    """Start the bundled binary and print its version."""
    try:
        print(cli_version(bundled_cli()))
    except AgentUnavailableError as exc:
        print(f"Self-check failed: {exc}", file=sys.stderr)
        return 2
    return 0


def _progress(step: str, summary: str) -> None:
    print(f"[{step}] {summary}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the exit status."""
    args = _parse_args(argv)
    if args.self_check:
        return self_check()
    settings = Settings()
    configure_logging(settings.log_level)
    if api_key(settings) is None:
        print(
            "GAME_SERVER_ANTHROPIC_API_KEY is not set: the designer calls the real API.",
            file=sys.stderr,
        )
        return 2
    request = _request(args)
    if request is None:
        return 2
    result = asyncio.run(design_hunt(request, on_progress=_progress, settings=settings))
    if args.json:
        print(design_out(result).model_dump_json(by_alias=True, indent=2))
    else:
        print(render(result))
    return 0 if result.error_code is None else 1


if __name__ == "__main__":
    sys.exit(main())
