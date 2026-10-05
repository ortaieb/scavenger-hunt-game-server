-- The game server's tables, dropped (if they exist) and created from scratch.
--
-- DESTRUCTIVE: every submission, participant and arrival is deleted. Meant for development,
-- until schema changes are applied as versioned migrations.
--
-- Run it with the server's connection settings:  make db-reset
-- or with psql:                                   psql "$DATABASE_URL" -f src/game_server/schema.sql
--
-- One transaction: if any statement fails, the database is left as it was.

BEGIN;

DROP TABLE IF EXISTS blocked_attempts, session_runs, arrivals, participants, submissions CASCADE;

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
    -- The referee's report, for moderator audit and cost tracking; NULL when it wasn't
    -- consulted. referee_judgement describes the photo: server-side only.
    referee_status        TEXT,
    referee_model         TEXT,
    referee_error         TEXT,
    referee_judgement     JSONB,
    referee_input_tokens  INTEGER,
    referee_output_tokens INTEGER,
    referee_latency_ms    INTEGER,
    -- The team's active arrival at the checkpoint that the photo used; NULL when there was none.
    arrival_id            BIGINT           REFERENCES arrivals (id),
    UNIQUE (session, participant, checkpoint, attempt)
);
CREATE INDEX submissions_by_arrival ON submissions (arrival_id);

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
