-- The game server's tables, dropped (if they exist) and created from scratch.
--
-- DESTRUCTIVE: every submission, referee trace, participant and arrival is deleted. Meant for
-- development, until schema changes are applied as versioned migrations.
--
-- Run it with the server's connection settings:  make db-reset
-- or with psql:                                   psql "$DATABASE_URL" -f src/game_server/schema.sql
--
-- One transaction: if any statement fails, the database is left as it was.

BEGIN;

DROP TABLE IF EXISTS
    referee_traces, referee_prompts, blocked_attempts, session_runs, arrivals, participants,
    submissions
    CASCADE;

-- A team's check-ins at a checkpoint, each with a one-time code. Not used for scoring: the
-- order of arrival is set by the accepted photo's received_at. A photo is held to its team's
-- active arrival (see submissions.arrival_id).
CREATE TABLE arrivals (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session     UUID        NOT NULL,
    participant UUID        NOT NULL,
    checkpoint  INTEGER     NOT NULL,
    code        TEXT        NOT NULL,
    pose        TEXT,
    issued_at   TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX arrivals_by_team ON arrivals (session, participant, checkpoint);

-- One row per photo submitted. Every row carries its session, so all of a session's data can
-- be deleted together when the session closes.
CREATE TABLE submissions (
    id                    BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session               UUID             NOT NULL,
    participant           UUID             NOT NULL,
    checkpoint            INTEGER          NOT NULL,
    attempt               INTEGER          NOT NULL,
    received_at           TIMESTAMPTZ      NOT NULL,
    capture_time          TIMESTAMPTZ      NOT NULL,
    lat                   DOUBLE PRECISION NOT NULL,
    long                  DOUBLE PRECISION NOT NULL,
    image_id              UUID             NOT NULL,
    verdict               TEXT             NOT NULL CHECK (verdict IN ('failed', 'pending', 'pass')),
    -- [{code, message}] of the failed checks.
    rejections            JSONB            NOT NULL,
    -- From the submitted location to the checkpoint. Server-side only.
    distance_m            DOUBLE PRECISION NOT NULL,
    -- The photo's 64-bit perceptual hash as 16 hex digits (BIGINT is signed).
    phash                 TEXT             NOT NULL,
    -- On a duplicate_photo rejection, the accepted submission it matched.
    phash_match_id        BIGINT           REFERENCES submissions (id) ON DELETE SET NULL,
    -- Every check that ran: [{check, outcome, confidence, reason, detail}].
    checks                JSONB            NOT NULL,
    -- The team's active arrival at the checkpoint that the photo used; NULL when there was none.
    arrival_id            BIGINT           REFERENCES arrivals (id),
    -- From received_at until the verdict was recorded, whether or not the referee was called.
    processing_ms         INTEGER          NOT NULL CHECK (processing_ms >= 0),
    UNIQUE (session, participant, checkpoint, attempt)
);
CREATE INDEX submissions_by_arrival ON submissions (arrival_id);

-- Each system prompt the referee has used, stored once and named by its hash in the traces.
-- Not session data: a prompt holds no player data or scene.
CREATE TABLE referee_prompts (
    sha256        TEXT        PRIMARY KEY,
    text          TEXT        NOT NULL,
    first_used_at TIMESTAMPTZ NOT NULL
);

-- One row per referee call (status ok or error), written in the same transaction as its
-- submission; no row when the referee wasn't consulted or is disabled. For moderator audit
-- and cost tracking. Server-side only: user_text holds the scene (the answer to the clue),
-- and response_text and judgement describe the photo. Rows carry their session and go with
-- their submission, so they're deleted with the session's other data.
CREATE TABLE referee_traces (
    id                          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session                     UUID        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL,
    submission_id               BIGINT      NOT NULL UNIQUE
                                            REFERENCES submissions (id) ON DELETE CASCADE,
    -- The stored player photo, the submission's own file: there's no second copy.
    image_id                    UUID        NOT NULL,
    -- The prepared JPEG the model saw (upright, resized, no EXIF); NULL when the photo
    -- couldn't be prepared (error_code invalid_image), so none was sent.
    image_sha256                TEXT,
    image_width                 INTEGER,
    image_height                INTEGER,
    -- The reference photos sent with it: [] until they are. (Not `references`: a reserved word.)
    reference_photos            JSONB       NOT NULL,
    prompt_sha256               TEXT        NOT NULL REFERENCES referee_prompts (sha256),
    -- The text part of the user turn: the scene and the pose.
    user_text                   TEXT        NOT NULL,
    model                       TEXT        NOT NULL,
    request_id                  TEXT,
    status                      TEXT        NOT NULL CHECK (status IN ('ok', 'error')),
    error_code                  TEXT,
    stop_reason                 TEXT,
    -- The model's output as received, and its parsed judgement when it was valid. NULL
    -- when no reply came back.
    response_text               TEXT,
    judgement                   JSONB,
    input_tokens                INTEGER,
    output_tokens               INTEGER,
    -- NULL until the referee uses prompt caching.
    cache_read_input_tokens     INTEGER,
    cache_creation_input_tokens INTEGER,
    -- At list price (game_server/pricing.py); NULL without a reply, or for an unknown model.
    cost_usd                    NUMERIC,
    -- The model call, SDK retries included.
    latency_ms                  INTEGER     NOT NULL
);
CREATE INDEX referee_traces_by_session ON referee_traces (session);

-- One participant per team that has joined, keyed by a server-generated id. consented_at is
-- updated on every join (the player ticked the consent box again).
CREATE TABLE participants (
    id           UUID        PRIMARY KEY,
    session      UUID        NOT NULL,
    team         TEXT        NOT NULL,
    joined_at    TIMESTAMPTZ NOT NULL,
    consented_at TIMESTAMPTZ NOT NULL,
    UNIQUE (session, team)
);

-- When the moderator started and finished each session. No row means not started. The file's
-- start-time/end-time are only the planned window; these are the session's real run.
CREATE TABLE session_runs (
    session    UUID        PRIMARY KEY,
    started_at TIMESTAMPTZ NULL,
    stopped_at TIMESTAMPTZ NULL
);

-- Teams that tried to play outside the session: a join or arrive refused, or a photo recorded
-- as failed, because the session hadn't started or had stopped. For the moderator overview.
-- Only the newest 500 per session are kept. Never the join code: the team it belongs to.
CREATE TABLE blocked_attempts (
    id      BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session UUID        NOT NULL,
    team    TEXT        NOT NULL,
    action  TEXT        NOT NULL CHECK (action IN ('join', 'arrive', 'photo')),
    code    TEXT        NOT NULL CHECK (code IN ('session_not_started', 'session_stopped')),
    at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX blocked_attempts_newest ON blocked_attempts (session, at DESC, id DESC);

COMMIT;
