CREATE TABLE agent_runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    source_thread TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_excerpt TEXT NOT NULL,
    intent TEXT NOT NULL,
    primary_capability TEXT NOT NULL,
    route_json TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN (
      'planned', 'in_progress', 'needs_input', 'completed', 'failed', 'cancelled'
    )),
    summary TEXT,
    result_ref TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_agent_runs_student_status
ON agent_runs(student_id, status, updated_at DESC);

CREATE INDEX idx_agent_runs_capability
ON agent_runs(primary_capability, updated_at DESC);

CREATE TABLE agent_run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    event_type TEXT NOT NULL CHECK (event_type IN (
      'planned', 'started', 'progress', 'needs_input', 'completed', 'failed', 'cancelled'
    )),
    capability_key TEXT,
    actor TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence_no)
);

CREATE INDEX idx_agent_run_events_run
ON agent_run_events(run_id, sequence_no);
