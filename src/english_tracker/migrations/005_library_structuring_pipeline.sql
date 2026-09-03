CREATE TABLE library_source_sets (
    source_set_id TEXT PRIMARY KEY,
    library_key TEXT NOT NULL,
    logical_key TEXT NOT NULL,
    title TEXT NOT NULL,
    year_hint INTEGER,
    exam_type TEXT,
    region_hint TEXT,
    content_kind TEXT NOT NULL CHECK (content_kind IN (
      'exam', 'exercise', 'teaching_material', 'answer_material', 'audio', 'mixed', 'other'
    )),
    pairing_status TEXT NOT NULL CHECK (pairing_status IN (
      'paired', 'prompt_only', 'answer_only', 'audio_only', 'mixed_single_file', 'needs_review'
    )),
    preferred_prompt_resource_id TEXT REFERENCES library_resources(resource_id),
    preferred_answer_resource_id TEXT REFERENCES library_resources(resource_id),
    preferred_audio_resource_id TEXT REFERENCES library_resources(resource_id),
    resource_count INTEGER NOT NULL DEFAULT 0 CHECK (resource_count >= 0),
    question_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (question_candidate_count >= 0),
    passage_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (passage_candidate_count >= 0),
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    parser_version TEXT,
    verification_status TEXT NOT NULL DEFAULT 'suggested' CHECK (verification_status IN (
      'suggested', 'needs_check', 'source_checked', 'verified', 'rejected'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(library_key, logical_key)
);

CREATE INDEX idx_library_source_sets_status
ON library_source_sets(library_key, pairing_status, content_kind, verification_status);

CREATE TABLE library_source_set_resources (
    source_set_id TEXT NOT NULL REFERENCES library_source_sets(source_set_id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL REFERENCES library_resources(resource_id),
    resource_role TEXT NOT NULL CHECK (resource_role IN (
      'prompt', 'answer', 'explanation', 'audio', 'transcript', 'teaching', 'duplicate', 'other'
    )),
    role_confidence REAL NOT NULL CHECK (role_confidence >= 0 AND role_confidence <= 1),
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_set_id, resource_id)
);

CREATE INDEX idx_library_set_resources_resource
ON library_source_set_resources(resource_id, resource_role);

CREATE TABLE library_text_chunks (
    chunk_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES library_resources(resource_id) ON DELETE CASCADE,
    source_set_id TEXT REFERENCES library_source_sets(source_set_id) ON DELETE SET NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end >= char_start),
    heading TEXT,
    chunk_text TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0 CHECK (token_estimate >= 0),
    parser_version TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'suggested' CHECK (verification_status IN (
      'suggested', 'needs_check', 'source_checked', 'verified', 'rejected'
    )),
    source_locator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(resource_id, chunk_index, parser_version)
);

CREATE INDEX idx_library_text_chunks_source
ON library_text_chunks(source_set_id, resource_id, chunk_index);

CREATE TABLE staged_passages (
    candidate_passage_id TEXT PRIMARY KEY,
    source_set_id TEXT NOT NULL REFERENCES library_source_sets(source_set_id) ON DELETE CASCADE,
    prompt_resource_id TEXT REFERENCES library_resources(resource_id),
    passage_type TEXT NOT NULL,
    title TEXT,
    passage_text TEXT NOT NULL,
    original_number_range TEXT,
    word_count INTEGER NOT NULL DEFAULT 0 CHECK (word_count >= 0),
    source_locator TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_status TEXT NOT NULL DEFAULT 'suggested' CHECK (verification_status IN (
      'suggested', 'needs_check', 'source_checked', 'verified', 'rejected'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_staged_passages_set
ON staged_passages(source_set_id, passage_type, verification_status);

CREATE TABLE staged_questions (
    candidate_question_id TEXT PRIMARY KEY,
    source_set_id TEXT NOT NULL REFERENCES library_source_sets(source_set_id) ON DELETE CASCADE,
    candidate_passage_id TEXT REFERENCES staged_passages(candidate_passage_id) ON DELETE SET NULL,
    prompt_resource_id TEXT REFERENCES library_resources(resource_id),
    answer_resource_id TEXT REFERENCES library_resources(resource_id),
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal > 0),
    original_number TEXT NOT NULL,
    section TEXT,
    question_type TEXT NOT NULL,
    stem TEXT NOT NULL,
    answer TEXT,
    explanation_raw TEXT,
    options_json TEXT NOT NULL DEFAULT '[]',
    primary_test_point TEXT,
    secondary_test_points_json TEXT NOT NULL DEFAULT '[]',
    difficulty TEXT,
    content_tags_json TEXT NOT NULL DEFAULT '[]',
    source_locator TEXT NOT NULL,
    answer_locator TEXT,
    parser_version TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_status TEXT NOT NULL DEFAULT 'suggested' CHECK (verification_status IN (
      'suggested', 'needs_check', 'source_checked', 'verified', 'rejected'
    )),
    review_reasons_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_set_id, source_ordinal, parser_version)
);

CREATE INDEX idx_staged_questions_type
ON staged_questions(question_type, verification_status, source_set_id);

CREATE TABLE staged_question_knowledge_map (
    candidate_question_id TEXT NOT NULL REFERENCES staged_questions(candidate_question_id) ON DELETE CASCADE,
    knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(knowledge_point_id),
    role TEXT NOT NULL CHECK (role IN ('primary', 'secondary', 'prerequisite', 'trap')),
    mapping_source TEXT NOT NULL CHECK (mapping_source IN ('legacy', 'rule', 'model_suggested', 'manual')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_status TEXT NOT NULL CHECK (verification_status IN (
      'suggested', 'source_checked', 'verified', 'needs_check', 'rejected'
    )),
    rationale TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(candidate_question_id, knowledge_point_id, role),
    CHECK (NOT (mapping_source = 'model_suggested' AND verification_status IN ('source_checked', 'verified'))),
    CHECK (NOT (mapping_source = 'rule' AND verification_status IN ('source_checked', 'verified')))
);

CREATE INDEX idx_staged_question_knowledge
ON staged_question_knowledge_map(knowledge_point_id, verification_status, candidate_question_id);

CREATE TABLE library_structure_reviews (
    review_id TEXT PRIMARY KEY,
    source_set_id TEXT REFERENCES library_source_sets(source_set_id) ON DELETE CASCADE,
    candidate_question_id TEXT REFERENCES staged_questions(candidate_question_id) ON DELETE CASCADE,
    resource_id TEXT REFERENCES library_resources(resource_id),
    problem_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    detail TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_library_structure_reviews_open
ON library_structure_reviews(status, severity, problem_type);

UPDATE project_work_items
SET evidence_path='exports/library_structure_current.json', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE work_item_id='WORK-FULL-PARSE';
