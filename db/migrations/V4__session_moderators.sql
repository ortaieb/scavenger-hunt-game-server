-- V4: session moderators by pid.
--
-- Moderating a session is a per-hunt role: each row lets one person (by the users directory's
-- pid, with no foreign key to it) moderate one session, alongside the session's moderator code.
-- Only the pid is stored, never the email it was added by.
--
-- Never edit this file once it's on main: change the schema in a new migration.

CREATE TABLE session_moderators (
    session  UUID        NOT NULL,
    pid      UUID        NOT NULL,
    added_by UUID,
    added_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT session_moderators_pkey PRIMARY KEY (session, pid)
);
CREATE INDEX session_moderators_by_pid_idx ON session_moderators (pid);
