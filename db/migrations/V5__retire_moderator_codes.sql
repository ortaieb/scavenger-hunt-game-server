-- V5: retire the moderator codes.
--
-- Moderators are people now (session_moderators, by pid), so a published session no longer has a
-- moderator code: strip it from the stored documents (the model rejects unknown keys) and free
-- its row in session_codes. Old code reads the same rows: the key is optional in the document,
-- and a session without a moderator code is valid. The 'moderator' kind stays allowed by the
-- column's check until a later release contracts it.
--
-- Never edit this file once it's on main: change the schema in a new migration.

SET lock_timeout = '5s';

DELETE FROM session_codes WHERE kind = 'moderator';

UPDATE published_sessions SET document = document - 'moderator-code' WHERE document ? 'moderator-code';
