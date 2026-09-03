-- Local-only, privacy-safe projection and delivery ledger for a Feishu Base read model.
-- No Base token, URL, question content, answer, explanation, OCR text, raw response,
-- or local path is stored by this schema.

CREATE TABLE base_projection_runs (
    projection_run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (
      length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    contract_version TEXT NOT NULL CHECK (contract_version = 'feishu-base-operational-v1'),
    target_fingerprint_sha256 TEXT NOT NULL CHECK (
      length(target_fingerprint_sha256) = 64
      AND target_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    projection_name TEXT NOT NULL CHECK (projection_name IN (
      'student_overview',
      'period_metrics',
      'knowledge_performance',
      'retest_summary',
      'data_quality',
      'generation_runs',
      'teacher_policy_correction_inbox'
    )),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    data_as_of TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (
      length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    record_count INTEGER NOT NULL CHECK (record_count > 0),
    publisher TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'staged' CHECK (status IN (
      'staged', 'publishing', 'retryable_failed', 'completed', 'permanent_failed'
    )),
    last_failure_category TEXT CHECK (last_failure_category IS NULL OR last_failure_category IN (
      'transport', 'rate_limited', 'authentication', 'permission', 'validation',
      'conflict', 'remote_unavailable', 'unknown'
    )),
    last_failure_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (student_id, subject_code)
      REFERENCES student_subjects(student_id, subject_code),
    UNIQUE(projection_run_id, projection_name, student_id, subject_code),
    CHECK (
      (status = 'staged' AND started_at IS NULL AND completed_at IS NULL)
      OR
      (status IN ('publishing', 'retryable_failed')
       AND started_at IS NOT NULL AND completed_at IS NULL)
      OR
      (status IN ('completed', 'permanent_failed')
       AND started_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX idx_base_projection_runs_student_status
ON base_projection_runs(student_id, subject_code, status, updated_at DESC);

CREATE TABLE base_projection_outbox (
    outbox_id TEXT PRIMARY KEY,
    projection_run_id TEXT NOT NULL REFERENCES base_projection_runs(projection_run_id),
    record_no INTEGER NOT NULL CHECK (record_no > 0),
    projection_name TEXT NOT NULL CHECK (projection_name IN (
      'student_overview',
      'period_metrics',
      'knowledge_performance',
      'retest_summary',
      'data_quality',
      'generation_runs',
      'teacher_policy_correction_inbox'
    )),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    projection_upsert_key TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (
      json_valid(payload_json) AND json_type(payload_json) = 'object'
    ),
    payload_sha256 TEXT NOT NULL CHECK (
      length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
      'pending', 'inflight', 'retryable_failed', 'succeeded',
      'permanent_failed', 'skipped_unchanged'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_claimed_at TEXT,
    last_failure_category TEXT CHECK (last_failure_category IS NULL OR last_failure_category IN (
      'transport', 'rate_limited', 'authentication', 'permission', 'validation',
      'conflict', 'remote_unavailable', 'unknown'
    )),
    last_failure_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (student_id, subject_code)
      REFERENCES student_subjects(student_id, subject_code),
    UNIQUE(projection_run_id, record_no),
    UNIQUE(projection_run_id, projection_upsert_key),
    CHECK (record_no > 0),
    CHECK (
      (status = 'pending' AND attempt_count = 0 AND next_attempt_at IS NULL AND completed_at IS NULL)
      OR
      (status = 'inflight' AND attempt_count > 0 AND next_attempt_at IS NULL AND completed_at IS NULL)
      OR
      (status = 'retryable_failed' AND attempt_count > 0 AND next_attempt_at IS NOT NULL AND completed_at IS NULL)
      OR
      (status = 'succeeded' AND attempt_count > 0 AND next_attempt_at IS NULL AND completed_at IS NOT NULL)
      OR
      (status = 'permanent_failed' AND attempt_count > 0 AND next_attempt_at IS NULL AND completed_at IS NOT NULL)
      OR
      (status = 'skipped_unchanged' AND attempt_count = 0 AND next_attempt_at IS NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX idx_base_projection_outbox_ready
ON base_projection_outbox(projection_run_id, status, next_attempt_at, record_no);

CREATE INDEX idx_base_projection_outbox_upsert_key
ON base_projection_outbox(projection_upsert_key, created_at DESC);

CREATE UNIQUE INDEX uq_base_projection_outbox_active_upsert_key
ON base_projection_outbox(projection_upsert_key)
WHERE status IN ('pending', 'inflight', 'retryable_failed');

CREATE TABLE base_projection_delivery_attempts (
    delivery_attempt_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    result_sha256 TEXT NOT NULL CHECK (
      length(result_sha256) = 64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    outbox_id TEXT NOT NULL REFERENCES base_projection_outbox(outbox_id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    outcome TEXT NOT NULL CHECK (outcome IN (
      'succeeded', 'retryable_failed', 'permanent_failed'
    )),
    remote_record_id TEXT,
    readback_payload_sha256 TEXT CHECK (
      readback_payload_sha256 IS NULL
      OR (length(readback_payload_sha256) = 64
          AND readback_payload_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    failure_category TEXT CHECK (failure_category IS NULL OR failure_category IN (
      'transport', 'rate_limited', 'authentication', 'permission', 'validation',
      'conflict', 'remote_unavailable', 'unknown'
    )),
    failure_code TEXT,
    retry_at TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE(outbox_id, attempt_no),
    CHECK (
      (outcome = 'succeeded'
       AND remote_record_id IS NOT NULL
       AND readback_payload_sha256 IS NOT NULL
       AND failure_category IS NULL AND failure_code IS NULL AND retry_at IS NULL)
      OR
      (outcome = 'retryable_failed'
       AND remote_record_id IS NULL
       AND readback_payload_sha256 IS NULL
       AND failure_category IS NOT NULL AND failure_code IS NOT NULL AND retry_at IS NOT NULL)
      OR
      (outcome = 'permanent_failed'
       AND remote_record_id IS NULL
       AND readback_payload_sha256 IS NULL
       AND failure_category IS NOT NULL AND failure_code IS NOT NULL AND retry_at IS NULL)
    )
);

CREATE INDEX idx_base_projection_delivery_outbox
ON base_projection_delivery_attempts(outbox_id, attempt_no);

CREATE TABLE base_projection_state (
    projection_upsert_key TEXT PRIMARY KEY,
    projection_name TEXT NOT NULL CHECK (projection_name IN (
      'student_overview',
      'period_metrics',
      'knowledge_performance',
      'retest_summary',
      'data_quality',
      'generation_runs',
      'teacher_policy_correction_inbox'
    )),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    remote_record_id TEXT NOT NULL,
    last_payload_sha256 TEXT NOT NULL CHECK (
      length(last_payload_sha256) = 64
      AND last_payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    last_outbox_id TEXT NOT NULL UNIQUE REFERENCES base_projection_outbox(outbox_id),
    last_delivery_attempt_id TEXT NOT NULL UNIQUE
      REFERENCES base_projection_delivery_attempts(delivery_attempt_id),
    first_published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (student_id, subject_code)
      REFERENCES student_subjects(student_id, subject_code)
);

CREATE INDEX idx_base_projection_state_student
ON base_projection_state(student_id, subject_code, projection_name);

CREATE TRIGGER base_projection_runs_initial_status
BEFORE INSERT ON base_projection_runs
WHEN NEW.status <> 'staged'
BEGIN
    SELECT RAISE(ABORT, 'base projection runs must start staged');
END;

CREATE TRIGGER base_projection_runs_active_owner
BEFORE INSERT ON base_projection_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM students s
    JOIN student_subjects ss ON ss.student_id = s.student_id
    JOIN subjects sub ON sub.subject_code = ss.subject_code
    WHERE s.student_id = NEW.student_id
      AND ss.subject_code = NEW.subject_code
      AND s.active = 1 AND ss.active = 1 AND sub.active = 1
)
BEGIN
    SELECT RAISE(ABORT, 'base projection run requires an active student subject');
END;

CREATE TRIGGER base_projection_runs_identity_immutable
BEFORE UPDATE ON base_projection_runs
WHEN NEW.projection_run_id IS NOT OLD.projection_run_id
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.request_sha256 IS NOT OLD.request_sha256
  OR NEW.contract_version IS NOT OLD.contract_version
  OR NEW.target_fingerprint_sha256 IS NOT OLD.target_fingerprint_sha256
  OR NEW.projection_name IS NOT OLD.projection_name
  OR NEW.student_id IS NOT OLD.student_id
  OR NEW.subject_code IS NOT OLD.subject_code
  OR NEW.data_as_of IS NOT OLD.data_as_of
  OR NEW.payload_sha256 IS NOT OLD.payload_sha256
  OR NEW.record_count IS NOT OLD.record_count
  OR NEW.publisher IS NOT OLD.publisher
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'base projection run identity is immutable');
END;

CREATE TRIGGER base_projection_runs_status_transition
BEFORE UPDATE OF status ON base_projection_runs
WHEN NOT (
    NEW.status = OLD.status
    OR (OLD.status = 'staged' AND NEW.status IN ('publishing', 'completed'))
    OR (OLD.status = 'publishing' AND NEW.status IN (
          'retryable_failed', 'completed', 'permanent_failed'
       ))
    OR (OLD.status = 'retryable_failed' AND NEW.status IN (
          'publishing', 'completed', 'permanent_failed'
       ))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid base projection run status transition');
END;

CREATE TRIGGER base_projection_runs_completed_gate
BEFORE UPDATE OF status ON base_projection_runs
WHEN NEW.status = 'completed'
 AND (
      (SELECT COUNT(*) FROM base_projection_outbox o
       WHERE o.projection_run_id = NEW.projection_run_id) <> NEW.record_count
      OR EXISTS (
          SELECT 1 FROM base_projection_outbox o
          WHERE o.projection_run_id = NEW.projection_run_id
            AND o.status NOT IN ('succeeded', 'skipped_unchanged')
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'completed projection run requires every record delivered or unchanged');
END;

CREATE TRIGGER base_projection_runs_retryable_gate
BEFORE UPDATE OF status ON base_projection_runs
WHEN NEW.status = 'retryable_failed'
 AND (
      NOT EXISTS (
          SELECT 1 FROM base_projection_outbox o
          WHERE o.projection_run_id = NEW.projection_run_id
            AND o.status = 'retryable_failed'
      )
      OR EXISTS (
          SELECT 1 FROM base_projection_outbox o
          WHERE o.projection_run_id = NEW.projection_run_id
            AND o.status IN ('pending', 'inflight')
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'retryable projection run requires only deferred retry work');
END;

CREATE TRIGGER base_projection_runs_permanent_failure_gate
BEFORE UPDATE OF status ON base_projection_runs
WHEN NEW.status = 'permanent_failed'
 AND (
      (SELECT COUNT(*) FROM base_projection_outbox o
       WHERE o.projection_run_id = NEW.projection_run_id) <> NEW.record_count
      OR NOT EXISTS (
          SELECT 1 FROM base_projection_outbox o
          WHERE o.projection_run_id = NEW.projection_run_id
            AND o.status = 'permanent_failed'
      )
      OR EXISTS (
          SELECT 1 FROM base_projection_outbox o
          WHERE o.projection_run_id = NEW.projection_run_id
            AND o.status IN ('pending', 'inflight', 'retryable_failed')
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'permanent failure requires a terminal outbox');
END;

CREATE TRIGGER base_projection_runs_append_only_delete
BEFORE DELETE ON base_projection_runs
BEGIN
    SELECT RAISE(ABORT, 'base projection runs cannot be deleted');
END;

CREATE TRIGGER base_projection_outbox_run_guard
BEFORE INSERT ON base_projection_outbox
WHEN NOT EXISTS (
    SELECT 1 FROM base_projection_runs r
    WHERE r.projection_run_id = NEW.projection_run_id
      AND r.projection_name = NEW.projection_name
      AND r.student_id = NEW.student_id
      AND r.subject_code = NEW.subject_code
      AND r.status = 'staged'
      AND NEW.record_no <= r.record_count
)
BEGIN
    SELECT RAISE(ABORT, 'projection outbox record does not match its staged run');
END;

CREATE TRIGGER base_projection_outbox_initial_state
BEFORE INSERT ON base_projection_outbox
WHEN NEW.status NOT IN ('pending', 'skipped_unchanged') OR NEW.attempt_count <> 0
BEGIN
    SELECT RAISE(ABORT, 'projection outbox records must start pending or unchanged');
END;

CREATE TRIGGER base_projection_outbox_common_payload
BEFORE INSERT ON base_projection_outbox
WHEN json_extract(NEW.payload_json, '$.projection_upsert_key') IS NOT NEW.projection_upsert_key
  OR json_extract(NEW.payload_json, '$.projection_name') IS NOT NEW.projection_name
  OR json_extract(NEW.payload_json, '$.projection_contract_version') IS NOT 'feishu-base-operational-v1'
  OR json_extract(NEW.payload_json, '$.student_id') IS NOT NEW.student_id
  OR json_extract(NEW.payload_json, '$.subject_code') IS NOT NEW.subject_code
  OR json_type(NEW.payload_json, '$.data_as_of') IS NOT 'text'
  OR json_type(NEW.payload_json, '$.metric_version') IS NOT 'text'
  OR json_extract(NEW.payload_json, '$.freshness_status') NOT IN (
       'FRESH', 'DELAYED', 'STALE', 'FAILED'
     )
  OR json_type(NEW.payload_json, '$.sample_size') IS NOT 'integer'
  OR json_extract(NEW.payload_json, '$.sample_size') < 0
BEGIN
    SELECT RAISE(ABORT, 'projection payload metadata does not match its outbox record');
END;

CREATE TRIGGER base_projection_outbox_student_overview_fields
BEFORE INSERT ON base_projection_outbox
WHEN NEW.projection_name = 'student_overview'
 AND (
      (SELECT COUNT(*) FROM json_each(NEW.payload_json)) <> 16
      OR EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json)
          WHERE key NOT IN (
            'projection_upsert_key','projection_name','projection_contract_version',
            'student_id','subject_code','data_as_of','metric_version',
            'freshness_status','sample_size','is_active','session_count',
            'attempt_count','scored_attempt_count','accuracy','review_due_count',
            'last_activity_at'
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'student overview payload violates its field whitelist');
END;

CREATE TRIGGER base_projection_outbox_period_metrics_fields
BEFORE INSERT ON base_projection_outbox
WHEN NEW.projection_name = 'period_metrics'
 AND (
      (SELECT COUNT(*) FROM json_each(NEW.payload_json)) <> 19
      OR EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json)
          WHERE key NOT IN (
            'projection_upsert_key','projection_name','projection_contract_version',
            'student_id','subject_code','data_as_of','metric_version',
            'freshness_status','sample_size','period_start','period_end',
            'assessment_kind','reporting_series','score_scale_max','attempt_count',
            'scored_attempt_count','accuracy','average_score_rate','calibration_count'
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'period metrics payload violates its field whitelist');
END;

CREATE TRIGGER base_projection_outbox_knowledge_performance_fields
BEFORE INSERT ON base_projection_outbox
WHEN NEW.projection_name = 'knowledge_performance'
 AND (
      (SELECT COUNT(*) FROM json_each(NEW.payload_json)) <> 15
      OR EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json)
          WHERE key NOT IN (
            'projection_upsert_key','projection_name','projection_contract_version',
            'student_id','subject_code','data_as_of','metric_version',
            'freshness_status','sample_size','knowledge_code','attempt_count',
            'distinct_item_count','weighted_accuracy','mastery_status','last_evidence_at'
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'knowledge performance payload violates its field whitelist');
END;

CREATE TRIGGER base_projection_outbox_retest_summary_fields
BEFORE INSERT ON base_projection_outbox
WHEN NEW.projection_name = 'retest_summary'
 AND (
      (SELECT COUNT(*) FROM json_each(NEW.payload_json)) <> 17
      OR EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json)
          WHERE key NOT IN (
            'projection_upsert_key','projection_name','projection_contract_version',
            'student_id','subject_code','data_as_of','metric_version',
            'freshness_status','sample_size','window_start','window_end','due_count',
            'completed_count','recovered_count','still_incorrect_count','overdue_count',
            'next_due_at'
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'retest summary payload violates its field whitelist');
END;

CREATE TRIGGER base_projection_outbox_data_quality_fields
BEFORE INSERT ON base_projection_outbox
WHEN NEW.projection_name = 'data_quality'
 AND (
      (SELECT COUNT(*) FROM json_each(NEW.payload_json)) <> 15
      OR EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json)
          WHERE key NOT IN (
            'projection_upsert_key','projection_name','projection_contract_version',
            'student_id','subject_code','data_as_of','metric_version',
            'freshness_status','sample_size','check_scope','total_check_count',
            'failed_check_count','critical_failure_count','trust_status','checked_at'
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'data quality payload violates its field whitelist');
END;

CREATE TRIGGER base_projection_outbox_generation_runs_fields
BEFORE INSERT ON base_projection_outbox
WHEN NEW.projection_name = 'generation_runs'
 AND (
      (SELECT COUNT(*) FROM json_each(NEW.payload_json)) <> 16
      OR EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json)
          WHERE key NOT IN (
            'projection_upsert_key','projection_name','projection_contract_version',
            'student_id','subject_code','data_as_of','metric_version',
            'freshness_status','sample_size','generation_id','artifact_type','run_status',
            'is_stale','created_at','started_at','completed_at'
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'generation run payload violates its field whitelist');
END;

CREATE TRIGGER base_projection_outbox_teacher_inbox_fields
BEFORE INSERT ON base_projection_outbox
WHEN NEW.projection_name = 'teacher_policy_correction_inbox'
 AND (
      (SELECT COUNT(*) FROM json_each(NEW.payload_json)) <> 19
      OR EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json)
          WHERE key NOT IN (
            'projection_upsert_key','projection_name','projection_contract_version',
            'student_id','subject_code','data_as_of','metric_version',
            'freshness_status','sample_size','inbox_item_id','inbox_kind','review_status',
            'priority','reason_code','source_entity_type','source_entity_id','opened_at',
            'due_at','resolved_at'
          )
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'teacher inbox payload violates its field whitelist');
END;

CREATE TRIGGER base_projection_outbox_unchanged_guard
BEFORE INSERT ON base_projection_outbox
WHEN NEW.status = 'skipped_unchanged'
 AND NOT EXISTS (
    SELECT 1 FROM base_projection_state s
    WHERE s.projection_upsert_key = NEW.projection_upsert_key
      AND s.projection_name = NEW.projection_name
      AND s.student_id = NEW.student_id
      AND s.subject_code = NEW.subject_code
      AND s.last_payload_sha256 = NEW.payload_sha256
 )
BEGIN
    SELECT RAISE(ABORT, 'unchanged projection record requires matching published state');
END;

CREATE TRIGGER base_projection_outbox_identity_immutable
BEFORE UPDATE ON base_projection_outbox
WHEN NEW.outbox_id IS NOT OLD.outbox_id
  OR NEW.projection_run_id IS NOT OLD.projection_run_id
  OR NEW.record_no IS NOT OLD.record_no
  OR NEW.projection_name IS NOT OLD.projection_name
  OR NEW.student_id IS NOT OLD.student_id
  OR NEW.subject_code IS NOT OLD.subject_code
  OR NEW.projection_upsert_key IS NOT OLD.projection_upsert_key
  OR NEW.payload_json IS NOT OLD.payload_json
  OR NEW.payload_sha256 IS NOT OLD.payload_sha256
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'projection outbox identity and payload are immutable');
END;

CREATE TRIGGER base_projection_outbox_status_transition
BEFORE UPDATE OF status ON base_projection_outbox
WHEN NOT (
    NEW.status = OLD.status
    OR (OLD.status IN ('pending', 'retryable_failed') AND NEW.status = 'inflight')
    OR (OLD.status = 'inflight' AND NEW.status IN (
          'succeeded', 'retryable_failed', 'permanent_failed'
       ))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid projection outbox status transition');
END;

CREATE TRIGGER base_projection_outbox_append_only_delete
BEFORE DELETE ON base_projection_outbox
BEGIN
    SELECT RAISE(ABORT, 'projection outbox records cannot be deleted');
END;

CREATE TRIGGER base_projection_delivery_attempt_guard
BEFORE INSERT ON base_projection_delivery_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM base_projection_outbox o
    WHERE o.outbox_id = NEW.outbox_id
      AND o.status = 'inflight'
      AND o.attempt_count = NEW.attempt_no
      AND (
        NEW.outcome <> 'succeeded'
        OR (
          NEW.readback_payload_sha256 = o.payload_sha256
          AND NOT EXISTS (
              SELECT 1 FROM base_projection_state s
              WHERE s.projection_upsert_key = o.projection_upsert_key
                AND s.remote_record_id <> NEW.remote_record_id
          )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'delivery result must match the active outbox attempt');
END;

CREATE TRIGGER base_projection_delivery_attempts_append_only_update
BEFORE UPDATE ON base_projection_delivery_attempts
BEGIN
    SELECT RAISE(ABORT, 'projection delivery attempts are append-only');
END;

CREATE TRIGGER base_projection_delivery_attempts_append_only_delete
BEFORE DELETE ON base_projection_delivery_attempts
BEGIN
    SELECT RAISE(ABORT, 'projection delivery attempts are append-only');
END;

CREATE TRIGGER base_projection_state_consistency_insert
BEFORE INSERT ON base_projection_state
WHEN NOT EXISTS (
    SELECT 1
    FROM base_projection_outbox o
    JOIN base_projection_delivery_attempts a
      ON a.outbox_id = o.outbox_id
    WHERE o.outbox_id = NEW.last_outbox_id
      AND a.delivery_attempt_id = NEW.last_delivery_attempt_id
      AND a.outcome = 'succeeded'
      AND a.remote_record_id = NEW.remote_record_id
      AND a.readback_payload_sha256 = o.payload_sha256
      AND o.status = 'succeeded'
      AND o.projection_upsert_key = NEW.projection_upsert_key
      AND o.projection_name = NEW.projection_name
      AND o.student_id = NEW.student_id
      AND o.subject_code = NEW.subject_code
      AND o.payload_sha256 = NEW.last_payload_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'projection state requires an audited successful delivery');
END;

CREATE TRIGGER base_projection_state_consistency_update
BEFORE UPDATE ON base_projection_state
WHEN NEW.projection_upsert_key IS NOT OLD.projection_upsert_key
  OR NEW.projection_name IS NOT OLD.projection_name
  OR NEW.student_id IS NOT OLD.student_id
  OR NEW.subject_code IS NOT OLD.subject_code
  OR NEW.remote_record_id IS NOT OLD.remote_record_id
  OR NEW.first_published_at IS NOT OLD.first_published_at
  OR NOT EXISTS (
      SELECT 1
      FROM base_projection_outbox o
      JOIN base_projection_delivery_attempts a
        ON a.outbox_id = o.outbox_id
      WHERE o.outbox_id = NEW.last_outbox_id
        AND a.delivery_attempt_id = NEW.last_delivery_attempt_id
        AND a.outcome = 'succeeded'
        AND a.remote_record_id = NEW.remote_record_id
        AND a.readback_payload_sha256 = o.payload_sha256
        AND o.status = 'succeeded'
        AND o.projection_upsert_key = NEW.projection_upsert_key
        AND o.projection_name = NEW.projection_name
        AND o.student_id = NEW.student_id
        AND o.subject_code = NEW.subject_code
        AND o.payload_sha256 = NEW.last_payload_sha256
  )
BEGIN
    SELECT RAISE(ABORT, 'projection state update must preserve identity and cite a successful delivery');
END;

CREATE TRIGGER base_projection_state_append_only_delete
BEFORE DELETE ON base_projection_state
BEGIN
    SELECT RAISE(ABORT, 'projection state cannot be deleted');
END;
