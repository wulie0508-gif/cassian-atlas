-- Candidate transcriptions remain outside the learning-fact tables until one
-- complete teacher-reviewed batch is committed.

CREATE TABLE extraction_batches (
    extraction_batch_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    contract_version TEXT NOT NULL,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    session_id TEXT NOT NULL REFERENCES learning_sessions(session_id),
    title TEXT NOT NULL,
    source_thread TEXT NOT NULL CHECK (source_thread IN (
      'engineering', 'dictation', 'courseware', 'manual', 'migration'
    )),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
      'draft', 'extracting', 'pending_review', 'ready_to_commit',
      'committed', 'cancelled', 'failed'
    )),
    expected_item_count INTEGER NOT NULL CHECK (expected_item_count > 0),
    review_version INTEGER NOT NULL DEFAULT 1 CHECK (review_version >= 1),
    comparison_policy_version TEXT NOT NULL,
    commit_idempotency_key TEXT UNIQUE,
    commit_request_sha256 TEXT CHECK (
      commit_request_sha256 IS NULL OR length(commit_request_sha256) = 64
    ),
    committed_ingest_event_id TEXT UNIQUE REFERENCES ingest_events(ingest_event_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    CHECK (
      (commit_idempotency_key IS NULL AND commit_request_sha256 IS NULL)
      OR
      (commit_idempotency_key IS NOT NULL AND commit_request_sha256 IS NOT NULL)
    ),
    CHECK (
      status <> 'committed'
      OR (
        commit_idempotency_key IS NOT NULL
        AND committed_ingest_event_id IS NOT NULL
        AND committed_at IS NOT NULL
      )
    )
);

CREATE INDEX idx_extraction_batches_student_status
ON extraction_batches(student_id, status, created_at);

CREATE TABLE extraction_assets (
    extraction_asset_id TEXT PRIMARY KEY,
    extraction_batch_id TEXT NOT NULL REFERENCES extraction_batches(extraction_batch_id),
    source_uri TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    media_type TEXT NOT NULL,
    byte_size INTEGER CHECK (byte_size IS NULL OR byte_size >= 0),
    page_number INTEGER CHECK (page_number IS NULL OR page_number > 0),
    created_at TEXT NOT NULL,
    UNIQUE(extraction_batch_id, extraction_asset_id)
);

CREATE INDEX idx_extraction_assets_batch_hash
ON extraction_assets(extraction_batch_id, sha256);

CREATE TABLE extraction_items (
    extraction_item_id TEXT PRIMARY KEY,
    extraction_batch_id TEXT NOT NULL REFERENCES extraction_batches(extraction_batch_id),
    extraction_asset_id TEXT,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    question_ref TEXT NOT NULL,
    question_type TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('R0', 'R1', 'R2', 'R3', 'R4')),
    second_model_required INTEGER NOT NULL DEFAULT 0 CHECK (second_model_required IN (0, 1)),
    second_model_reason TEXT,
    evidence_locator_json TEXT NOT NULL,
    attempt_template_json TEXT NOT NULL,
    template_sha256 TEXT NOT NULL CHECK (length(template_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(extraction_batch_id, ordinal),
    UNIQUE(extraction_batch_id, extraction_item_id),
    FOREIGN KEY(extraction_batch_id, extraction_asset_id)
      REFERENCES extraction_assets(extraction_batch_id, extraction_asset_id),
    CHECK (second_model_required = 0 OR trim(COALESCE(second_model_reason, '')) <> ''),
    CHECK (risk_level NOT IN ('R1', 'R2', 'R3') OR second_model_required = 1)
);

CREATE TABLE extraction_provider_results (
    provider_result_id TEXT PRIMARY KEY,
    submission_idempotency_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    submission_sha256 TEXT NOT NULL CHECK (length(submission_sha256) = 64),
    extraction_batch_id TEXT NOT NULL,
    extraction_item_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('codex', 'doubao', 'deterministic')),
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    result_status TEXT NOT NULL CHECK (result_status IN (
      'succeeded', 'failed', 'unconfigured', 'timeout', 'rate_limited'
    )),
    raw_transcription TEXT,
    normalized_transcription TEXT,
    capture_status TEXT CHECK (capture_status IS NULL OR capture_status IN (
      'captured', 'captured_blank', 'not_captured', 'needs_check',
      'blocked_image_quality', 'blocked_alignment'
    )),
    uncertain_spans_json TEXT,
    candidate_alternatives_json TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    evidence_locator_json TEXT,
    raw_output_json TEXT,
    response_sha256 TEXT CHECK (response_sha256 IS NULL OR length(response_sha256) = 64),
    error_summary TEXT,
    completed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(extraction_batch_id, extraction_item_id, provider_result_id),
    UNIQUE(submission_idempotency_key, extraction_item_id),
    FOREIGN KEY(extraction_batch_id, extraction_item_id)
      REFERENCES extraction_items(extraction_batch_id, extraction_item_id),
    CHECK (
      result_status <> 'succeeded' OR capture_status IS NOT NULL
    ),
    CHECK (
      result_status = 'succeeded'
      OR trim(COALESCE(error_summary, '')) <> ''
    )
);

