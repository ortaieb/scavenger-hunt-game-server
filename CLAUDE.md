# CLAUDE.md

Guidelines for working in this Python application.

## Package management: uv

This project uses **uv**, not pip/venv/poetry directly. Never call `pip install` or edit `requirements.txt` by hand.

| Task | Command |
|---|---|
| Create/sync the environment from lockfile | `uv sync` |
| Add a runtime dependency | `uv add <package>` |
| Add a dev-only dependency | `uv add --dev <package>` |
| Remove a dependency | `uv remove <package>` |
| Run a command inside the project's venv | `uv run <command>` |
| Run the app | `uv run python -m <package>` |
| Upgrade all dependencies | `uv lock --upgrade` |
| Upgrade one dependency | `uv lock --upgrade-package <package>` |

Dependencies live in `pyproject.toml`; `uv.lock` is committed and must not be hand-edited. After changing `pyproject.toml` directly, run `uv sync` to regenerate the lockfile.

Never run scripts with a bare `python` or `python3` — always `uv run python ...`, so the correct interpreter and dependencies are used.

## Testing

- **Framework:** `pytest`, with `pytest-cov` for coverage and `pytest-mock` for mocking.
- **Location:** tests live in `tests/`, mirroring the `src/` package layout (`src/app/foo.py` → `tests/test_foo.py`).
- **Naming:** test files `test_*.py`; test functions `test_*`; one behaviour per test.
- **Style:** prefer plain `assert` statements (pytest rewrites them) over `unittest`-style assertions. Use `pytest.fixture` for shared setup and `pytest.mark.parametrize` for input variations instead of loops inside a test.
- **Mocking:** patch at the point of use, not the point of definition. Avoid mocking what you don't own — wrap third-party calls in a thin internal function and mock that instead.

Commands:

```bash
uv run pytest                          # run the full suite
uv run pytest tests/test_foo.py        # run one file
uv run pytest -k "some_case"           # run matching tests
uv run pytest --cov=src --cov-report=term-missing   # with coverage
```

Rules for Claude:
- Every new function or bugfix gets a corresponding test in the same change.
- Run `uv run pytest` before considering a task complete; don't report work as done with failing or skipped tests.
- Don't weaken a test (loosen an assertion, add an unwarranted `skip`) to make it pass — fix the underlying issue or ask first.

## Linting, formatting and type checking

- **Linter + formatter:** `ruff`. It replaces flake8/isort/black — don't add any of those separately.
- **Type checker:** `mypy`, run in strict-ish mode (see `pyproject.toml` for the exact config).

Commands:

```bash
uv run ruff check .          # lint
uv run ruff check . --fix    # lint, autofixing what it can
uv run ruff format .         # format
uv run mypy .                # type-check
```

Rules for Claude:
- Run `uv run ruff check . --fix && uv run ruff format .` before finishing any change that touches Python files.
- Run `uv run mypy .` and don't leave new type errors behind. Use precise types; avoid `Any` unless there's no reasonable alternative, and say why when you do.
- Don't disable a rule inline (`# noqa`, `# type: ignore`) to silence a warning without fixing it, unless it's a genuine false positive — and say so in a comment.

## Combined check

Run this before treating any task as finished:

```bash
uv run ruff check . --fix && uv run ruff format . && uv run mypy . && uv run pytest
```

## Database migrations

The schema is Flyway migrations in `db/migrations/` (see the README's *Database migrations*).
Never create or change tables any other way: no DDL in Python, no edits to applied files.

| Task | Command |
|---|---|
| Start the next migration | `make db-new-migration NAME=what_it_does` |
| Apply them locally / to the test database | `make db-migrate` / `make db-migrate-test` |
| What's applied and pending | `make db-info` |
| The CI guard-rails, locally | `make migration-guard` |

Rules for Claude (and everyone):

1. **Never edit, rename or delete a migration that's on `main`.** Fix it with a new one. Flyway
   checksums applied migrations and refuses to run when one has changed, and CI's migration guard
   refuses the PR. The one exception: a migration on `main` that **failed in production** was
   rolled back whole and never recorded, so correct that same file, in a PR labelled
   `fix-unapplied-migration`.
2. **One change per file:** `V<next integer>__<what_it_does>.sql`, lower-case snake case
   (`make db-new-migration NAME=what_it_does`). Its version must be greater than the highest on
   `main`: if another PR took it first, rebase and renumber.
3. **Backward compatible with the running version (expand, then contract).** The old deploy
   serves on the new schema for a while, and a rollback runs old code on it indefinitely:
   - add columns as nullable, or with a default;
   - a rename or type change is add new → backfill → switch the code → drop old, across
     separate releases;
   - drop a column or table only once no deployed code reads it.
4. **Mind the live game.** DDL takes locks that queue every query behind it. Start migrations
   that alter busy tables (`submissions`, `arrivals`, `participants`, `referee_traces`) with
   `SET lock_timeout = '5s';` so they fail fast rather than freeze the game. Don't ship schema
   changes during a live hunt.
5. **Recreate `ruled_submissions` when `submissions` or `rulings` change.** The view uses `s.*`,
   which PostgreSQL expands once, when the view is created: drop and recreate it in the same file,
   or new columns won't appear in it (and PostgreSQL refuses to drop or alter a column it uses).
6. **Name new constraints and indexes explicitly.** V1's keep PostgreSQL's generated names
   (e.g. `hunt_drafts_status_check`); use those names when altering them.
7. **Content changes are migrations too:** backfills, fixes to existing rows and reference data
   go in `V<n>` files under the same rules. Data the app owns at runtime (published hunts,
   drafts, rulings) is only touched to reshape it for a schema change.
8. **Test against data, not just an empty database.** CI migrates an empty database, which won't
   catch, say, `ADD COLUMN … NOT NULL` without a default on a populated table. When a migration
   reshapes existing rows, run it locally against a copy with realistic data before merging.
9. **The server never migrates itself.** Migrations run only through Flyway: `make db-migrate`
   locally, and the workflows in CI and production.

## Project conventions

- Python version: see `requires-python` in `pyproject.toml` — target that version's syntax, not older or newer.
- Use type hints on all new functions (parameters and return type).
- Prefer `pathlib.Path` over `os.path`.
- Prefer f-strings over `.format()` or `%`.
- Keep functions small and single-purpose; extract rather than let one function grow past ~40 lines.
- Public functions and classes get a docstring; keep it to what the signature doesn't already say.
- No bare `except:` — catch specific exceptions.
