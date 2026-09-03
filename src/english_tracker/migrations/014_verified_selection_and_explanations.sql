CREATE TABLE question_selection_manifests (
    selection_manifest_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    generation_id TEXT REFERENCES artifact_generation_runs(generation_id),
    training_mode TEXT NOT NULL CHECK (
      training_mode IN ('correction', 'transfer', 'assessment', 'coverage')
    ),
    status TEXT NOT NULL DEFAULT 'building' CHECK (
      status IN ('building', 'finalized', 'cancelled')
    ),
    source_namespace TEXT NOT NULL,
    source_snapshot_sha256 TEXT NOT NULL CHECK (length(source_snapshot_sha256) = 64),
    source_locator_json TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    random_seed INTEGER,
    target_knowledge_json TEXT NOT NULL,
    selection_policy_json TEXT NOT NULL,
    explanation_contract_json TEXT NOT NULL,
    candidate_question_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_question_count >= 0),
    selected_group_count INTEGER NOT NULL DEFAULT 0 CHECK (selected_group_count >= 0),
    selected_question_count INTEGER NOT NULL DEFAULT 0 CHECK (selected_question_count >= 0),
    exclusion_count INTEGER NOT NULL DEFAULT 0 CHECK (exclusion_count >= 0),
    coverage_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    CHECK (status <> 'finalized' OR finalized_at IS NOT NULL)
);

CREATE INDEX idx_question_selection_manifest_student
ON question_selection_manifests(student_id, data_as_of DESC, created_at DESC);

CREATE TABLE question_selection_groups (
    selection_group_id TEXT PRIMARY KEY,
    selection_manifest_id TEXT NOT NULL REFERENCES question_selection_manifests(selection_manifest_id),
    group_kind TEXT NOT NULL CHECK (group_kind IN ('item', 'passage')),
    passage_id TEXT,
    source_id TEXT NOT NULL,
    source_locator_json TEXT NOT NULL,
    passage_content_sha256 TEXT,
    group_content_sha256 TEXT NOT NULL CHECK (length(group_content_sha256) = 64),
    expected_question_count INTEGER NOT NULL CHECK (expected_question_count > 0),
    selected_question_count INTEGER NOT NULL CHECK (selected_question_count > 0),
    complete_group INTEGER NOT NULL CHECK (complete_group IN (0, 1)),
    reason_codes_json TEXT NOT NULL,
    knowledge_codes_json TEXT NOT NULL,
    evidence_references_json TEXT NOT NULL,
    duplicate_check_json TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    UNIQUE(selection_manifest_id, ordinal),
    UNIQUE(selection_group_id, selection_manifest_id),
    CHECK (
      (group_kind = 'passage' AND passage_id IS NOT NULL AND complete_group = 1)
      OR (group_kind = 'item' AND passage_id IS NULL AND expected_question_count = 1
          AND selected_question_count = 1 AND complete_group = 1)
    )
);

CREATE INDEX idx_question_selection_groups_passage
ON question_selection_groups(passage_id, selection_manifest_id)
WHERE passage_id IS NOT NULL;

CREATE TABLE question_selection_items (
    selection_manifest_id TEXT NOT NULL REFERENCES question_selection_manifests(selection_manifest_id),
    selection_group_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    passage_id TEXT,
    question_type TEXT NOT NULL,
    original_number TEXT,
    source_locator_json TEXT NOT NULL,
    question_content_sha256 TEXT NOT NULL CHECK (length(question_content_sha256) = 64),
    standard_answer_sha256 TEXT NOT NULL CHECK (length(standard_answer_sha256) = 64),
    verification_status TEXT NOT NULL CHECK (
      verification_status IN ('source_checked', 'verified')
    ),
    is_real_question INTEGER NOT NULL CHECK (is_real_question = 1),
    reason_codes_json TEXT NOT NULL,
    knowledge_codes_json TEXT NOT NULL,
    mapping_evidence_json TEXT NOT NULL,
    duplicate_check_json TEXT NOT NULL,
    expected_public_explanation_cache_key TEXT NOT NULL
      CHECK (length(expected_public_explanation_cache_key) = 64),
    public_explanation_status TEXT NOT NULL CHECK (
      public_explanation_status IN (
        'not_generated', 'ai_draft', 'pending_review', 'source_checked',
        'teacher_confirmed', 'stale', 'deprecated', 'rejected'
      )
    ),
    group_ordinal INTEGER NOT NULL CHECK (group_ordinal > 0),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY(selection_manifest_id, question_id),
    UNIQUE(selection_manifest_id, ordinal),
    FOREIGN KEY(selection_group_id, selection_manifest_id)
      REFERENCES question_selection_groups(selection_group_id, selection_manifest_id)
);

