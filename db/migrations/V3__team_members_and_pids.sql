-- V3: team members by pid, and who owns each photo.
--
-- A team keeps one `participant` id shared by every phone. With accounts each person joins as
-- themselves and consents for themselves: `team_members` holds one row per person per session,
-- so nobody is in two teams of one session. The `pid` is the users directory's person id, with
-- no foreign key to it (the directory is kept apart from the game). Every column added here is
-- nullable: with accounts off, nothing fills them.
--
-- Never edit this file once it's on main: change the schema in a new migration.

SET lock_timeout = '5s';

-- ruled_submissions selects `s.*`, which PostgreSQL expands once, when the view is created:
-- it's recreated below so it carries the new column.
DROP VIEW ruled_submissions;

CREATE TABLE team_members (
    session          UUID        NOT NULL,
    participant      UUID        NOT NULL,
    pid              UUID        NOT NULL,
    joined_at        TIMESTAMPTZ NOT NULL,
    photo_consent_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT team_members_pkey PRIMARY KEY (session, pid),
    CONSTRAINT team_members_participant_fkey FOREIGN KEY (participant)
        REFERENCES participants (id) ON DELETE CASCADE
);
CREATE INDEX team_members_by_participant_idx ON team_members (participant);
CREATE INDEX team_members_by_pid_idx ON team_members (pid);

-- The caller, when there was one.
ALTER TABLE submissions ADD COLUMN pid UUID;
ALTER TABLE arrivals ADD COLUMN pid UUID;
-- Who made them (filled in from #119).
ALTER TABLE rulings ADD COLUMN pid UUID;
ALTER TABLE hunt_drafts ADD COLUMN pid UUID;

CREATE VIEW ruled_submissions AS
SELECT
    s.*,
    latest.ruling,
    latest.note,
    latest.ruled_at,
    CASE latest.ruling WHEN 'approve' THEN 'pass' WHEN 'reject' THEN 'failed' ELSE s.verdict END
        AS effective_verdict,
    s.verdict IN ('pass', 'pending') OR EXISTS (
        SELECT 1 FROM rulings approved
        WHERE approved.submission_id = s.id AND approved.ruling = 'approve'
    ) AS completes
FROM submissions s
LEFT JOIN LATERAL (
    SELECT ruling, note, ruled_at FROM rulings
    WHERE rulings.submission_id = s.id
    ORDER BY rulings.id DESC
    LIMIT 1
) latest ON TRUE;
