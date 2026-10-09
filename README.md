# scavenger-hunt-game-server

HTTP game server for the Scavenger Hunt application, built with [FastAPI](https://fastapi.tiangolo.com/)
and managed with [uv](https://docs.astral.sh/uv/).

## Business logic

| Method | Path         | Purpose                                                    |
|--------|--------------|------------------------------------------------------------|
| `GET`  | `/`          | Liveness check: `200 OK`, `text/plain`: `Hello, World!`    |
| `POST` | [`/join`](docs/api.md#post-join) | A team joins its session with its join code and the player's photo consent |
| `GET`  | [`/sessions/{session}/participants/{participant}/state`](docs/api.md#get-sessionssessionparticipantsparticipantstate) | The team's status, progress and only its current clue |
| `POST` | [`/sessions/{session}/participants/{participant}/arrive`](docs/api.md#post-sessionssessionparticipantsparticipantarrive) | Check in at the current checkpoint: the pose and a one-time code |
| `POST` | [`/sessions/{session}/start`](docs/api.md#post-sessionssessionstart) | **Moderator:** start the session |
| `POST` | [`/sessions/{session}/stop`](docs/api.md#post-sessionssessionstop) | **Moderator:** finish the session, for good |
| `GET`  | [`/sessions/{session}/overview`](docs/api.md#get-sessionssessionoverview) | **Moderator:** the clock, the photos left to review, standings, each team's progress and blocked attempts |
| `GET`  | [`/sessions/{session}/traces`](docs/api.md#get-sessionssessiontraces) | **Moderator:** every verdict, newest first, with its referee trace and any ruling, and the session's spend and wait times |
| `GET`  | [`/sessions/{session}/review`](docs/api.md#get-sessionssessionreview) | **Moderator:** the photos waiting for a ruling, oldest first, beside what they should show and what the referee said, and the latest rulings |
| `GET`  | [`/sessions/{session}/submissions/{submission}/photo`](docs/api.md#get-sessionssessionsubmissionssubmissionphoto) | **Moderator:** a player's photo, as the referee saw it |
| `GET`  | [`/sessions/{session}/checkpoints/{sequence}/reference-photos/{position}`](docs/api.md#get-sessionssessioncheckpointssequencereference-photosposition) | **Moderator:** a checkpoint's reference photo, prepared the same way |
| `POST` | [`/sessions/{session}/submissions/{submission}/ruling`](docs/api.md#post-sessionssessionsubmissionssubmissionruling) | **Moderator:** approve or reject a photo, whatever its verdict; scoring follows the ruling |
| `POST` | [`/designer/drafts`](docs/api.md#post-designerdrafts) | **Organiser:** start designing a hunt for an area and a theme |
| `GET`  | [`/designer/drafts`](docs/api.md#get-designerdrafts) | **Organiser:** the drafts, newest first |
| `GET`  | [`/designer/drafts/{draft}`](docs/api.md#get-designerdraftsdraft) | **Organiser:** a draft in full: its checkpoints, route, problems and cost |
| `GET`  | [`/health`](#deploying-on-railway) | Readiness: `200 {"status": "ok"}` when the submissions database answers, else `503 {"status": "unavailable"}` |
| `POST` | [`/challenge`](docs/api.md#post-challenge) | A participant submits a photo for the checkpoint it checked in at |
| `POST` | [`/checkpoint/proximity`](docs/api.md#post-checkpointproximity) | **Advisory only:** does the player look in range of an open checkpoint? |

## Documentation

- [API reference](docs/api.md): the [game loop](docs/api.md#game-loop) step by step, every
  endpoint, the [submission checks](docs/api.md#submission-checks),
  [scoring](docs/api.md#scoring), the [referee](docs/api.md#referee-visual-challenge) and its
  [evals](docs/api.md#referee-evals), and the [hunt designer](docs/api.md#hunt-designer)
- [Sessions file](docs/sessions-file.md): defining sessions, checkpoints and teams, and what
  stays [secret](docs/sessions-file.md#secrecy)
- [Project layout](docs/project-layout.md): what each module does
- For players and moderators, the web app's
  [player guide](https://github.com/ortaieb/scavenger-hunt-web-app/blob/main/docs/user-guide.md) and
  [moderator guide](https://github.com/ortaieb/scavenger-hunt-web-app/blob/main/docs/moderator-guide.md)

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs the right Python for you;
  the version is pinned in `.python-version`)
- PostgreSQL 16 or later. `make db-up` runs one in Docker
- GNU Make (optional, for the shortcuts below)
- Docker (for the local database and the container image)

## Quick start

```bash
make install     # uv sync: create .venv from uv.lock
make db-up       # PostgreSQL in Docker, on localhost:5432
cp .env.example .env   # its database settings match `make db-up`
make db-reset    # create the tables
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
| `GAME_SERVER_PORT`       | `8000`    | HTTP port (1–65535). When unset, the platform's `PORT` is used (Railway injects it), then 8000 |
| `GAME_SERVER_LOG_LEVEL`  | `info`    | `critical`, `error`, `warning`, `info`, `debug` or `trace`  |
| `GAME_SERVER_IMAGE_BASE_PATH` | `data/images` | Where challenge images are stored; created if missing. Relative paths resolve against the working directory |
| `GAME_SERVER_MAX_IMAGE_BYTES` | `10485760` | Largest accepted challenge image (10 MiB)            |
| `GAME_SERVER_MAX_CAPTURE_AGE_SECONDS` | `300` | Oldest accepted photo, measured from `capture-time` to `received-at` (> 0). See [time checks](docs/api.md#submission-checks) |
| `GAME_SERVER_MAX_CLOCK_SKEW_SECONDS` | `30` | How far `capture-time` may be ahead of `received-at`, for phone clock drift (> 0) |
| `GAME_SERVER_ANTHROPIC_API_KEY` | unset | Claude API key for the [referee](docs/api.md#referee-visual-challenge) and the [hunt-designer agent](docs/api.md#the-agent). Unset: neither calls the API. Never logged |
| `GAME_SERVER_REFEREE_MODEL` | `claude-haiku-4-5` | Model the referee uses (vision + structured outputs) |
| `GAME_SERVER_REFEREE_DEADLINE_SECONDS` | `8` | The whole referee step, retries included, ends by then (> 0). Past it the photo's verdict is `pending`, for a moderator. See [deadline and retries](docs/api.md#deadline-and-retries) |
| `GAME_SERVER_REFEREE_TIMEOUT_SECONDS` | `8` | Longest wait for one attempt, or the time left before the deadline if that's less (> 0) |
| `GAME_SERVER_REFEREE_MAX_RETRIES` | `2` | Retries on connection errors, 429 and 5xx, within the deadline (≥ 0) |
| `GAME_SERVER_REFEREE_MAX_IMAGE_EDGE` | `1568` | Long edge, in px, of the image sent to the model, and of the photos [served to the moderator](docs/api.md#get-sessionssessionsubmissionssubmissionphoto) (> 0) |
| `GAME_SERVER_REFEREE_MAX_REFERENCES` | `2` | How many of a checkpoint's [reference photos](docs/api.md#reference-photos) are sent with each photo, in the sessions file's order (0–5; `0` turns them off). They're **sent to the model provider** (Anthropic) |
| `GAME_SERVER_REFEREE_REFERENCE_MAX_EDGE` | `768` | Long edge, in px, of each reference photo sent to the model (> 0) |
| `GAME_SERVER_REFEREE_MIN_CONFIDENCE` | `0.8` | Model confidence (0–1) at or above which a visual check's `pass`/`fail` counts; below it the check is `uncertain` |
| `GAME_SERVER_ARRIVAL_CODE_TTL_SECONDS` | `600` | How long an [arrival's](docs/api.md#post-sessionssessionparticipantsparticipantarrive) one-time code stays valid, in seconds (> 0) |
| `GAME_SERVER_PROXIMITY_HINT_INTERVAL_SECONDS` | `10` | Minimum seconds between [proximity hints](docs/api.md#post-checkpointproximity) per (session, participant) (> 0) |
| `GAME_SERVER_PHASH_MAX_DISTANCE` | `6` | Hamming distance (0–32 of 64 bits) at or below which a photo is a [duplicate](docs/api.md#submission-checks) of an accepted one |
| `GAME_SERVER_ORGANISER_KEY` | unset | The [hunt designer's](docs/api.md#hunt-designer) key, at least 24 characters. Unset: nobody can use the designer. Never logged |
| `GAME_SERVER_DESIGNER_RUNNER` | `agent` | What fills a draft: `agent`, the [hunt designer](#hunt-designer) (needs `GAME_SERVER_ANTHROPIC_API_KEY`: without it, starting a design answers `503`), or `stub`, a fixed hunt for local development and the web app's tests |
| `GAME_SERVER_DESIGNER_STUB_DELAY_SECONDS` | `1` | How long the `stub` runner takes (≥ 0) |
| `GAME_SERVER_DESIGNER_MIN_SPACING_M` | `150` | The least distance between two of a draft's checkpoints, in metres; see the [draft rules](docs/api.md#draft-rules) |
| `GAME_SERVER_DESIGNER_MODEL` | `claude-sonnet-5-5` | Model the [hunt-designer agent](docs/api.md#the-agent) runs on |
| `GAME_SERVER_DESIGNER_MAX_TURNS` | `30` | Most turns one design run may take (> 0); past it the run fails with `max_turns` |
| `GAME_SERVER_DESIGNER_MAX_BUDGET_USD` | `1.00` | Most one design run may spend, in US dollars (> 0); past it the run fails with `max_budget` |
| `GAME_SERVER_DESIGNER_DEADLINE_SECONDS` | `300` | Longest one design run may take, in seconds (> 0); past it the run is cut off and fails with `deadline` |
| `GAME_SERVER_DESIGNER_MAX_AREA_KM`, `GAME_SERVER_OSM_*` | | The designer's [map data](docs/api.md#map-data): the largest area, and the OpenStreetMap services and timeout |
| `GAME_SERVER_DB_*` | | PostgreSQL connection and pool: see [Database](#database) |
| `GAME_SERVER_SESSIONS_FILE` | unset | JSON file of [game sessions](docs/sessions-file.md#game-sessions-and-checkpoints) to load at startup. Unset: no sessions |

**On Railway**, turn the referee and the hunt designer on by adding
`GAME_SERVER_ANTHROPIC_API_KEY` as a sealed service variable; see
[Deploying on Railway](#deploying-on-railway).

To use a `.env` file:

```bash
cp .env.example .env    # then edit; .env is git-ignored
```

Invalid values (e.g. `GAME_SERVER_PORT=0`), or an invalid sessions file, stop the server at
startup with a validation error.

### Database

Submissions, participants and arrivals live in an external PostgreSQL database, so they
outlive the container. The server reaches it through a connection pool
([`database.py`](src/game_server/database.py), on `psycopg_pool`): each request borrows a
connection and returns it when done. Connections are checked before they're lent out, so ones
the server or a proxy dropped while idle are replaced, and the pool reconnects on its own after
the database restarts.

Give the connection as a URL, as separate fields, or both. A field that is set overrides the
same part of the URL. libpq's own `PG*` variables fill in anything neither sets.

| Variable | Default | Description |
|----------|---------|-------------|
| `GAME_SERVER_DB_URL` | unset | `postgresql://user:password@host:port/dbname`. Holds a credential: never logged |
| `GAME_SERVER_DB_HOST`, `GAME_SERVER_DB_PORT`, `GAME_SERVER_DB_NAME`, `GAME_SERVER_DB_USER`, `GAME_SERVER_DB_PASSWORD` | unset | The URL's parts, separately. The password is never logged |
| `GAME_SERVER_DB_SSLMODE` | `require` | libpq's [`sslmode`](https://www.postgresql.org/docs/current/libpq-ssl.html#LIBPQ-SSL-PROTECTION): `disable`, `allow`, `prefer`, `require`, `verify-ca` or `verify-full`. Overrides any `sslmode` in the URL |
| `GAME_SERVER_DB_SSLROOTCERT` | unset | For `verify-ca`/`verify-full`: the CA file that signed the server's certificate, or `system` for the OS's trusted CAs |
| `GAME_SERVER_DB_SSLCERT`, `GAME_SERVER_DB_SSLKEY` | unset | A client certificate and key, for servers that authenticate clients by certificate |
| `GAME_SERVER_DB_CONNECT_TIMEOUT_SECONDS` | `10` | How long to wait for a new connection to be established (> 0) |
| `GAME_SERVER_DB_POOL_MIN_SIZE` | `1` | Connections kept open (≥ 0) |
| `GAME_SERVER_DB_POOL_MAX_SIZE` | `10` | Most connections open at once (≥ the minimum) |
| `GAME_SERVER_DB_POOL_TIMEOUT_SECONDS` | `10` | How long a request waits for a free connection before failing (> 0) |

**TLS is required by default.** `require` encrypts the connection but doesn't check who the
server is. Where the provider's CA is available, use `verify-full` with
`GAME_SERVER_DB_SSLROOTCERT` to also rule out an impostor. Only turn TLS off (`disable`) for a
database on your own machine, like the one `make db-up` starts.

The pool connects in the background, so an unreachable database doesn't stop the server from
starting. [`GET /health`](#deploying-on-railway) answers `503` until the database answers and
has its tables.

#### Creating the tables

Until schema changes are applied as versioned migrations, the tables are created by
[`schema.sql`](src/game_server/schema.sql). It **drops** the game server's tables if they exist
(with all their data) and creates them from scratch, in one transaction: if anything fails,
the database is left as it was. Other tables in the database aren't touched.

```bash
make db-reset                                   # uses the server's settings (env / .env), TLS included
uv run python -m game_server.db_reset --yes     # the same, without make
psql "$DATABASE_URL" -f src/game_server/schema.sql   # or straight from psql
```

Without `--yes` the command refuses to run. Run it once against a new database, and again
whenever `schema.sql` changes. It deletes the data, **published hunts included**
([published sessions](docs/sessions-file.md#published-sessions) live in the database), so never
run it during a hunt.

## Development

| Task                              | Command            |
|-----------------------------------|--------------------|
| Run with auto-reload              | `make dev`         |
| Lint (ruff, auto-fix)             | `make lint`        |
| Format (ruff)                     | `make format`      |
| Type-check (mypy, strict)         | `make typecheck`   |
| Start / stop a local PostgreSQL (Docker) | `make db-up` / `make db-down` |
| Drop and recreate the tables (**deletes all data**) | `make db-reset` |
| Tests (pytest; needs PostgreSQL)  | `make test`        |
| Live tests: the referee and the designer (real API calls, cost money; need `GAME_SERVER_ANTHROPIC_API_KEY`) | `uv run pytest -m live` |
| Design a hunt from the command line (real API calls; see [below](#designing-a-hunt-from-the-command-line)) | `uv run python -m game_server.designer --area ... --theme ...` |
| Referee eval on your test photos (real API calls; see [Referee evals](docs/api.md#referee-evals)) | `make eval-referee EVAL_DIR=...` |
| Tests with coverage               | `make coverage`    |
| All of the above before a PR      | `make check`       |

The tests need a PostgreSQL database they may wipe. They use
`postgresql://postgres:postgres@localhost:5432/game_server_test` (what `make db-up` creates), or
`GAME_SERVER_TEST_DB_URL` if set. The suite recreates the tables with the
[reset script](#creating-the-tables) when it starts, and empties them before every test.

Dependencies are managed with uv only: `uv add <pkg>` / `uv add --dev <pkg>`; never edit `uv.lock`
by hand. See [CLAUDE.md](CLAUDE.md) for the full conventions.

### Continuous integration

[`.github/workflows/pr-build.yml`](.github/workflows/pr-build.yml) validates every pull request
(into any branch) when it is opened, reopened or updated with new commits. A newer push cancels
the run still in progress for the same PR.

- **validate**: installs uv, installs the Python pinned in `.python-version` (uv-managed only,
  no caches, so every run starts clean), creates a fresh virtualenv with `uv sync --locked`,
  then runs `ruff check`, `ruff format --check`, `mypy`, `uv build` and `pytest` with coverage.
  The tests run against a PostgreSQL 16 service container.
- **docker**: builds the Docker image without pushing it, creates the tables in a PostgreSQL
  container with the image's own reset script, then starts the image the way Railway does (with
  an injected `PORT`, connecting over TLS) and waits for `GET /health` to answer `ok`. Starting
  it is what catches a native library missing from the distroless runtime.

CI does not auto-fix. Run `make check` locally before pushing to catch the same issues.

## Hunt designer

The [hunt designer](docs/api.md#hunt-designer) turns an area and a theme into a draft hunt for
the organiser to review: an agent on the Claude Agent SDK picks real places from OpenStreetMap,
orders them into a walkable loop, and writes their clues and challenges. The web app's designer
screen drives it through the API:

```bash
curl -X POST http://localhost:8000/designer/drafts \
  -H "Authorization: Bearer $GAME_SERVER_ORGANISER_KEY" -H 'Content-Type: application/json' \
  -d '{"area": "Chiswick, London", "theme": "Painters, brewers and the river"}'
# 202 {"id": "…", "status": "running"}: follow GET /designer/drafts/{id} until it's ready or failed
```

It needs `GAME_SERVER_ORGANISER_KEY` and `GAME_SERVER_ANTHROPIC_API_KEY`. A run takes minutes
and costs up to `GAME_SERVER_DESIGNER_MAX_BUDGET_USD` (default $1). It's bounded by its turns,
its budget and `GAME_SERVER_DESIGNER_DEADLINE_SECONDS` (default 5 minutes), and one runs at a
time. Players' requests are served as usual while it runs. A draft whose run is cut short by a
restart or a shutdown fails with `interrupted`; the
[statuses and error codes](docs/api.md#statuses-and-errors) say what each outcome means for the
organiser. `GAME_SERVER_DESIGNER_RUNNER=stub` swaps the agent for a fixed hunt, with no key and
no API calls, for local development and the web app's tests.

At startup, with a key set, the server checks that the Claude Code binary bundled with the Agent
SDK starts, and logs `Hunt designer self-check: <version>`, or why it failed.

### Designing a hunt from the command line

The [hunt-designer agent](docs/api.md#the-agent) runs from the command line too, with the same
prompt, tools, sandbox and limits as the API, so its prompt
([`designer_prompt.md`](src/game_server/designer_prompt.md)) can be tuned without the server or
the web app. It needs only `GAME_SERVER_ANTHROPIC_API_KEY` and network access (the Claude API and
OpenStreetMap): no database and no running server. Every run calls the real API and costs money,
up to `GAME_SERVER_DESIGNER_MAX_BUDGET_USD`.

```bash
uv run python -m game_server.designer --area "Chiswick, London" \
  --theme "Painters, brewers and the river" [--checkpoints 3] [--max-walk-km 3] [--json]
```

It prints the accepted draft (each checkpoint's place, clue, scene, pose, proximity and why it
was picked, then the route) or the run's error code, and the run's stats: model, turns, cost,
duration and result. `--json` prints the area, checkpoints and route in the
[API's draft shape](docs/api.md#get-designerdraftsdraft), with the run's stats under `run`.
Progress and the tool-call log lines go to stderr. The exit status is 0 for an accepted draft,
1 for a run that ended without one, and 2 for a setup problem (no key, an invalid request).

`uv run python -m game_server.designer --self-check` only starts the Claude Code binary bundled
with the Agent SDK and prints its version, to check the platform can run it.

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
  (uid 65532). It also carries `libz.so.1`, copied from the builder: numpy's wheel links against
  it and distroless/cc doesn't ship it. When adding a dependency with native code, run `ldd`
  over the venv's `*.so` files in the builder and copy in anything the runtime lacks.

The image needs a PostgreSQL database: pass its [settings](#database) with `-e` or
`--env-file`. To create the tables from the image (it has no shell, so override the
entrypoint):

```bash
docker run --rm --env-file .env --entrypoint /app/.venv/bin/python game-server:dev \
  -m game_server.db_reset --yes
```

From inside a container, `localhost` is the container itself: reach a database on your machine
at `host.docker.internal` (add `--add-host=host.docker.internal:host-gateway` on Linux).

In the image, challenge images are stored in `/app/data/images`, writable by `nonroot`.
`/app/data` is declared as a volume. Mount one to keep images across container restarts:

```bash
docker run --rm -p 8000:8000 --env-file .env -v game-server-data:/app/data game-server:dev
```

To load game sessions, mount the file read-only and point the setting at it:

```bash
docker run --rm -p 8000:8000 \
  -v "$PWD/sessions.json:/app/sessions.json:ro" \
  -e GAME_SERVER_SESSIONS_FILE=/app/sessions.json \
  game-server:dev
```

Because there is no shell, use `docker logs` to inspect a container rather than `docker exec`.

## Deploying on Railway

The game server deploys to [Railway](https://railway.com/) from this repository's
`Dockerfile`. [`railway.toml`](railway.toml) declares the build and the health check. Settings
in it **override the same settings in the Railway dashboard**.

- **Port.** Railway injects `PORT` and routes traffic to it. The server listens on
  `GAME_SERVER_PORT` if set, otherwise on `PORT`, otherwise 8000. **Don't set
  `GAME_SERVER_PORT` on Railway**: it would override `PORT` and traffic wouldn't reach the
  server.
- **Health check.** `railway.toml` sets `healthcheckPath = "/health"`. A new deploy only takes
  traffic once `GET /health` answers `200`, which needs the database to answer and have its
  tables.

Set these up once in the dashboard (they can't be declared in `railway.toml`):

1. **A PostgreSQL database.** Add Railway's PostgreSQL service to the project, then on the game
   server set `GAME_SERVER_DB_URL` to the reference variable `${{Postgres.DATABASE_URL}}`
   (the private-network URL). TLS stays on: `GAME_SERVER_DB_SSLMODE` defaults to `require`.
   Railway's PostgreSQL presents a self-signed certificate, so `verify-ca`/`verify-full` don't
   apply unless you supply its CA. Create the tables once, from your machine, against the
   database's **public** URL (`DATABASE_PUBLIC_URL` in the PostgreSQL service's variables):

   ```bash
   GAME_SERVER_DB_URL='postgresql://...' make db-reset   # DESTRUCTIVE: drops existing tables
   ```

   Until then `/health` answers `503` and the deploy doesn't take traffic.
2. **A volume mounted at `/app/data`.** Photos (`images/`) and the sessions file live there.
   Without a volume they're lost on every deploy.
3. **`RAILWAY_RUN_UID=0` as a service variable.** Railway mounts volumes owned by root, and
   the image runs as the unprivileged `nonroot` user, so it can't write to the volume. This
   variable runs the container as root on Railway (Railway's documented fix). It's a
   trade-off: the non-root hardening doesn't apply there. Without it, every photo submission
   fails when the server stores the image. `/health` checks the database, not the volume, so it
   doesn't catch this.
4. **Game data variables:**
   - `GAME_SERVER_SESSIONS_FILE` pointing at a sessions file on the volume, e.g.
     `/app/data/hunt/sessions.json`. Upload it with `railway volume` or the dashboard.
   - Optionally `GAME_SERVER_ANTHROPIC_API_KEY` (sealed) to turn on the
     [referee](docs/api.md#referee-visual-challenge) and the [hunt designer](#hunt-designer).
   - Optionally `GAME_SERVER_ORGANISER_KEY` (sealed) to open the
     [hunt designer](docs/api.md#hunt-designer) to the organiser.
   - Optionally the hunt designer's model, limits and deadline, if the defaults don't suit:
     `GAME_SERVER_DESIGNER_MODEL`, `GAME_SERVER_DESIGNER_MAX_TURNS`,
     `GAME_SERVER_DESIGNER_MAX_BUDGET_USD` and `GAME_SERVER_DESIGNER_DEADLINE_SECONDS`.

**The hunt designer on Railway.** Each design run starts a Claude Code process next to the
server. Anthropic suggests allowing about 1 GiB of memory per agent run, so keep the service's
memory limit well above what the server uses on its own (Claude Code takes about 230 MiB once
started, and grows with the run). The image has no shell to run `--self-check` in: the deploy
logs show the startup's `Hunt designer self-check` line instead. A deploy or restart during a
design run fails its draft with `interrupted`: stopping Claude Code and marking the draft takes
the server about 5 s after `SIGTERM`, and if it's killed sooner, the new deploy marks the draft
when it starts.

**Reference photos** go on the volume next to the sessions file, which they're resolved
against. The referee sends them to the model provider (Anthropic) with each photo judged at
their checkpoint, so they must be the organisers' own, with nobody in shot:

```text
/app/data/hunt/
  sessions.json                # GAME_SERVER_SESSIONS_FILE=/app/data/hunt/sessions.json
  reference/fountain-north.jpg
  reference/fountain-south.jpg
```

Upload the photos **before** a sessions file that names them. Otherwise the new deploy can't
start, and Railway keeps traffic on the old one.

## License

See [LICENSE](LICENSE).