CREATE INDEX idx_question_selection_items_question
ON question_selection_items(question_id, selection_manifest_id);

CREATE TABLE question_selection_exclusions (
    exclusion_id TEXT PRIMARY KEY,
    selection_manifest_id TEXT NOT NULL REFERENCES question_selection_manifests(selection_manifest_id),
    candidate_key TEXT NOT NULL,
    candidate_question_id TEXT,
    candidate_passage_id TEXT,
    source_id TEXT,
    source_locator_json TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK (
      reason_code IN (
        'unknown_question', 'unverified_question', 'not_real_question',
        'missing_source_locator', 'missing_standard_answer', 'incomplete_question',
        'incomplete_passage', 'exact_duplicate', 'near_duplicate',
        'question_limit', 'group_limit'
      )
    ),
    detail_json TEXT NOT NULL,
    question_content_sha256 TEXT,
    matched_question_id TEXT,
    similarity_score REAL CHECK (
      similarity_score IS NULL OR (similarity_score >= 0 AND similarity_score <= 1)
    ),
    created_at TEXT NOT NULL,
    UNIQUE(selection_manifest_id, candidate_key, reason_code)
);

CREATE INDEX idx_question_selection_exclusions_manifest
ON question_selection_exclusions(selection_manifest_id, reason_code, candidate_key);

-- This table deliberately contains no student, attempt, answer-submission, or diagnosis
-- foreign key.  Its content is reusable public question knowledge only.
CREATE TABLE public_question_explanations (
    public_explanation_id TEXT PRIMARY KEY,
    cache_key TEXT NOT NULL UNIQUE CHECK (length(cache_key) = 64),
    cache_key_version TEXT NOT NULL,
    question_id TEXT NOT NULL,
    source_namespace TEXT NOT NULL,
    source_id TEXT NOT NULL,
    passage_id TEXT,
    source_locator_json TEXT NOT NULL,
    source_snapshot_sha256 TEXT NOT NULL CHECK (length(source_snapshot_sha256) = 64),
    question_content_sha256 TEXT NOT NULL CHECK (length(question_content_sha256) = 64),
    standard_answer_sha256 TEXT NOT NULL CHECK (length(standard_answer_sha256) = 64),
    knowledge_mapping_sha256 TEXT NOT NULL CHECK (length(knowledge_mapping_sha256) = 64),
    rubric_version TEXT NOT NULL,
    rubric_sha256 TEXT NOT NULL CHECK (length(rubric_sha256) = 64),
    explanation_policy_version TEXT NOT NULL,
    explanation_schema_version TEXT NOT NULL,
    explanation_status TEXT NOT NULL CHECK (
      explanation_status IN (
        'ai_draft', 'pending_review', 'source_checked', 'teacher_confirmed',
        'stale', 'deprecated', 'rejected'
      )
    ),
    explanation_json TEXT NOT NULL,
    explanation_sha256 TEXT NOT NULL CHECK (length(explanation_sha256) = 64),
    created_by TEXT NOT NULL,
    confirmed_by TEXT,
    supersedes_public_explanation_id TEXT REFERENCES public_question_explanations(public_explanation_id),
    invalidated_at TEXT,
    invalidation_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
      explanation_status NOT IN ('source_checked', 'teacher_confirmed')
      OR (confirmed_by IS NOT NULL AND trim(confirmed_by) <> '')
    ),
    CHECK (
      explanation_status NOT IN ('stale', 'deprecated', 'rejected')
      OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL
          AND trim(invalidation_reason) <> '')
    )
);