CREATE INDEX idx_extraction_provider_item
ON extraction_provider_results(extraction_batch_id, extraction_item_id, provider, result_status, completed_at);

CREATE TABLE extraction_confirmation_decisions (
    confirmation_decision_id TEXT PRIMARY KEY,
    submission_idempotency_key TEXT NOT NULL,
    submission_sha256 TEXT NOT NULL CHECK (length(submission_sha256) = 64),
    extraction_batch_id TEXT NOT NULL,
    extraction_item_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    review_version INTEGER NOT NULL CHECK (review_version > 0),
    action TEXT NOT NULL CHECK (action IN (
      'pending_review', 'needs_check', 'human_confirmed', 'human_corrected',
      'confirmed_blank', 'not_captured', 'rejected_alignment'
    )),
    confirmed_text TEXT,
    selected_provider_result_id TEXT,
    evaluation_json TEXT,
    actor TEXT NOT NULL CHECK (trim(actor) <> ''),
    reason TEXT,
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(extraction_batch_id, extraction_item_id, revision_no),
    UNIQUE(extraction_batch_id, extraction_item_id, confirmation_decision_id),
    UNIQUE(submission_idempotency_key, extraction_item_id),
    FOREIGN KEY(extraction_batch_id, extraction_item_id)
      REFERENCES extraction_items(extraction_batch_id, extraction_item_id),
    FOREIGN KEY(extraction_batch_id, extraction_item_id, selected_provider_result_id)
      REFERENCES extraction_provider_results(extraction_batch_id, extraction_item_id, provider_result_id)
);

CREATE INDEX idx_extraction_decisions_current
ON extraction_confirmation_decisions(extraction_batch_id, extraction_item_id, revision_no DESC);

CREATE TABLE extraction_commit_links (
    extraction_batch_id TEXT NOT NULL,
    extraction_item_id TEXT NOT NULL,
    confirmation_decision_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    evaluation_id TEXT NOT NULL UNIQUE REFERENCES evaluations(evaluation_id),
    ingest_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    committed_at TEXT NOT NULL,
    PRIMARY KEY(extraction_batch_id, extraction_item_id),
    FOREIGN KEY(extraction_batch_id, extraction_item_id)
      REFERENCES extraction_items(extraction_batch_id, extraction_item_id),
    FOREIGN KEY(extraction_batch_id, extraction_item_id, confirmation_decision_id)
      REFERENCES extraction_confirmation_decisions(
        extraction_batch_id, extraction_item_id, confirmation_decision_id
      )
);

-- Every batch is explicitly learner/session scoped before any candidate data is
-- accepted.  The active subject enrollment prevents a stale dashboard choice
-- from authorizing a write.
CREATE TRIGGER extraction_batches_owner_insert
BEFORE INSERT ON extraction_batches
WHEN NOT EXISTS (
        SELECT 1 FROM students s
        WHERE s.student_id = NEW.student_id AND s.active = 1
     )
  OR NOT EXISTS (
        SELECT 1 FROM learning_sessions ls
        WHERE ls.session_id = NEW.session_id
          AND ls.student_id = NEW.student_id
          AND ls.record_status = 'active'
     )
  OR NOT EXISTS (
        SELECT 1 FROM student_subjects ss
        WHERE ss.student_id = NEW.student_id
          AND ss.subject_code = NEW.subject_code
          AND ss.active = 1
     )
