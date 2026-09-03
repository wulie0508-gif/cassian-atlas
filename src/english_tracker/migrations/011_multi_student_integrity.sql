-- Multi-student ownership is enforced in SQLite, not only in Python callers.

CREATE TRIGGER artifacts_require_student_insert
BEFORE INSERT ON artifacts
WHEN NEW.student_id IS NULL OR trim(NEW.student_id) = ''
BEGIN
    SELECT RAISE(ABORT, 'artifacts.student_id is required');
END;

CREATE TRIGGER artifacts_require_student_update
BEFORE UPDATE OF student_id ON artifacts
WHEN NEW.student_id IS NULL OR trim(NEW.student_id) = ''
BEGIN
    SELECT RAISE(ABORT, 'artifacts.student_id is required');
END;

CREATE TRIGGER learning_sessions_artifact_owner_insert
BEFORE INSERT ON learning_sessions
WHEN NEW.artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.artifact_id AND a.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'learning session artifact belongs to another student');
END;

CREATE TRIGGER learning_sessions_artifact_owner_update
BEFORE UPDATE OF student_id, artifact_id ON learning_sessions
WHEN NEW.artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.artifact_id AND a.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'learning session artifact belongs to another student');
END;

CREATE TRIGGER learning_sessions_attempt_owner_update
BEFORE UPDATE OF student_id ON learning_sessions
WHEN EXISTS (
    SELECT 1 FROM attempts a
    WHERE a.session_id = OLD.session_id AND a.student_id IS NOT NEW.student_id
)
BEGIN
    SELECT RAISE(ABORT, 'learning session student conflicts with an existing attempt');
END;

CREATE TRIGGER attempts_session_owner_insert
BEFORE INSERT ON attempts
WHEN NOT EXISTS (
    SELECT 1 FROM learning_sessions ls
    WHERE ls.session_id = NEW.session_id AND ls.student_id = NEW.student_id
)
BEGIN
    SELECT RAISE(ABORT, 'attempt session belongs to another student');
END;

CREATE TRIGGER attempts_session_owner_update
BEFORE UPDATE OF student_id, session_id ON attempts
WHEN NOT EXISTS (
    SELECT 1 FROM learning_sessions ls
    WHERE ls.session_id = NEW.session_id AND ls.student_id = NEW.student_id
)
BEGIN
    SELECT RAISE(ABORT, 'attempt session belongs to another student');
END;

CREATE TRIGGER attempts_artifact_owner_insert
BEFORE INSERT ON attempts
WHEN NEW.artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.artifact_id AND a.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'attempt artifact belongs to another student');
END;

CREATE TRIGGER attempts_artifact_owner_update
BEFORE UPDATE OF student_id, artifact_id ON attempts
WHEN NEW.artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.artifact_id AND a.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'attempt artifact belongs to another student');
END;

CREATE TRIGGER attempts_review_owner_update
BEFORE UPDATE OF student_id, item_id ON attempts
WHEN EXISTS (
        SELECT 1 FROM review_state rs
        WHERE rs.last_attempt_id = OLD.attempt_id
          AND (rs.student_id IS NOT NEW.student_id OR rs.item_id IS NOT NEW.item_id)
     )
  OR EXISTS (
        SELECT 1 FROM review_tasks rt
        WHERE (rt.source_attempt_id = OLD.attempt_id OR rt.completed_by_attempt_id = OLD.attempt_id)
          AND (rt.student_id IS NOT NEW.student_id OR rt.item_id IS NOT NEW.item_id)
     )
BEGIN
    SELECT RAISE(ABORT, 'attempt student or item conflicts with review records');
END;

CREATE TRIGGER review_state_attempt_owner_insert
BEFORE INSERT ON review_state
WHEN NEW.last_attempt_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM attempts a
     WHERE a.attempt_id = NEW.last_attempt_id
       AND a.student_id = NEW.student_id
       AND a.item_id = NEW.item_id
 )
BEGIN
    SELECT RAISE(ABORT, 'review state attempt belongs to another student or item');
END;

CREATE TRIGGER review_state_attempt_owner_update
BEFORE UPDATE OF student_id, item_id, last_attempt_id ON review_state
WHEN NEW.last_attempt_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM attempts a
     WHERE a.attempt_id = NEW.last_attempt_id
       AND a.student_id = NEW.student_id
       AND a.item_id = NEW.item_id
 )