CREATE UNIQUE INDEX uq_public_question_explanation_current
ON public_question_explanations(question_id)
WHERE explanation_status IN ('ai_draft', 'pending_review', 'source_checked', 'teacher_confirmed');

CREATE INDEX idx_public_question_explanation_lookup
ON public_question_explanations(question_id, cache_key, explanation_status, updated_at DESC);

CREATE TRIGGER question_selection_requires_building_insert
BEFORE INSERT ON question_selection_manifests
WHEN NEW.status <> 'building'
BEGIN
    SELECT RAISE(ABORT, 'selection manifest must be inserted in building status');
END;

CREATE TRIGGER question_selection_generation_owner_insert
BEFORE INSERT ON question_selection_manifests
WHEN NEW.generation_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifact_generation_runs g
     WHERE g.generation_id = NEW.generation_id AND g.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'selection generation belongs to another student');
END;

CREATE TRIGGER question_selection_generation_owner_update
BEFORE UPDATE OF student_id, generation_id ON question_selection_manifests
WHEN NEW.generation_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifact_generation_runs g
     WHERE g.generation_id = NEW.generation_id AND g.student_id = NEW.student_id
 )
BEGIN
    SELECT RAISE(ABORT, 'selection generation belongs to another student');
END;

CREATE TRIGGER question_selection_finalize_integrity
BEFORE UPDATE OF status ON question_selection_manifests
WHEN NEW.status = 'finalized' AND OLD.status <> 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'selection group count mismatch')
    WHERE NEW.selected_group_count <> (
      SELECT COUNT(*) FROM question_selection_groups g
      WHERE g.selection_manifest_id = NEW.selection_manifest_id
    );
    SELECT RAISE(ABORT, 'selection item count mismatch')
    WHERE NEW.selected_question_count <> (
      SELECT COUNT(*) FROM question_selection_items i
      WHERE i.selection_manifest_id = NEW.selection_manifest_id
    );
    SELECT RAISE(ABORT, 'selection exclusion count mismatch')
    WHERE NEW.exclusion_count <> (
      SELECT COUNT(*) FROM question_selection_exclusions e
      WHERE e.selection_manifest_id = NEW.selection_manifest_id
    );
    SELECT RAISE(ABORT, 'selection contains an incomplete passage group')
    WHERE EXISTS (
      SELECT 1 FROM question_selection_groups g
      WHERE g.selection_manifest_id = NEW.selection_manifest_id
        AND (g.complete_group <> 1 OR g.expected_question_count <> g.selected_question_count)
    );
    SELECT RAISE(ABORT, 'selection group item count mismatch')
    WHERE EXISTS (
      SELECT 1
      FROM question_selection_groups g
      WHERE g.selection_manifest_id = NEW.selection_manifest_id
        AND g.selected_question_count <> (
          SELECT COUNT(*) FROM question_selection_items i
          WHERE i.selection_manifest_id = NEW.selection_manifest_id
            AND i.selection_group_id = g.selection_group_id
        )
    );
END;

CREATE TRIGGER question_selection_manifest_immutable_update
BEFORE UPDATE ON question_selection_manifests
WHEN OLD.status = 'finalized'
BEGIN
    SELECT RAISE(ABORT, 'finalized selection manifest is immutable');
END;

CREATE TRIGGER question_selection_manifest_immutable_delete
BEFORE DELETE ON question_selection_manifests
BEGIN
    SELECT RAISE(ABORT, 'selection manifest is append-only');
END;

CREATE TRIGGER question_selection_group_after_finalize_insert
BEFORE INSERT ON question_selection_groups
WHEN EXISTS (
  SELECT 1 FROM question_selection_manifests m
  WHERE m.selection_manifest_id = NEW.selection_manifest_id AND m.status <> 'building'
)
BEGIN
    SELECT RAISE(ABORT, 'selection groups are immutable after finalization');
END;

CREATE TRIGGER question_selection_group_immutable_update
BEFORE UPDATE ON question_selection_groups
BEGIN
    SELECT RAISE(ABORT, 'selection groups are append-only');
END;

CREATE TRIGGER question_selection_group_immutable_delete
BEFORE DELETE ON question_selection_groups
BEGIN
    SELECT RAISE(ABORT, 'selection groups are append-only');
