"""Guard-rails for Flyway migrations in a pull request: `python tools/check_migrations.py`.

Compares the PR (HEAD) with its base branch and fails if:

- a migration that's already on the base branch was modified, renamed or deleted. Flyway
  checksums every applied migration and refuses to run when one has changed, so the fix for a
  merged migration is always a new one;
- a new migration's version isn't greater than the highest on the base branch, so two PRs that
  both add `V5` can't both merge: the second renumbers after rebasing;
- a file in db/migrations isn't named `V<n>__<what_it_does>.sql`.

One escape hatch, `--allow-modified` (the `fix-unapplied-migration` label in CI): a migration
that's on the base branch but failed in production. PostgreSQL rolls a failed migration back
whole and Flyway doesn't record it, so the fix is to correct that same file. It allows
modifying a migration, never renaming or deleting one.

Standard library only, so CI can run it without installing the project.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

MIGRATIONS = "db/migrations"
MIGRATION_NAME = re.compile(r"^V([1-9][0-9]*)__([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
LABEL = "fix-unapplied-migration"


@dataclass(frozen=True)
class Change:
    """One line of `git diff --name-status`: A, M, D, R…, C… or T, and the path(s)."""

    status: str
    paths: tuple[str, ...]


def git(*args: str) -> str:
    """Run git and return its output; raises `subprocess.CalledProcessError` on failure."""
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git isn't on the PATH")
    # S603: git itself, with arguments built here (refs and paths), and no shell.
    done = subprocess.run(  # noqa: S603
        [executable, *args], capture_output=True, text=True, check=True
    )
    return done.stdout


def changes(base: str) -> list[Change]:
    """What the PR changed under db/migrations since it branched from `base`."""
    out = git("diff", "--name-status", "-M", f"{base}...HEAD", "--", MIGRATIONS)
    result = []
    for line in out.splitlines():
        status, *paths = line.split("\t")
        result.append(Change(status=status, paths=tuple(paths)))
    return result


def versions_on(ref: str) -> list[int]:
    """The migration versions on `ref` (the base branch's tip)."""
    # By pathspec, so a base without db/migrations yet just lists nothing.
    out = git("ls-tree", "--name-only", ref, "--", f"{MIGRATIONS}/")
    names = (file_name(path) for path in out.splitlines())
    return [int(m.group(1)) for name in names if (m := MIGRATION_NAME.match(name))]


def file_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def problems(base: str, allow_modified: bool) -> list[str]:
    """Everything wrong with the PR's migrations, each with what to do about it."""
    found = []
    highest = max(versions_on(base), default=0)
    for change in changes(base):
        kind, path = change.status[0], change.paths[-1]
        if kind == "A":
            found += _added(path, highest, base)
        elif kind == "M" and allow_modified:
            continue
        elif kind == "M":
            found.append(
                f"{path} is already on {base} and was modified. Never edit a merged migration: "
                "add a new one that makes the change. (If it failed in production, so it was "
                f"never applied, label the PR `{LABEL}` to correct it in place.)"
            )
        else:
            what = {"D": "deleted", "R": "renamed", "C": "copied", "T": "changed type"}.get(
                kind, f"changed ({change.status})"
            )
            found.append(
                f"{change.paths[0]} is already on {base} and was {what}. Merged migrations are "
                "never renamed or deleted: add a new migration instead."
            )
    return found


def _added(path: str, highest: int, base: str) -> list[str]:
    name = file_name(path)
    match = MIGRATION_NAME.match(name)
    if match is None:
        return [
            f"{path}: not a migration name. Use V<n>__<what_it_does>.sql, lower-case snake case "
            "(make db-new-migration NAME=… creates the next one)."
        ]
    version = int(match.group(1))
    if version <= highest:
        return [
            f"{path}: version {version} isn't greater than V{highest}, the highest on {base}. "
            f"Rebase on {base} and renumber it V{highest + 1} (and any after it)."
        ]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    """Check the PR's migrations against `--base`; exit 1 with the problems if any."""
    parser = argparse.ArgumentParser(
        description="Check a pull request's Flyway migrations against its base branch."
    )
    parser.add_argument("--base", default="origin/main", help="the base branch (a git ref)")
    parser.add_argument(
        "--allow-modified",
        action="store_true",
        help=f"allow correcting a merged migration that failed in production (label `{LABEL}`)",
    )
    args = parser.parse_args(argv)
    found = problems(args.base, args.allow_modified)
    for problem in found:
        print(f"::error::{problem}" if _in_github_actions() else f"error: {problem}")
    if not found:
        print(f"Migrations OK against {args.base}.")
    return 1 if found else 0


def _in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


if __name__ == "__main__":
    sys.exit(main())