BEGIN
    SELECT RAISE(ABORT, 'extraction batch student, session, or subject ownership is invalid');
END;

CREATE TRIGGER extraction_batches_identity_immutable
BEFORE UPDATE OF
  extraction_batch_id,idempotency_key,request_sha256,contract_version,
  student_id,subject_code,session_id,expected_item_count,
  comparison_policy_version,created_at
ON extraction_batches
WHEN OLD.extraction_batch_id IS NOT NEW.extraction_batch_id
  OR OLD.idempotency_key IS NOT NEW.idempotency_key
  OR OLD.request_sha256 IS NOT NEW.request_sha256
  OR OLD.contract_version IS NOT NEW.contract_version
  OR OLD.student_id IS NOT NEW.student_id
  OR OLD.subject_code IS NOT NEW.subject_code
  OR OLD.session_id IS NOT NEW.session_id
  OR OLD.expected_item_count IS NOT NEW.expected_item_count
  OR OLD.comparison_policy_version IS NOT NEW.comparison_policy_version
  OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'extraction batch identity and ownership are immutable');
END;

CREATE TRIGGER extraction_batches_committed_immutable
BEFORE UPDATE ON extraction_batches
WHEN OLD.status = 'committed'
BEGIN
    SELECT RAISE(ABORT, 'committed extraction batches are immutable');
END;

CREATE TRIGGER extraction_batches_no_delete
BEFORE DELETE ON extraction_batches
BEGIN
    SELECT RAISE(ABORT, 'extraction batches are audit records and cannot be deleted');
END;

