CREATE TABLE students (
    student_id TEXT PRIMARY KEY,
    display_name TEXT,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    target_retention REAL NOT NULL DEFAULT 0.90 CHECK (target_retention > 0 AND target_retention < 1),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE ingest_events (
    ingest_event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    source_thread TEXT NOT NULL CHECK (source_thread IN ('engineering', 'dictation', 'courseware', 'manual', 'migration')),
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT,
    status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'reverted', 'failed')),
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    backup_path TEXT,
    imported_at TEXT NOT NULL,
    reverted_at TEXT,
    note TEXT
);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    material_date TEXT,
    private_path TEXT,
    external_uri TEXT,
    content_sha256 TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    created_by_event_id TEXT REFERENCES ingest_events(ingest_event_id),
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'superseded', 'voided')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE learning_sessions (
    session_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    source_thread TEXT NOT NULL CHECK (source_thread IN ('engineering', 'dictation', 'courseware', 'manual', 'migration')),
    session_type TEXT NOT NULL,
    title TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    planned_item_count INTEGER,
    completed_item_count INTEGER,
    note TEXT,
    created_by_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'superseded', 'voided')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE session_observations (
    observation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES learning_sessions(session_id),
    observation_type TEXT NOT NULL,
    observation_text TEXT NOT NULL,
    evidence_level TEXT NOT NULL DEFAULT 'session_only' CHECK (evidence_level IN ('session_only', 'item_linked', 'teacher_confirmed')),
    created_by_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'voided')),
    created_at TEXT NOT NULL
);

CREATE TABLE session_progress (
    progress_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES learning_sessions(session_id),
    content_label TEXT NOT NULL,
    external_namespace TEXT,
    external_id TEXT,
    progress_status TEXT NOT NULL CHECK (progress_status IN ('planned', 'in_progress', 'completed', 'not_started', 'skipped')),
    completed_count INTEGER,
    total_count INTEGER,
    note TEXT,
    created_by_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'voided')),
    created_at TEXT NOT NULL,
    UNIQUE(session_id, content_label, external_namespace, external_id)
);

CREATE TABLE content_items (
    item_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    item_type TEXT NOT NULL,
    prompt_snapshot TEXT,
    answer_snapshot TEXT,
    direction TEXT,
    response_mode TEXT NOT NULL DEFAULT 'mixed' CHECK (response_mode IN ('active_recall', 'recognition', 'production', 'mixed', 'unknown')),
    difficulty_label TEXT,
    difficulty_weight REAL NOT NULL DEFAULT 1.0 CHECK (difficulty_weight > 0),
    source_validation_status TEXT NOT NULL DEFAULT 'unverified',
    legacy_ref TEXT,
    metadata_json TEXT,
    content_hash TEXT,
    created_by_event_id TEXT REFERENCES ingest_events(ingest_event_id),
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'superseded', 'voided')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE external_references (
    external_reference_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES content_items(item_id),
    namespace TEXT NOT NULL,
    reference_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    external_parent_id TEXT,
    source_validation_status TEXT NOT NULL DEFAULT 'unverified',
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(namespace, reference_type, external_id)
);

CREATE TABLE knowledge_points (
    knowledge_point_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    parent_id TEXT REFERENCES knowledge_points(knowledge_point_id),
    domain TEXT NOT NULL,
    name_en TEXT NOT NULL,
    name_cn TEXT NOT NULL,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE item_knowledge_map (
    item_id TEXT NOT NULL REFERENCES content_items(item_id),
    knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(knowledge_point_id),
    mapping_role TEXT NOT NULL DEFAULT 'primary' CHECK (mapping_role IN ('primary', 'secondary', 'prerequisite', 'inferred')),
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    evidence_source TEXT,
    validation_status TEXT NOT NULL DEFAULT 'unverified',
    PRIMARY KEY(item_id, knowledge_point_id)
);

CREATE TABLE error_types (
    error_type_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    parent_id TEXT REFERENCES error_types(error_type_id),
    label_en TEXT NOT NULL,
    label_cn TEXT NOT NULL,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE error_type_aliases (
    alias_normalized TEXT PRIMARY KEY,
    raw_alias TEXT NOT NULL,
    error_type_id TEXT NOT NULL REFERENCES error_types(error_type_id),
    source_system TEXT
);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    ingest_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    session_id TEXT NOT NULL REFERENCES learning_sessions(session_id),
    item_id TEXT NOT NULL REFERENCES content_items(item_id),
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    attempted_at TEXT NOT NULL,
    student_answer TEXT,
    standard_answer_snapshot TEXT,
    answer_capture_status TEXT NOT NULL CHECK (answer_capture_status IN ('captured', 'captured_blank', 'not_captured', 'unknown_legacy')),
    attempt_phase TEXT NOT NULL DEFAULT 'first' CHECK (attempt_phase IN ('first', 'review', 'practice', 'exam')),
    response_mode TEXT NOT NULL DEFAULT 'unknown' CHECK (response_mode IN ('active_recall', 'recognition', 'production', 'mixed', 'unknown')),
    validation_status TEXT NOT NULL DEFAULT 'unverified' CHECK (validation_status IN ('verified', 'source_checked', 'unverified', 'needs_check')),
    teacher_note TEXT,
    source_material_ref TEXT,
    supersedes_attempt_id TEXT REFERENCES attempts(attempt_id),
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'superseded', 'voided')),
    created_at TEXT NOT NULL
);

