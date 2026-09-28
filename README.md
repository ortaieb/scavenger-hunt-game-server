# scavenger-hunt-game-server

HTTP game server for the Scavenger Hunt application, built with [FastAPI](https://fastapi.tiangolo.com/)
and managed with [uv](https://docs.astral.sh/uv/).

## Business logic

| Method | Path         | Purpose                                                    |
|--------|--------------|------------------------------------------------------------|
| `GET`  | `/`          | Liveness check: `200 OK`, `text/plain`: `Hello, World!`    |
| `POST` | `/challenge` | A participant submits a photo for a scavenger-hunt challenge |

### `POST /challenge`

A participant submits a photo for one checkpoint. The server records it as their next attempt
at that checkpoint and returns a **verdict it decides itself**.

**Every verdict is decided server-side.** The client supplies only claims: coordinates, capture
time and the photo. Nothing it sends can mark a check as passed, and unknown metadata fields
(such as a `verified` or `in-range` flag) are rejected. The web app may warn a player who looks
out of range, but that is a courtesy and plays no part in the verdict.

Request: `multipart/form-data` with two parts.

1. **`metadata`**: JSON text, sent with `Content-Type: application/json`:

   ```json
   {
     "session": "aeffe667-4f9f-4108-b5e2-56ae821fe413",
     "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
     "checkpoint": 2,
     "location": { "lat": 51.509948, "long": -1.485923 },
     "capture-time": "2026-10-03T12:05:45+01:00"
   }
   ```

   | Field          | Rules                                                               |
   |----------------|---------------------------------------------------------------------|
   | `session`      | UUID of a [loaded game session](#game-sessions-and-checkpoints)     |
   | `participant`  | UUID of the participant (a claim: not authenticated yet)            |
   | `checkpoint`   | The checkpoint's `sequence` in that session: a JSON integer ≥ 1. `"2"`, `2.0` and `true` are rejected |
   | `location`     | `lat` in [-90, 90], `long` in [-180, 180], decimal degrees          |
   | `capture-time` | ISO 8601 date-time **with** a UTC offset (e.g. `-06:00` or `Z`)      |

   All fields are required. Unknown fields are rejected.

2. **`challenge-image`**: the photo, as a file part with `Content-Type: image/jpeg`.

Processing:

1. The server stamps **`received-at`** from its own UTC clock. This, not the client's
   `capture-time`, is the time the verdict uses and reports.
2. The metadata and image are validated, and the session and checkpoint are looked up.
   A request rejected at this stage (any `4xx`) stores nothing: no image, no database row.
3. Every registered check runs, even after one rejects, so the verdict lists every reason.
   Checks can only rule a submission *out*:
   - Any rejection → verdict **`failed`**.
   - No rejections → verdict **`pending`**, never `pass`. A phone can report any location, so
     passing the deterministic checks doesn't prove the player was there. `pass` is reserved
     for when presence-proof (the checkpoint's one-time code) and visual-challenge checks exist.

   The registered checks are described in [Submission checks](#submission-checks).
4. The image is written to `<image-base-path>/<random-uuid>.jpeg`, and the submission is
   recorded in the database as the next **attempt** for its (session, participant, checkpoint):
   1, 2, 3…. Failed submissions count as attempts.
5. The server logs:

   ```
   Received challenge request for <session>[<participant>] arrived at <capture-time> from (<lat>,<long>), image stored in: <path>; checkpoint <n> attempt <n> distance <metres>m verdict <verdict> rejections [<code>,...]
   ```

Response body (`200` or `202`):

```json
{
  "verdict": {
    "game": "aeffe667-4f9f-4108-b5e2-56ae821fe413",
    "participant": "7c860ccc-9adf-4e22-b54f-3ff158f5d600",
    "checkpoint": {
      "sequence": 2,
      "attempt": 1,
      "time": "2026-10-03T11:06:02.113Z",
      "verdict": "failed",
      "rejections": [
        { "code": "outside_window", "message": "This checkpoint isn't open right now." }
      ]
    }
  },
  "image_id": "fb5fb9c2-cdde-480f-8864-904829c53716"
}
```

- `time` is `received-at`, in UTC.
- Each rejection has a stable snake_case `code` for clients to branch on, and a `message` that
  is safe to show the player. It never contains checkpoint coordinates, distances or bearings.

Responses:

| Status | When                                                                           |
|--------|--------------------------------------------------------------------------------|
| `200`  | Recorded, verdict `failed` (at least one rejection)                            |
| `202`  | Recorded, verdict `pending` (no rejections)                                    |
| `404`  | Unknown `session` (`{"detail": "unknown session"}`), or no checkpoint with that `sequence` in the session (`"unknown checkpoint"`) |
| `413`  | Image larger than `GAME_SERVER_MAX_IMAGE_BYTES`                                |
| `415`  | `challenge-image` content type is not `image/jpeg`                             |
| `422`  | Missing part, invalid metadata (JSON, fields, unknown fields), or image bytes are not a JPEG |

Example:

```bash
curl -i localhost:8000/challenge \
  -F 'metadata={"session":"aeffe667-4f9f-4108-b5e2-56ae821fe413","participant":"7c860ccc-9adf-4e22-b54f-3ff158f5d600","checkpoint":2,"location":{"lat":51.509948,"long":-1.485923},"capture-time":"2026-10-03T12:05:45+01:00"};type=application/json' \
  -F 'challenge-image=@photo.jpg;type=image/jpeg'
```

#### Submission checks

Each check can only add a rejection. Codes are stable and messages are safe to show the player.

**Time** (`checks/time_window.py`). The deciding clock is the server's `received-at`. The
client's `capture-time` is a claim: it can get a submission rejected, but it can never rescue
one received outside the window. All three rules are evaluated, so a submission can get
several of these codes.

| Code                | Rejects when                                                                  | Message |
|---------------------|-------------------------------------------------------------------------------|---------|
| `outside_window`    | `received-at` is before the checkpoint's [effective window](#game-sessions-and-checkpoints) opens or after it closes. Both bounds are inclusive: exactly at opening or closing is accepted | "This checkpoint isn't open right now." |
| `stale_capture`     | `received-at − capture-time` > `GAME_SERVER_MAX_CAPTURE_AGE_SECONDS` (default 300). Exactly at the limit is accepted | "Photo was taken too long ago, please take a new one." |
| `capture_in_future` | `capture-time − received-at` > `GAME_SERVER_MAX_CLOCK_SKEW_SECONDS` (default 30, allowing for phone clock drift) | "Photo's capture time is ahead of the server's clock. Check your phone's date and time, then take a new one." |

Times are compared as absolute instants, so `2026-10-03T12:00:00Z` and
`2026-10-03T06:00:00-06:00` behave identically. Messages never reveal a window's times.

**Geofence** (`checks/geofence.py`). The server measures the distance itself, from the
submitted coordinates to the checkpoint's `location`. It uses the haversine great-circle
formula with the mean Earth radius (6 371 008.8 m), which is well within tolerance for
proximities of tens to hundreds of metres.

| Code           | Rejects when                                                        | Message |
|----------------|---------------------------------------------------------------------|---------|
| `out_of_range` | distance > the checkpoint's `proximity`. Exactly on the boundary counts as in range | "Your location is outside the checkpoint area." |

- **Claim, not proof.** A phone can report any location it likes. So the geofence can rule a
  submission *out* (the claim itself says "not here"), but passing it proves nothing about
  presence. That's what the checkpoint's one-time code will be for, and why a submission with
  no rejections is `pending`, never `pass`.
- **The fence is never widened.** There's no allowance for GPS accuracy, and the client can't
  send an accuracy value (unknown metadata fields are rejected). If radii prove too tight in
  the field, the moderator widens `proximity` in the sessions file.
- **The answer isn't leaked.** The message carries no distance, direction or coordinates, so
  retries can't be played as a hot/cold game. The response body never includes the distance.
  It is stored on the submission row and written to the server log, for moderator review only.

The duplicate-photo check (#11) will be added here.

#### Submission records

Submissions are stored in SQLite (`GAME_SERVER_DB_PATH`), in a `submissions` table:

| Column                      | Content                                                   |
|-----------------------------|-----------------------------------------------------------|
| `id`                        | Row id                                                    |
| `session`, `participant`    | UUIDs from the request                                    |
| `checkpoint`, `attempt`     | Checkpoint `sequence` and this attempt's number           |
| `received_at`               | Server receive time, ISO 8601 UTC                         |
| `capture_time`              | The client's claim, as sent                               |
| `lat`, `long`               | The client's claimed position                             |
| `image_id`                  | Stored image's file name (without `.jpeg`)                |
| `verdict`                   | `failed`, `pending` (or later `pass`)                     |
| `rejections`                | JSON list of `{code, message}`                            |
| `distance_m`                | Metres from the claimed position to the checkpoint (server-side only; empty for rows recorded before #10) |

The attempt number is allocated and the row inserted in one transaction, so concurrent
submissions can't share an attempt number. A unique constraint backs this up. Every row carries
its `session`, so all of a session's data can be deleted together when the session closes.
The schema is created at startup. Later changes, such as new per-check audit columns, are
applied as ordered migrations tracked with SQLite's `PRAGMA user_version`, so existing
databases are upgraded in place.

### Game sessions and checkpoints

A **game session** is one hunt. It has a region, a start and end time, and an ordered list of
**checkpoints**. Each checkpoint is a place participants must find from a clue and photograph.
Moderators write sessions by hand in a JSON file that the server loads at startup (see
`GAME_SERVER_SESSIONS_FILE`). A moderator API will come later.

The file is a JSON list of sessions. Abridged from [`sessions.example.json`](sessions.example.json):

```json
[
  {
    "id": "aeffe667-4f9f-4108-b5e2-56ae821fe413",
    "name": "Hyde Park Saturday Hunt",
    "location": "Hyde Park and Kensington Gardens, London",
    "start-time": "2026-10-03T10:00:00+01:00",
    "end-time": "2026-10-03T13:00:00+01:00",
    "checkpoints": [
      {
        "sequence": 1,
        "name": "Stone fountain",
        "clue": "Where a princess is remembered by water that never runs in a straight line.",
        "location": { "lat": 51.504873, "long": -0.169872 },
        "proximity": 40
      },
      {
        "sequence": 3,
        "name": "Speakers' Corner",
        "clue": "...",
        "location": { "lat": 51.512346, "long": -0.159203 },
        "proximity": 50,
        "window": {
          "opens-at": "2026-10-03T12:00:00+01:00",
          "closes-at": "2026-10-03T13:00:00+01:00"
        }
      }
    ]
  }
]
```

| Field                      | Rules                                                                |
|----------------------------|----------------------------------------------------------------------|
| `id`                       | UUID, unique across the file. Participants send it as `session`      |
| `name`                     | Non-empty                                                            |
| `location`                 | Non-empty description of the region (free text, not coordinates)    |
| `start-time`, `end-time`   | ISO 8601 with a UTC offset; `end-time` must be after `start-time`    |
| `checkpoints`              | At least one                                                         |
| `checkpoints[].sequence`   | Integer ≥ 1, unique within the session: the checkpoint's id           |
| `checkpoints[].name`, `clue` | Non-empty                                                          |
| `checkpoints[].location`   | `{lat, long}`: the answer to the clue (see *Secrecy* below)            |
| `checkpoints[].proximity`  | Integer > 0: how many metres from `location` counts as "arrived"     |
| `checkpoints[].window`     | Optional `{opens-at, closes-at}`, `opens-at` < `closes-at`, both within the session's start/end |

Unknown fields are rejected everywhere. If the file can't be read, isn't valid JSON, breaks any
rule above or repeats a session id, the server **refuses to start**. The error lists each
problem as `path: message`, e.g. `[0].checkpoints[1].proximity: Input should be greater than 0`.

**Effective window:** a checkpoint accepts submissions during its `window` if it has one,
otherwise for the whole session (`start-time` to `end-time`).

#### Secrecy

A checkpoint's coordinates are the answer to its clue. **No endpoint may return checkpoint
coordinates, or distances to them**, not even in error messages. Validation errors from the
sessions file never echo input values, so coordinates don't reach the logs either. Keep the
real sessions file out of version control: `sessions.json` is git-ignored.

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
| `GAME_SERVER_MAX_CAPTURE_AGE_SECONDS` | `300` | Oldest accepted photo, measured from `capture-time` to `received-at` (> 0). See [time checks](#submission-checks) |
| `GAME_SERVER_MAX_CLOCK_SKEW_SECONDS` | `30` | How far `capture-time` may be ahead of `received-at`, for phone clock drift (> 0) |
| `GAME_SERVER_DB_PATH` | `data/game.sqlite3` | SQLite database of [submissions](#submission-records); created with its directory if missing |
| `GAME_SERVER_SESSIONS_FILE` | unset | JSON file of [game sessions](#game-sessions-and-checkpoints) to load at startup. Unset: no sessions |

To use a `.env` file:

```bash
cp .env.example .env    # then edit; .env is git-ignored
```

Invalid values (e.g. `GAME_SERVER_PORT=0`), or an invalid sessions file, stop the server at
startup with a validation error.

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
  sessions.py        # game session/checkpoint models, file loading, SessionRepository
  storage.py         # ImageStore: writes images to disk
  checks/
    base.py          # Check protocol, SubmissionContext, Rejection, verdict decision
    registry.py      # get_checks: the checks every submission goes through
    time_window.py   # outside_window, stale_capture, capture_in_future
    geofence.py      # out_of_range
  submissions.py     # SubmissionStore: SQLite record of submissions and attempts
  clock.py           # injectable UTC clock
  geo.py             # haversine distance_m
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

In the image, challenge images are stored in `/app/data/images` and the submissions database
in `/app/data/game.sqlite3`. Both are writable by `nonroot`.
`/app/data` is declared as a volume. Mount one to keep images and submissions across container restarts:

```bash
docker run --rm -p 8000:8000 -v game-server-data:/app/data game-server:dev
```

To load game sessions, mount the file read-only and point the setting at it:

```bash
docker run --rm -p 8000:8000 \
  -v "$PWD/sessions.json:/app/sessions.json:ro" \
  -e GAME_SERVER_SESSIONS_FILE=/app/sessions.json \
  game-server:dev
```

Because there is no shell, use `docker logs` to inspect a container rather than `docker exec`.

## License

See [LICENSE](LICENSE).