CREATE TRIGGER extraction_batches_commit_guard
BEFORE UPDATE OF status ON extraction_batches
WHEN NEW.status = 'committed' AND OLD.status <> 'committed'
BEGIN
    SELECT CASE WHEN OLD.status <> 'ready_to_commit'
      THEN RAISE(ABORT, 'extraction batch must be ready_to_commit before commit') END;

    SELECT CASE WHEN NEW.commit_idempotency_key IS NULL
                     OR NEW.commit_request_sha256 IS NULL
                     OR NEW.committed_ingest_event_id IS NULL
                     OR NEW.committed_at IS NULL
      THEN RAISE(ABORT, 'extraction commit metadata is incomplete') END;

    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM ingest_events ie
        WHERE ie.ingest_event_id = NEW.committed_ingest_event_id
          AND ie.status = 'applied'
      )
      THEN RAISE(ABORT, 'extraction commit ingest event is missing or inactive') END;

    SELECT CASE WHEN (
        SELECT COUNT(*) FROM extraction_items i
        WHERE i.extraction_batch_id = NEW.extraction_batch_id
      ) <> NEW.expected_item_count
      THEN RAISE(ABORT, 'extraction batch item count does not match expected_item_count') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM extraction_items i
        WHERE i.extraction_batch_id = NEW.extraction_batch_id
          AND NOT EXISTS (
            SELECT 1
            FROM extraction_confirmation_decisions d
            WHERE d.extraction_batch_id = i.extraction_batch_id
              AND d.extraction_item_id = i.extraction_item_id
              AND d.revision_no = (
                SELECT MAX(d2.revision_no)
                FROM extraction_confirmation_decisions d2
                WHERE d2.extraction_batch_id = i.extraction_batch_id
                  AND d2.extraction_item_id = i.extraction_item_id
              )
          )
      )
      THEN RAISE(ABORT, 'every extraction item requires a current human decision') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM extraction_confirmation_decisions d
        WHERE d.extraction_batch_id = NEW.extraction_batch_id
          AND d.revision_no = (
            SELECT MAX(d2.revision_no)
            FROM extraction_confirmation_decisions d2
            WHERE d2.extraction_batch_id = d.extraction_batch_id AND d2.extraction_item_id = d.extraction_item_id
          )
          AND d.action IN ('pending_review', 'needs_check')
      )
      THEN RAISE(ABORT, 'pending_review or needs_check blocks extraction commit') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM extraction_confirmation_decisions d
        WHERE d.extraction_batch_id = NEW.extraction_batch_id
          AND d.revision_no = (
            SELECT MAX(d2.revision_no)
            FROM extraction_confirmation_decisions d2
            WHERE d2.extraction_batch_id = d.extraction_batch_id AND d2.extraction_item_id = d.extraction_item_id
          )
          AND d.action IN ('human_confirmed', 'human_corrected', 'confirmed_blank')
          AND NOT EXISTS (
            SELECT 1 FROM extraction_commit_links l
            WHERE l.extraction_batch_id = d.extraction_batch_id AND l.extraction_item_id = d.extraction_item_id
          )
      )
      THEN RAISE(ABORT, 'every committable extraction decision requires one commit link') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM extraction_confirmation_decisions d
        JOIN extraction_commit_links l
          ON l.extraction_batch_id = d.extraction_batch_id AND l.extraction_item_id = d.extraction_item_id
        WHERE d.extraction_batch_id = NEW.extraction_batch_id
          AND d.revision_no = (
            SELECT MAX(d2.revision_no)
            FROM extraction_confirmation_decisions d2
            WHERE d2.extraction_batch_id = d.extraction_batch_id AND d2.extraction_item_id = d.extraction_item_id
          )
          AND d.action IN ('not_captured', 'rejected_alignment')
      )
      THEN RAISE(ABORT, 'excluded extraction decisions cannot link to formal attempts') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM extraction_commit_links l
        WHERE l.extraction_batch_id = NEW.extraction_batch_id
          AND l.ingest_event_id <> NEW.committed_ingest_event_id
      )
      THEN RAISE(ABORT, 'extraction commit links must use the committed ingest event') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM attempts a
        WHERE a.ingest_event_id = NEW.committed_ingest_event_id
          AND a.record_status = 'active'
          AND NOT EXISTS (
            SELECT 1 FROM extraction_commit_links l
            WHERE l.extraction_batch_id = NEW.extraction_batch_id
              AND l.attempt_id = a.attempt_id
          )
      )
      THEN RAISE(ABORT, 'committed ingest event contains an unlinked formal attempt') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM extraction_items i
        JOIN extraction_confirmation_decisions d
          ON d.extraction_batch_id = i.extraction_batch_id AND d.extraction_item_id = i.extraction_item_id
        WHERE i.extraction_batch_id = NEW.extraction_batch_id
          AND i.second_model_required = 1
          AND d.revision_no = (
            SELECT MAX(d2.revision_no)
            FROM extraction_confirmation_decisions d2
            WHERE d2.extraction_batch_id = d.extraction_batch_id AND d2.extraction_item_id = d.extraction_item_id
          )
          AND d.action IN ('human_confirmed', 'human_corrected', 'confirmed_blank')
          AND (
            NOT EXISTS (
              SELECT 1 FROM extraction_provider_results p
              WHERE p.extraction_batch_id = i.extraction_batch_id
                AND p.extraction_item_id = i.extraction_item_id
                AND p.provider = 'codex'
                AND p.result_status = 'succeeded'
            )
            OR NOT EXISTS (
              SELECT 1 FROM extraction_provider_results p
              WHERE p.extraction_batch_id = i.extraction_batch_id
                AND p.extraction_item_id = i.extraction_item_id
                AND p.provider = 'doubao'
                AND p.result_status = 'succeeded'
            )
          )
      )
      THEN RAISE(ABORT, 'second-model-required items need successful independent Codex and Doubao results') END;
END;

