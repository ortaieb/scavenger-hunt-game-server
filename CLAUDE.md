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

## Project conventions

- Python version: see `requires-python` in `pyproject.toml` — target that version's syntax, not older or newer.
- Use type hints on all new functions (parameters and return type).
- Prefer `pathlib.Path` over `os.path`.
- Prefer f-strings over `.format()` or `%`.
- Keep functions small and single-purpose; extract rather than let one function grow past ~40 lines.
- Public functions and classes get a docstring; keep it to what the signature doesn't already say.
- No bare `except:` — catch specific exceptions.