END;

CREATE TRIGGER question_selection_item_after_finalize_insert
BEFORE INSERT ON question_selection_items
WHEN EXISTS (
  SELECT 1 FROM question_selection_manifests m
  WHERE m.selection_manifest_id = NEW.selection_manifest_id AND m.status <> 'building'
)
BEGIN
    SELECT RAISE(ABORT, 'selection items are immutable after finalization');
END;

CREATE TRIGGER question_selection_item_group_consistency
BEFORE INSERT ON question_selection_items
WHEN NOT EXISTS (
  SELECT 1
  FROM question_selection_groups g
  WHERE g.selection_group_id = NEW.selection_group_id
    AND g.selection_manifest_id = NEW.selection_manifest_id
    AND g.source_id = NEW.source_id
    AND (
      (g.group_kind = 'passage' AND g.passage_id IS NEW.passage_id)
      OR (g.group_kind = 'item' AND NEW.passage_id IS NULL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'selection item does not match its source group');
END;

CREATE TRIGGER question_selection_item_immutable_update
BEFORE UPDATE ON question_selection_items
BEGIN
    SELECT RAISE(ABORT, 'selection items are append-only');
END;

CREATE TRIGGER question_selection_item_immutable_delete
BEFORE DELETE ON question_selection_items
BEGIN
    SELECT RAISE(ABORT, 'selection items are append-only');
END;

CREATE TRIGGER question_selection_exclusion_after_finalize_insert
BEFORE INSERT ON question_selection_exclusions
WHEN EXISTS (
  SELECT 1 FROM question_selection_manifests m
  WHERE m.selection_manifest_id = NEW.selection_manifest_id AND m.status <> 'building'
)
BEGIN
    SELECT RAISE(ABORT, 'selection exclusions are immutable after finalization');
END;

CREATE TRIGGER question_selection_exclusion_immutable_update
BEFORE UPDATE ON question_selection_exclusions
BEGIN
    SELECT RAISE(ABORT, 'selection exclusions are append-only');
END;

CREATE TRIGGER question_selection_exclusion_immutable_delete
BEFORE DELETE ON question_selection_exclusions
BEGIN
    SELECT RAISE(ABORT, 'selection exclusions are append-only');
END;

CREATE TRIGGER public_question_explanation_identity_immutable
BEFORE UPDATE ON public_question_explanations
WHEN NEW.cache_key IS NOT OLD.cache_key
  OR NEW.cache_key_version IS NOT OLD.cache_key_version
  OR NEW.question_id IS NOT OLD.question_id
  OR NEW.source_namespace IS NOT OLD.source_namespace
  OR NEW.source_id IS NOT OLD.source_id
  OR NEW.passage_id IS NOT OLD.passage_id
  OR NEW.source_locator_json IS NOT OLD.source_locator_json
  OR NEW.source_snapshot_sha256 IS NOT OLD.source_snapshot_sha256
  OR NEW.question_content_sha256 IS NOT OLD.question_content_sha256
  OR NEW.standard_answer_sha256 IS NOT OLD.standard_answer_sha256
  OR NEW.knowledge_mapping_sha256 IS NOT OLD.knowledge_mapping_sha256
  OR NEW.rubric_version IS NOT OLD.rubric_version
  OR NEW.rubric_sha256 IS NOT OLD.rubric_sha256
  OR NEW.explanation_policy_version IS NOT OLD.explanation_policy_version
  OR NEW.explanation_schema_version IS NOT OLD.explanation_schema_version
  OR NEW.explanation_json IS NOT OLD.explanation_json
  OR NEW.explanation_sha256 IS NOT OLD.explanation_sha256
  OR NEW.created_by IS NOT OLD.created_by
  OR NEW.confirmed_by IS NOT OLD.confirmed_by
  OR NEW.supersedes_public_explanation_id IS NOT OLD.supersedes_public_explanation_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'public explanation identity and content are immutable');
END;

CREATE TRIGGER public_question_explanation_immutable_delete
BEFORE DELETE ON public_question_explanations
BEGIN
    SELECT RAISE(ABORT, 'public explanations are append-only');
END;
