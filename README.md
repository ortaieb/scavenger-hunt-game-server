# scavenger-hunt-game-server

HTTP game server for the Scavenger Hunt application, built with [FastAPI](https://fastapi.tiangolo.com/)
and managed with [uv](https://docs.astral.sh/uv/).

## Business logic

This is the first iteration of the server. It exposes a single endpoint that confirms the service
is up; game features will be added in later issues.

| Method | Path | Response                                    |
|--------|------|---------------------------------------------|
| `GET`  | `/`  | `200 OK`, `text/plain`: `Hello, World!`     |

FastAPI also serves interactive API docs at `/docs` (Swagger UI) and `/redoc`, and the OpenAPI
schema at `/openapi.json`.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs the right Python for you;
  the version is pinned in `.python-version`)
- GNU Make (optional, for the shortcuts below)
- Docker (optional, for the container image)

## Quick start

```bash
make install     # uv sync: create .venv from uv.lock
make run         # start on http://localhost:8000
curl localhost:8000/
# Hello, World!
```

Run `make` (or `make help`) to list all targets.

## Configuration

Settings are read from environment variables, then from a `.env` file in the working directory,
then fall back to defaults. Real environment variables win over `.env`.

| Variable                 | Default   | Description                                                 |
|--------------------------|-----------|-------------------------------------------------------------|
| `GAME_SERVER_HOST`       | `0.0.0.0` | Interface to bind to                                        |
| `GAME_SERVER_PORT`       | `8000`    | HTTP port (1–65535)                                         |
| `GAME_SERVER_LOG_LEVEL`  | `info`    | `critical`, `error`, `warning`, `info`, `debug` or `trace`  |

To use a `.env` file:

```bash
cp .env.example .env    # then edit; .env is git-ignored
```

Invalid values (e.g. `GAME_SERVER_PORT=0`) stop the server at startup with a validation error.

## Development

| Task                              | Command            |
|-----------------------------------|--------------------|
| Run with auto-reload              | `make dev`         |
| Lint (ruff, auto-fix)             | `make lint`        |
| Format (ruff)                     | `make format`      |
| Type-check (mypy, strict)         | `make typecheck`   |
| Tests (pytest)                    | `make test`        |
| Tests with coverage               | `make coverage`    |
| All of the above before a PR      | `make check`       |

Dependencies are managed with uv only: `uv add <pkg>` / `uv add --dev <pkg>`; never edit `uv.lock`
by hand. See [CLAUDE.md](CLAUDE.md) for the full conventions.

### Project layout

```
src/game_server/
  __main__.py   # entry point: `python -m game_server` / `game-server`
  app.py        # FastAPI app factory and routes
  config.py     # Settings (env / .env)
tests/          # pytest suite, mirrors src/
```

## Docker

```bash
make docker-build                 # builds game-server:dev
make docker-run                   # serves on http://localhost:8000
make docker-run PORT=9000         # publish on a different host port
```

Or directly, overriding settings with `-e` / `--env-file`:

```bash
docker run --rm -p 9000:9000 -e GAME_SERVER_PORT=9000 game-server:dev
docker run --rm -p 8000:8000 --env-file .env game-server:dev
```

The image is a multi-stage build:

- **Builder** (`debian:bookworm-slim` + uv) installs a standalone CPython, strips unused parts of
  the stdlib, and installs the project non-editable with runtime dependencies only (no dev tools,
  no source tree).
- **Runtime** (`gcr.io/distroless/cc-debian12:nonroot`) contains only the interpreter and the
  virtualenv. It has no shell or package manager and runs as the unprivileged `nonroot` user
  (uid 65532).

Because there is no shell, use `docker logs` to inspect a container rather than `docker exec`.

## License

See [LICENSE](LICENSE).
