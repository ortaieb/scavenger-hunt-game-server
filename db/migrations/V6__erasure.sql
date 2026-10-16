-- V6: erasure.
--
-- Erasing a person (the right to be forgotten) keeps their team's results but clears what
-- describes them in the photos they submitted: where they stood and the photo's fingerprint.
-- Those columns become nullable; nothing else about a submission changes. Dropping NOT NULL
-- changes no column of `ruled_submissions`, so the view stays as it is. Old code runs on this
-- schema: it never writes NULL there and never reads the location. It does read the fingerprint,
-- for the duplicate check (this build skips a NULL one), so after an erasure a rollback would
-- fail new photos in that person's session while it is still running.
--
-- An erasure also records whether the person asked not to be contacted again: then the
-- directory step adds a suppression entry before deleting their row.
--
-- Never edit this file once it's on main: change the schema in a new migration.

SET lock_timeout = '5s';

ALTER TABLE submissions ALTER COLUMN lat DROP NOT NULL;
ALTER TABLE submissions ALTER COLUMN long DROP NOT NULL;
ALTER TABLE submissions ALTER COLUMN distance_m DROP NOT NULL;
ALTER TABLE submissions ALTER COLUMN phash DROP NOT NULL;

ALTER TABLE directory.erasures ADD COLUMN do_not_contact BOOLEAN NOT NULL DEFAULT false;
