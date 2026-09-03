ALTER TABLE artifacts ADD COLUMN student_id TEXT REFERENCES students(student_id);

UPDATE artifacts
SET student_id = (
    SELECT MIN(ls.student_id)
    FROM learning_sessions ls
    WHERE ls.artifact_id = artifacts.artifact_id
)
WHERE student_id IS NULL
  AND 1 = (
    SELECT COUNT(DISTINCT ls.student_id)
    FROM learning_sessions ls
    WHERE ls.artifact_id = artifacts.artifact_id
  );

CREATE INDEX idx_artifacts_student_status
ON artifacts(student_id, record_status, created_at DESC);

CREATE TABLE artifact_generation_runs (
    generation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (
      status IN ('planned', 'in_progress', 'completed', 'failed', 'cancelled')
    ),
    source_snapshot_json TEXT NOT NULL,
    source_snapshot_sha256 TEXT NOT NULL,
    skill_name TEXT,
    skill_version TEXT,
    prompt_version TEXT,
    model_name TEXT,
    output_artifact_id TEXT REFERENCES artifacts(artifact_id),
    output_path TEXT,
    output_sha256 TEXT,
    summary TEXT,
    stale_reason TEXT,
    stale_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_artifact_generation_student_status
ON artifact_generation_runs(student_id, status, updated_at DESC);

CREATE TRIGGER agent_run_events_append_only_update
BEFORE UPDATE ON agent_run_events
BEGIN
    SELECT RAISE(ABORT, 'agent_run_events is append-only');
END;

CREATE TRIGGER agent_run_events_append_only_delete
BEFORE DELETE ON agent_run_events
BEGIN
    SELECT RAISE(ABORT, 'agent_run_events is append-only');
END;
