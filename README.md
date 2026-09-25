# scavenger-hunt-game-server

HTTP game server for the Scavenger Hunt application, built with [FastAPI](https://fastapi.tiangolo.com/)
and managed with [uv](https://docs.astral.sh/uv/).

## Business logic

| Method | Path         | Purpose                                                    |
|--------|--------------|------------------------------------------------------------|
| `GET`  | `/`          | Liveness check: `200 OK`, `text/plain`: `Hello, World!`    |
| `POST` | `/challenge` | A participant submits a photo for a scavenger-hunt challenge |

### `POST /challenge`

Request: `multipart/form-data` with two parts.

1. **`metadata`**: JSON text, sent with `Content-Type: application/json`:

   ```json
   {
     "session": "aeffe667-4f9f-4108-b5e2-56ae821fe413",
     "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
     "location": { "lat": 51.509948, "long": -1.485923 },
     "capture-time": "2012-03-29T10:05:45-06:00"
   }
   ```

   | Field          | Rules                                                          |
   |----------------|----------------------------------------------------------------|
   | `session`      | UUID of the game session                                       |
   | `participant`  | UUID of the participant                                        |
   | `location`     | `lat` in [-90, 90], `long` in [-180, 180], decimal degrees     |
   | `capture-time` | ISO 8601 date-time **with** a UTC offset (e.g. `-06:00` or `Z`) |

   All fields are required. Unknown fields are rejected.

2. **`challenge-image`**: the photo, as a file part with `Content-Type: image/jpeg`.

Processing:

1. The metadata and image are validated first, so a rejected request stores nothing.
2. The image is written to `<image-base-path>/<random-uuid>.jpeg`
   (see `GAME_SERVER_IMAGE_BASE_PATH`).
3. The server logs:

   ```
   Received challenge request for <session>[<participant>] arrived at <capture-time> from (<lat>,<long>), image stored in: <path to image>
   ```

Responses:

| Status | When                                                                           |
|--------|--------------------------------------------------------------------------------|
| `202`  | Accepted; body `{"image_id": "<uuid>"}` (the stored file's name)               |
| `413`  | Image larger than `GAME_SERVER_MAX_IMAGE_BYTES`                                |
| `415`  | `challenge-image` content type is not `image/jpeg`                             |
| `422`  | Missing part, invalid metadata (JSON or fields), or image bytes are not a JPEG |

Example:

```bash
curl -i localhost:8000/challenge \
  -F 'metadata={"session":"aeffe667-4f9f-4108-b5e2-56ae821fe413","participant":"7c860ccc-9adf-4e22-b54f-3ff158f5d600","location":{"lat":51.509948,"long":-1.485923},"capture-time":"2012-03-29T10:05:45-06:00"};type=application/json' \
  -F 'challenge-image=@photo.jpg;type=image/jpeg'
```

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
| `GAME_SERVER_IMAGE_BASE_PATH` | `data/images` | Where challenge images are stored; created if missing. Relative paths resolve against the working directory |
| `GAME_SERVER_MAX_IMAGE_BYTES` | `10485760` | Largest accepted challenge image (10 MiB)            |

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

### Continuous integration

[`.github/workflows/pr-build.yml`](.github/workflows/pr-build.yml) validates every pull request
(into any branch) when it is opened, reopened or updated with new commits. A newer push cancels
the run still in progress for the same PR.

- **validate**: installs uv, installs the Python pinned in `.python-version` (uv-managed only,
  no caches, so every run starts clean), creates a fresh virtualenv with `uv sync --locked`,
  then runs `ruff check`, `ruff format --check`, `mypy`, `uv build` and `pytest` with coverage.
- **docker**: builds the Docker image without pushing it.

CI does not auto-fix. Run `make check` locally before pushing to catch the same issues.

### Project layout

```
src/game_server/
  __main__.py        # entry point: `python -m game_server` / `game-server`
  app.py             # FastAPI app factory, `GET /`
  challenge.py       # `POST /challenge` route and request handling
  models.py          # request/response models
  storage.py         # ImageStore: writes images to disk
  config.py          # Settings (env / .env)
  logging_config.py  # stderr logging for the app's own loggers
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

In the image, challenge images are stored in `/app/data/images`, which is writable by `nonroot`.
`/app/data` is declared as a volume. Mount one to keep images across container restarts:

```bash
docker run --rm -p 8000:8000 -v game-server-data:/app/data game-server:dev
```

Because there is no shell, use `docker logs` to inspect a container rather than `docker exec`.

## License

See [LICENSE](LICENSE).