CREATE TRIGGER extraction_assets_open_batch_insert
BEFORE INSERT ON extraction_assets
WHEN NOT EXISTS (
    SELECT 1 FROM extraction_batches b
    WHERE b.extraction_batch_id = NEW.extraction_batch_id
      AND b.status NOT IN ('committed', 'cancelled', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'extraction assets cannot be added to a closed batch');
END;

CREATE TRIGGER extraction_assets_append_only_update
BEFORE UPDATE ON extraction_assets
BEGIN
    SELECT RAISE(ABORT, 'extraction assets are append-only');
END;

CREATE TRIGGER extraction_assets_append_only_delete
BEFORE DELETE ON extraction_assets
BEGIN
    SELECT RAISE(ABORT, 'extraction assets are append-only');
END;

CREATE TRIGGER extraction_items_append_only_update
BEFORE UPDATE ON extraction_items
BEGIN
    SELECT RAISE(ABORT, 'extraction items are append-only');
END;

CREATE TRIGGER extraction_items_open_batch_insert
BEFORE INSERT ON extraction_items
WHEN NOT EXISTS (
    SELECT 1 FROM extraction_batches b
    WHERE b.extraction_batch_id = NEW.extraction_batch_id
      AND b.status NOT IN ('committed', 'cancelled', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'extraction items cannot be added to a closed batch');
END;

CREATE TRIGGER extraction_items_append_only_delete
BEFORE DELETE ON extraction_items
BEGIN
    SELECT RAISE(ABORT, 'extraction items are append-only');
END;

CREATE TRIGGER extraction_provider_results_open_batch_insert
BEFORE INSERT ON extraction_provider_results
WHEN NOT EXISTS (
    SELECT 1 FROM extraction_batches b
    WHERE b.extraction_batch_id = NEW.extraction_batch_id
      AND b.status NOT IN ('committed', 'cancelled', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'provider results cannot be added to a closed extraction batch');
END;

CREATE TRIGGER extraction_provider_submission_consistency
BEFORE INSERT ON extraction_provider_results
WHEN EXISTS (
    SELECT 1 FROM extraction_provider_results p
    WHERE p.submission_idempotency_key = NEW.submission_idempotency_key
      AND (
        p.submission_sha256 <> NEW.submission_sha256
        OR p.extraction_batch_id <> NEW.extraction_batch_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'provider submission idempotency key was reused with a different payload');
END;

CREATE TRIGGER extraction_provider_results_append_only_update
BEFORE UPDATE ON extraction_provider_results
BEGIN
    SELECT RAISE(ABORT, 'extraction provider results are append-only');
END;

CREATE TRIGGER extraction_provider_results_append_only_delete
BEFORE DELETE ON extraction_provider_results
BEGIN
    SELECT RAISE(ABORT, 'extraction provider results are append-only');
END;

CREATE TRIGGER extraction_confirmation_submission_consistency
BEFORE INSERT ON extraction_confirmation_decisions
WHEN EXISTS (
    SELECT 1 FROM extraction_confirmation_decisions d
    WHERE d.submission_idempotency_key = NEW.submission_idempotency_key
      AND (
        d.submission_sha256 <> NEW.submission_sha256
        OR d.extraction_batch_id <> NEW.extraction_batch_id
        OR d.review_version <> NEW.review_version
      )
)
BEGIN
    SELECT RAISE(ABORT, 'confirmation idempotency key was reused with a different submission');
END;

CREATE TRIGGER extraction_confirmation_revision_sequence
BEFORE INSERT ON extraction_confirmation_decisions
WHEN NEW.revision_no <> COALESCE((
    SELECT MAX(d.revision_no)
    FROM extraction_confirmation_decisions d
    WHERE d.extraction_batch_id = NEW.extraction_batch_id AND d.extraction_item_id = NEW.extraction_item_id
), 0) + 1
BEGIN
    SELECT RAISE(ABORT, 'confirmation decision revision must append sequentially');
END;

CREATE TRIGGER extraction_confirmation_semantics
BEFORE INSERT ON extraction_confirmation_decisions
WHEN (
       NEW.action IN ('human_confirmed', 'human_corrected')
       AND trim(COALESCE(NEW.confirmed_text, '')) = ''
     )
  OR (
       NEW.action = 'confirmed_blank'
       AND trim(COALESCE(NEW.confirmed_text, '')) <> ''
     )
  OR (
       NEW.action IN ('pending_review', 'needs_check', 'not_captured', 'rejected_alignment')
       AND (
         NEW.confirmed_text IS NOT NULL
         OR NEW.evaluation_json IS NOT NULL
       )
     )
BEGIN
    SELECT RAISE(ABORT, 'confirmation text or evaluation does not match its action');
END;

CREATE TRIGGER extraction_confirmation_selected_provider
BEFORE INSERT ON extraction_confirmation_decisions
WHEN NEW.selected_provider_result_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM extraction_provider_results p
    WHERE p.extraction_batch_id = NEW.extraction_batch_id
      AND p.extraction_item_id = NEW.extraction_item_id
      AND p.provider_result_id = NEW.selected_provider_result_id
      AND p.result_status = 'succeeded'
 )
BEGIN
    SELECT RAISE(ABORT, 'selected provider result must be a successful result for this item');
END;

CREATE TRIGGER extraction_confirmation_open_batch_insert
BEFORE INSERT ON extraction_confirmation_decisions
WHEN NOT EXISTS (
    SELECT 1 FROM extraction_batches b
    WHERE b.extraction_batch_id = NEW.extraction_batch_id
      AND b.status NOT IN ('committed', 'cancelled', 'failed')
)
BEGIN
    SELECT RAISE(ABORT, 'confirmation decisions cannot be added to a closed extraction batch');
END;

CREATE TRIGGER extraction_confirmation_append_only_update
BEFORE UPDATE ON extraction_confirmation_decisions
BEGIN
    SELECT RAISE(ABORT, 'extraction confirmation decisions are append-only');
END;

CREATE TRIGGER extraction_confirmation_append_only_delete
BEFORE DELETE ON extraction_confirmation_decisions
BEGIN
    SELECT RAISE(ABORT, 'extraction confirmation decisions are append-only');
END;

CREATE TRIGGER extraction_commit_links_guard
BEFORE INSERT ON extraction_commit_links
WHEN NOT EXISTS (
        SELECT 1
        FROM extraction_confirmation_decisions d
        WHERE d.extraction_batch_id = NEW.extraction_batch_id
          AND d.extraction_item_id = NEW.extraction_item_id
          AND d.confirmation_decision_id = NEW.confirmation_decision_id
          AND d.revision_no = (
            SELECT MAX(d2.revision_no)
            FROM extraction_confirmation_decisions d2
            WHERE d2.extraction_batch_id = d.extraction_batch_id AND d2.extraction_item_id = d.extraction_item_id
          )
          AND d.action IN ('human_confirmed', 'human_corrected', 'confirmed_blank')
     )
  OR NOT EXISTS (
        SELECT 1
        FROM attempts a
        JOIN extraction_batches b ON b.extraction_batch_id = NEW.extraction_batch_id
        WHERE a.attempt_id = NEW.attempt_id
          AND b.status = 'ready_to_commit'
          AND a.student_id = b.student_id
          AND a.session_id = b.session_id
          AND a.ingest_event_id = NEW.ingest_event_id
          AND a.record_status = 'active'
     )
  OR NOT EXISTS (
        SELECT 1 FROM evaluations e
        WHERE e.evaluation_id = NEW.evaluation_id
          AND e.attempt_id = NEW.attempt_id
          AND e.is_current = 1
     )
  OR NOT EXISTS (
        SELECT 1 FROM ingest_events ie
        WHERE ie.ingest_event_id = NEW.ingest_event_id
          AND ie.status = 'applied'
     )
BEGIN
    SELECT RAISE(ABORT, 'extraction commit link violates confirmation or learning-fact ownership');
END;

CREATE TRIGGER extraction_commit_links_append_only_update
BEFORE UPDATE ON extraction_commit_links
BEGIN
    SELECT RAISE(ABORT, 'extraction commit links are append-only');
END;

CREATE TRIGGER extraction_commit_links_append_only_delete
BEFORE DELETE ON extraction_commit_links
BEGIN
    SELECT RAISE(ABORT, 'extraction commit links are append-only');
END;
