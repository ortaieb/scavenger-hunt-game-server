-- V2: the users directory, and two database logins that keep it apart from the game.
--
-- Scavenger Hunt's own directory of people: who they are, how to reach them, what they've agreed
-- to and their platform role. Everything else refers to a person only by `pid`, a random UUID
-- the application makes (never derived from an email or a Clerk id). The directory lives in its
-- own schema, behind its own login, so the game can't read it by accident; erasing a person is
-- then deleting their directory rows. There are no foreign keys between the `directory` schema
-- and the game tables, in either direction.
--
-- Never edit this file once it's on main: change the schema in a new migration.

SET lock_timeout = '5s';

-- The two roles, without passwords: a migration never holds one. Once per environment, an
-- operator runs `ALTER ROLE … LOGIN PASSWORD …` (see the README's Deploying on Railway).
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'game_app') THEN
        CREATE ROLE game_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'directory_app') THEN
        CREATE ROLE directory_app NOLOGIN;
    END IF;
END
$$;

CREATE SCHEMA directory;

-- The only place a pid meets an identity.
CREATE TABLE directory.people (
    pid           UUID        NOT NULL,
    clerk_user_id TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'active',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT people_pkey PRIMARY KEY (pid),
    CONSTRAINT people_clerk_user_id_key UNIQUE (clerk_user_id),
    CONSTRAINT people_status_check CHECK (status IN ('active', 'erasing'))
);

-- home_area is a postcode district such as W4 or SW1A, never a full postcode.
CREATE TABLE directory.profiles (
    pid          UUID        NOT NULL,
    email        TEXT        NOT NULL,
    display_name TEXT,
    home_area    TEXT,
    locale       TEXT        NOT NULL DEFAULT 'en-GB',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT profiles_pkey PRIMARY KEY (pid),
    CONSTRAINT profiles_pid_fkey FOREIGN KEY (pid)
        REFERENCES directory.people (pid) ON DELETE CASCADE,
    CONSTRAINT profiles_home_area_check CHECK (home_area ~ '^[A-Z]{1,2}[0-9][0-9A-Z]?$')
);

-- One row per grant, so a withdrawal keeps its history.
CREATE TABLE directory.contact_consents (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY,
    pid             UUID        NOT NULL,
    purpose         TEXT        NOT NULL,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at    TIMESTAMPTZ,
    wording_version TEXT        NOT NULL,
    source          TEXT        NOT NULL,
    CONSTRAINT contact_consents_pkey PRIMARY KEY (id),
    CONSTRAINT contact_consents_pid_fkey FOREIGN KEY (pid)
        REFERENCES directory.people (pid) ON DELETE CASCADE,
    CONSTRAINT contact_consents_purpose_check CHECK (purpose IN ('upcoming_hunts')),
    CONSTRAINT contact_consents_source_check CHECK (source IN ('join', 'profile')),
    CONSTRAINT contact_consents_withdrawn_check CHECK (withdrawn_at IS NULL OR withdrawn_at >= granted_at)
);
-- At most one grant per person and purpose is in force at a time.
CREATE UNIQUE INDEX contact_consents_one_active_idx
    ON directory.contact_consents (pid, purpose) WHERE withdrawn_at IS NULL;

-- No row means the player role.
CREATE TABLE directory.role_grants (
    pid        UUID        NOT NULL,
    role       TEXT        NOT NULL,
    granted_by UUID,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT role_grants_pkey PRIMARY KEY (pid),
    CONSTRAINT role_grants_pid_fkey FOREIGN KEY (pid)
        REFERENCES directory.people (pid) ON DELETE CASCADE,
    CONSTRAINT role_grants_role_check CHECK (role IN ('organiser', 'admin'))
);

-- The audit trail of role changes.
CREATE TABLE directory.role_changes (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY,
    pid        UUID        NOT NULL,
    from_role  TEXT        NOT NULL,
    to_role    TEXT        NOT NULL,
    changed_by UUID,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT role_changes_pkey PRIMARY KEY (id),
    CONSTRAINT role_changes_pid_fkey FOREIGN KEY (pid)
        REFERENCES directory.people (pid) ON DELETE CASCADE,
    CONSTRAINT role_changes_from_role_check CHECK (from_role IN ('player', 'organiser', 'admin')),
    CONSTRAINT role_changes_to_role_check CHECK (to_role IN ('player', 'organiser', 'admin'))
);
CREATE INDEX role_changes_by_pid_idx ON directory.role_changes (pid);

-- Writes to Clerk, retried until they land.
CREATE TABLE directory.clerk_outbox (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY,
    pid             UUID        NOT NULL,
    payload         JSONB       NOT NULL,
    attempts        INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT clerk_outbox_pkey PRIMARY KEY (id),
    CONSTRAINT clerk_outbox_pid_fkey FOREIGN KEY (pid)
        REFERENCES directory.people (pid) ON DELETE CASCADE,
    CONSTRAINT clerk_outbox_attempts_check CHECK (attempts >= 0)
);
CREATE INDEX clerk_outbox_due_idx ON directory.clerk_outbox (next_attempt_at) WHERE done_at IS NULL;
CREATE INDEX clerk_outbox_by_pid_idx ON directory.clerk_outbox (pid);

-- Holds no identity, so no foreign key: it's evidence that an erasure happened, and the list to
-- replay after a restore from a backup.
CREATE TABLE directory.erasures (
    pid                UUID        NOT NULL,
    requested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source             TEXT        NOT NULL,
    game_done_at       TIMESTAMPTZ,
    directory_done_at  TIMESTAMPTZ,
    clerk_done_at      TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    CONSTRAINT erasures_pkey PRIMARY KEY (pid)
);

-- People who asked not to be contacted, by a keyed hash of their email, never the address.
CREATE TABLE directory.suppressions (
    email_hmac BYTEA       NOT NULL,
    reason     TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT suppressions_pkey PRIMARY KEY (email_hmac)
);

-- directory_app: everything in the directory schema (and in tables added to it later), nothing in
-- the game's.
GRANT USAGE ON SCHEMA directory TO directory_app;
GRANT ALL ON ALL TABLES IN SCHEMA directory TO directory_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA directory TO directory_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA directory GRANT ALL ON TABLES TO directory_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA directory GRANT ALL ON SEQUENCES TO directory_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM directory_app;

-- game_app: what the game needs on its tables (and on those added later), nothing in the
-- directory schema. It may read Flyway's history, for /health, but never write it.
GRANT USAGE ON SCHEMA public TO game_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO game_app;
REVOKE INSERT, UPDATE, DELETE ON flyway_schema_history FROM game_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO game_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO game_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO game_app;
REVOKE ALL ON SCHEMA directory FROM game_app;