CREATE TABLE evaluations (
    evaluation_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    revision_no INTEGER NOT NULL DEFAULT 1,
    result TEXT NOT NULL CHECK (result IN ('correct', 'partial', 'wrong', 'needs_check')),
    score REAL,
    max_score REAL,
    evaluated_by TEXT NOT NULL DEFAULT 'teacher',
    is_human_corrected INTEGER NOT NULL DEFAULT 0 CHECK (is_human_corrected IN (0, 1)),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    supersedes_evaluation_id TEXT REFERENCES evaluations(evaluation_id),
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id, revision_no)
);

CREATE UNIQUE INDEX uq_evaluations_current
ON evaluations(attempt_id) WHERE is_current = 1;

CREATE TABLE attempt_error_map (
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    error_type_id TEXT NOT NULL REFERENCES error_types(error_type_id),
    raw_error_type TEXT,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    note TEXT,
    PRIMARY KEY(attempt_id, error_type_id, raw_error_type)
);

CREATE TABLE review_state (
    student_id TEXT NOT NULL REFERENCES students(student_id),
    item_id TEXT NOT NULL REFERENCES content_items(item_id),
    state TEXT NOT NULL DEFAULT 'learning' CHECK (state IN ('new', 'learning', 'due', 'mastered', 'suspended')),
    due_at TEXT,
    interval_days INTEGER NOT NULL DEFAULT 0,
    stability REAL,
    difficulty REAL,
    repetitions INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_attempt_id TEXT REFERENCES attempts(attempt_id),
    last_result TEXT CHECK (last_result IS NULL OR last_result IN ('correct', 'partial', 'wrong', 'needs_check')),
    last_reviewed_at TEXT,
    scheduling_algorithm TEXT NOT NULL DEFAULT 'simple-v1',
    algorithm_version TEXT NOT NULL DEFAULT '1',
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(student_id, item_id)
);

CREATE TABLE review_tasks (
    review_task_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    item_id TEXT NOT NULL REFERENCES content_items(item_id),
    source_attempt_id TEXT REFERENCES attempts(attempt_id),
    reason_code TEXT NOT NULL,
    due_at TEXT NOT NULL,
    priority REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'completed', 'cancelled', 'voided')),
    created_by_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    completed_by_attempt_id TEXT REFERENCES attempts(attempt_id),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX uq_review_tasks_open
ON review_tasks(student_id, item_id) WHERE status = 'open';

CREATE TABLE mastery_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    as_of TEXT NOT NULL,
    window_days INTEGER,
    snapshot_json TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(student_id, as_of, window_days, algorithm_version)
);

CREATE TABLE ingest_event_rows (
    ingest_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('insert', 'link', 'supersede', 'void', 'skip')),
    before_json TEXT,
    after_json TEXT,
    PRIMARY KEY(ingest_event_id, entity_type, entity_id, action)
);

CREATE TABLE audit_log (
    audit_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    ingest_event_id TEXT REFERENCES ingest_events(ingest_event_id),
    before_json TEXT,
    after_json TEXT,
    reason TEXT
);

CREATE TABLE legacy_records (
    legacy_record_id TEXT PRIMARY KEY,
    source_system TEXT NOT NULL,
    record_type TEXT NOT NULL,
    legacy_key TEXT NOT NULL,
    target_entity_type TEXT,
    target_entity_id TEXT,
    raw_payload_json TEXT NOT NULL,
    migration_status TEXT NOT NULL CHECK (migration_status IN ('migrated', 'linked', 'retained_only', 'needs_check')),
    note TEXT,
    created_by_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    created_at TEXT NOT NULL,
    UNIQUE(source_system, record_type, legacy_key)
);

CREATE INDEX idx_attempts_student_time ON attempts(student_id, attempted_at);
CREATE INDEX idx_attempts_item_time ON attempts(item_id, attempted_at);
CREATE INDEX idx_attempts_session ON attempts(session_id);
CREATE INDEX idx_item_knowledge_knowledge ON item_knowledge_map(knowledge_point_id, item_id);
CREATE INDEX idx_review_tasks_due ON review_tasks(student_id, status, due_at);
CREATE INDEX idx_external_refs_lookup ON external_references(namespace, reference_type, external_id);
CREATE INDEX idx_ingest_status ON ingest_events(status, imported_at);