BEGIN
    SELECT RAISE(ABORT, 'review state attempt belongs to another student or item');
END;

CREATE TRIGGER review_tasks_source_attempt_owner_insert
BEFORE INSERT ON review_tasks
WHEN NEW.source_attempt_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM attempts a
     WHERE a.attempt_id = NEW.source_attempt_id
       AND a.student_id = NEW.student_id
       AND a.item_id = NEW.item_id
 )
BEGIN
    SELECT RAISE(ABORT, 'review task source attempt belongs to another student or item');
END;

CREATE TRIGGER review_tasks_source_attempt_owner_update
BEFORE UPDATE OF student_id, item_id, source_attempt_id ON review_tasks
WHEN NEW.source_attempt_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM attempts a
     WHERE a.attempt_id = NEW.source_attempt_id
       AND a.student_id = NEW.student_id
       AND a.item_id = NEW.item_id
 )
BEGIN
    SELECT RAISE(ABORT, 'review task source attempt belongs to another student or item');
END;

CREATE TRIGGER review_tasks_completed_attempt_owner_insert
BEFORE INSERT ON review_tasks
WHEN NEW.completed_by_attempt_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM attempts a
     WHERE a.attempt_id = NEW.completed_by_attempt_id
       AND a.student_id = NEW.student_id
       AND a.item_id = NEW.item_id
 )
BEGIN
    SELECT RAISE(ABORT, 'review task completion attempt belongs to another student or item');
END;

CREATE TRIGGER review_tasks_completed_attempt_owner_update
BEFORE UPDATE OF student_id, item_id, completed_by_attempt_id ON review_tasks
WHEN NEW.completed_by_attempt_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM attempts a
     WHERE a.attempt_id = NEW.completed_by_attempt_id
       AND a.student_id = NEW.student_id
       AND a.item_id = NEW.item_id
 )
BEGIN
    SELECT RAISE(ABORT, 'review task completion attempt belongs to another student or item');
END;

CREATE TRIGGER generation_output_artifact_owner_insert
BEFORE INSERT ON artifact_generation_runs
WHEN NEW.output_artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.output_artifact_id AND a.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'generation output artifact belongs to another student');
END;

CREATE TRIGGER generation_output_artifact_owner_update
BEFORE UPDATE OF student_id, output_artifact_id ON artifact_generation_runs
WHEN NEW.output_artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.output_artifact_id AND a.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'generation output artifact belongs to another student');
END;

CREATE TRIGGER artifacts_linked_owner_update
BEFORE UPDATE OF student_id ON artifacts
WHEN EXISTS (
        SELECT 1 FROM learning_sessions ls
        WHERE ls.artifact_id = OLD.artifact_id AND ls.student_id IS NOT NEW.student_id
     )
  OR EXISTS (
        SELECT 1 FROM attempts a
        WHERE a.artifact_id = OLD.artifact_id AND a.student_id IS NOT NEW.student_id
     )
  OR EXISTS (
        SELECT 1 FROM artifact_generation_runs g
        WHERE g.output_artifact_id = OLD.artifact_id AND g.student_id IS NOT NEW.student_id
     )
BEGIN
    SELECT RAISE(ABORT, 'artifact student conflicts with linked learning records');
END;

-- Event producers may opt in immediately; legacy writers remain compatible while
-- the orchestration API begins supplying the key.
ALTER TABLE agent_run_events ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX uq_agent_run_events_idempotency
ON agent_run_events(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TRIGGER schema_migrations_checksum_required_insert
BEFORE INSERT ON schema_migrations
WHEN NEW.checksum_sha256 IS NULL OR trim(NEW.checksum_sha256) = ''
BEGIN
    SELECT RAISE(ABORT, 'schema migration checksum is required');
END;

CREATE TRIGGER schema_migrations_checksum_required_update
BEFORE UPDATE OF checksum_sha256 ON schema_migrations
WHEN NEW.checksum_sha256 IS NULL OR trim(NEW.checksum_sha256) = ''
BEGIN
    SELECT RAISE(ABORT, 'schema migration checksum is required');
END;
