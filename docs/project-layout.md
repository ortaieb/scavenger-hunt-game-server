# Project layout

Back to the [README](../README.md).

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
    session_running.py  # session_not_started, session_stopped
    checked_in.py    # not_checked_in, check_in_expired (the team's active arrival)
    time_window.py   # outside_window, stale_capture, capture_in_future
    geofence.py      # out_of_range
    duplicate_photo.py  # duplicate_photo
    visual.py        # scene_matches, pose_correct (from the referee's report)
  database.py        # Database: PostgreSQL connection pool and its settings
  schema.sql         # the tables: dropped and created from scratch (destructive)
  db_reset.py        # `python -m game_server.db_reset --yes`: runs schema.sql
  submissions.py     # SubmissionStore: submissions, attempts, participants, arrivals
  referee_traces.py  # record_trace: one row per referee call, with its submission; read_traces
  clock.py           # injectable UTC clock, and the monotonic timer for processing_ms
  geo.py             # haversine distance_m
  phash.py           # perceptual_hash (Pillow + numpy), hamming_distance
  proximity.py       # `POST /checkpoint/proximity` advisory hint
  checkpoints.py     # `GET /sessions/{session}/checkpoints/{sequence}/challenge` pose
  health.py          # `GET /health` readiness check
  join.py            # `POST /join`, and find_participant for later endpoints
  moderation.py      # require_moderator: the session moderator code (Bearer)
  errors.py          # ApiError: {detail, code} error responses
  session_runs.py    # SessionRun and session_phase (scheduled / running / stopped)
  session_control.py # moderator start and stop, and the session clock
  overview.py        # `…/overview`: the moderator's standings, progress and blocked attempts
  traces.py          # `…/traces`: the moderator's verdicts with their referee traces and spend
  game_state.py      # team state: team_state() rules and the `…/state` endpoint
  scoring.py         # points by order of arrival: team_points(), places()
  arrive.py          # `…/arrive`: check in, pose and one-time code
  arrivals.py        # Arrival, LatestArrival: when a check-in still holds for a photo
  lookup.py          # find_checkpoint: shared session/checkpoint lookup (404s)
  imaging.py         # safe image decoding: pixel cap, EXIF orientation, decode errors
  referee.py         # visual-challenge referee on the Claude API
  referee_prompt.md  # the referee's system prompt
  referee_references.py  # ReferencePhotos: each checkpoint's reference photos, prepared once
  pricing.py         # Claude API list prices and cost_usd (traces and eval report)
  evals/             # offline referee eval harness (manifest, scoring, report, runner)
evals/referee/       # eval manifest schema and example (no photos in the repo)
  rate_limit.py      # in-memory per-key RateLimiter
  config.py          # Settings (env / .env)
  logging_config.py  # stderr logging for the app's own loggers
tests/          # pytest suite, mirrors src/
```

