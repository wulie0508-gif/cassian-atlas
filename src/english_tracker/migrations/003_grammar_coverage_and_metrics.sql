CREATE TABLE source_snapshots (
    source_snapshot_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes >= 0),
    source_modified_at TEXT,
    filter_definition TEXT NOT NULL,
    question_count INTEGER NOT NULL CHECK (question_count >= 0),
    passage_count INTEGER NOT NULL CHECK (passage_count >= 0),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(namespace, source_sha256, filter_definition)
);

CREATE UNIQUE INDEX uq_source_snapshots_current
ON source_snapshots(namespace) WHERE is_current = 1;

CREATE TABLE grammar_passage_catalog (
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(source_snapshot_id),
    passage_id TEXT NOT NULL,
    source_id TEXT,
    title TEXT,
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    source_checked_question_count INTEGER NOT NULL CHECK (source_checked_question_count >= 0),
    complete_passage INTEGER NOT NULL CHECK (complete_passage IN (0, 1)),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('source_checked', 'verified', 'needs_check', 'unverified')),
    PRIMARY KEY(source_snapshot_id, passage_id)
);

CREATE TABLE grammar_question_catalog (
    source_snapshot_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    passage_id TEXT NOT NULL,
    source_id TEXT,
    original_number TEXT,
    year INTEGER,
    exam_type TEXT,
    district_or_school TEXT,
    difficulty_label TEXT,
    answer_available INTEGER NOT NULL CHECK (answer_available IN (0, 1)),
    explanation_available INTEGER NOT NULL CHECK (explanation_available IN (0, 1)),
    primary_test_point_raw TEXT,
    primary_test_point_normalized TEXT,
    secondary_test_points_raw TEXT,
    secondary_test_points_normalized TEXT,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('source_checked', 'verified', 'needs_check', 'unverified')),
    PRIMARY KEY(source_snapshot_id, question_id),
    FOREIGN KEY(source_snapshot_id, passage_id)
      REFERENCES grammar_passage_catalog(source_snapshot_id, passage_id)
);

CREATE TABLE question_knowledge_map (
    source_snapshot_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(knowledge_point_id),
    role TEXT NOT NULL CHECK (role IN ('primary', 'secondary', 'prerequisite', 'trap')),
    mapping_source TEXT NOT NULL CHECK (mapping_source IN ('legacy', 'rule', 'model_suggested', 'manual')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('suggested', 'source_checked', 'verified', 'needs_check', 'rejected')),
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_snapshot_id, question_id, knowledge_point_id, role),
    FOREIGN KEY(source_snapshot_id, question_id)
      REFERENCES grammar_question_catalog(source_snapshot_id, question_id),
    CHECK (NOT (mapping_source = 'model_suggested' AND verification_status IN ('source_checked', 'verified')))
);

CREATE INDEX idx_grammar_catalog_passage
ON grammar_question_catalog(source_snapshot_id, passage_id, original_number);

CREATE INDEX idx_question_knowledge_lookup
ON question_knowledge_map(source_snapshot_id, knowledge_point_id, question_id);

ALTER TABLE item_knowledge_map RENAME TO item_knowledge_map_v2_legacy;

CREATE TABLE item_knowledge_map (
    item_id TEXT NOT NULL REFERENCES content_items(item_id),
    knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(knowledge_point_id),
    mapping_role TEXT NOT NULL DEFAULT 'primary' CHECK (mapping_role IN ('primary', 'secondary', 'prerequisite', 'trap')),
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    mapping_source TEXT NOT NULL DEFAULT 'legacy' CHECK (mapping_source IN ('legacy', 'rule', 'model_suggested', 'manual')),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('suggested', 'source_checked', 'verified', 'needs_check', 'unverified', 'rejected')),
    rationale TEXT,
    source_snapshot_id TEXT REFERENCES source_snapshots(source_snapshot_id),
    PRIMARY KEY(item_id, knowledge_point_id),
    CHECK (NOT (mapping_source = 'model_suggested' AND verification_status IN ('source_checked', 'verified')))
);

INSERT INTO item_knowledge_map(
    item_id, knowledge_point_id, mapping_role, weight, mapping_source,
    confidence, verification_status, rationale, source_snapshot_id
)
SELECT
    item_id,
    knowledge_point_id,
    CASE WHEN mapping_role = 'inferred' THEN 'secondary' ELSE mapping_role END,
    weight,
    'legacy',
    CASE WHEN mapping_role = 'inferred' THEN 0.70 ELSE 1.0 END,
    CASE
      WHEN validation_status IN ('verified', 'source_checked', 'needs_check') THEN validation_status
      ELSE 'unverified'
    END,
    evidence_source,
    NULL
FROM item_knowledge_map_v2_legacy;

DROP TABLE item_knowledge_map_v2_legacy;

CREATE INDEX idx_item_knowledge_knowledge
ON item_knowledge_map(knowledge_point_id, item_id);

ALTER TABLE attempt_error_map RENAME TO attempt_error_map_v2_legacy;

CREATE TABLE attempt_error_map (
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    error_type_id TEXT NOT NULL REFERENCES error_types(error_type_id),
    raw_error_type TEXT,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    note TEXT,
    error_source TEXT NOT NULL DEFAULT 'legacy' CHECK (error_source IN (
      'student_answer', 'teacher_observation', 'manual', 'legacy', 'rule', 'model_suggested'
    )),
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN (
      'suggested', 'source_checked', 'verified', 'needs_check', 'rejected', 'unverified'
    )),
    rationale TEXT,
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'voided')),
    invalidation_reason TEXT,
    PRIMARY KEY(attempt_id, error_type_id, raw_error_type),
    CHECK (NOT (error_source = 'model_suggested' AND verification_status IN ('source_checked', 'verified')))
);

INSERT INTO attempt_error_map(
    attempt_id, error_type_id, raw_error_type, confidence, note,
    error_source, verification_status, rationale, record_status, invalidation_reason
)
SELECT
    old.attempt_id,
    old.error_type_id,
    old.raw_error_type,
    old.confidence,
    old.note,
    'legacy',
    CASE WHEN a.answer_capture_status = 'not_captured' THEN 'rejected' ELSE 'unverified' END,
    old.note,
    CASE WHEN a.answer_capture_status = 'not_captured' THEN 'voided' ELSE 'active' END,
    CASE WHEN a.answer_capture_status = 'not_captured'
      THEN 'Specific error cause cannot be inferred when the original student answer was not captured.'
      ELSE NULL END
FROM attempt_error_map_v2_legacy old
JOIN attempts a ON a.attempt_id = old.attempt_id;

DROP TABLE attempt_error_map_v2_legacy;

CREATE INDEX idx_attempt_error_active
ON attempt_error_map(attempt_id, record_status);

CREATE TABLE session_assessments (
    session_id TEXT PRIMARY KEY REFERENCES learning_sessions(session_id),
    assessment_kind TEXT NOT NULL CHECK (assessment_kind IN (
      'lesson', 'topic_quiz', 'biweekly_mixed_test', 'full_exam', 'dictation', 'homework', 'other'
    )),
    reporting_series TEXT NOT NULL,
    delivery_mode TEXT NOT NULL DEFAULT 'unspecified' CHECK (delivery_mode IN (
      'offline_closed', 'offline_open', 'online', 'home', 'unspecified'
    )),
    raw_score REAL,
    max_score REAL CHECK (max_score IS NULL OR max_score > 0),
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    blank_count INTEGER CHECK (blank_count IS NULL OR blank_count >= 0),
    validation_status TEXT NOT NULL DEFAULT 'unverified' CHECK (validation_status IN ('verified', 'source_checked', 'unverified', 'needs_check')),
    created_by_event_id TEXT NOT NULL REFERENCES ingest_events(ingest_event_id),
    created_at TEXT NOT NULL,
    CHECK (raw_score IS NULL OR max_score IS NOT NULL),
    CHECK (raw_score IS NULL OR (raw_score >= 0 AND raw_score <= max_score))
);

CREATE TABLE passage_selection_runs (
    selection_run_id TEXT PRIMARY KEY,
    student_id TEXT REFERENCES students(student_id),
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(source_snapshot_id),
    requested_knowledge_json TEXT NOT NULL,
    recent_error_window_days INTEGER,
    algorithm TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT OR IGNORE INTO knowledge_points(
    knowledge_point_id, code, parent_id, domain, name_en, name_cn, description
) VALUES
('KP-GRA-BACKBONE', 'sentence_backbone', 'KP-GRA', 'grammar', 'Complete sentence backbone', '完整句子主干', 'Locate clauses and determine whether each clause has a complete finite predicate.'),
('KP-GRA-PREDCOUNT', 'predicate_count', 'KP-GRA-BACKBONE', 'grammar', 'Predicate count', '谓语数量', 'Count finite predicates before choosing a finite or non-finite form.'),
('KP-GRA-PASSIVE', 'passive_voice', 'KP-GRA-VOICE', 'grammar', 'Passive voice', '被动语态', 'Choose and form passive voice in finite and non-finite structures.'),
('KP-GRA-MODAL', 'modal_verb', 'KP-GRA', 'grammar', 'Modal verbs', '情态动词', 'Select modal meaning and form.'),
('KP-GRA-INFINITIVE', 'infinitive', 'KP-GRA-NONFINITE', 'grammar', 'Infinitive', '不定式', 'Infinitive form and syntactic function.'),
('KP-GRA-GERUND', 'gerund', 'KP-GRA-NONFINITE', 'grammar', 'Gerund', '动名词', 'Gerund form and syntactic function.'),
('KP-GRA-PARTICIPLE', 'participle', 'KP-GRA-NONFINITE', 'grammar', 'Participles', '分词', 'Present and past participle category.'),
('KP-GRA-PRESPART', 'present_participle', 'KP-GRA-PARTICIPLE', 'grammar', 'Present participle', '现在分词', 'Present participle form and syntactic function.'),
('KP-GRA-PASTPART', 'past_participle', 'KP-GRA-PARTICIPLE', 'grammar', 'Past participle', '过去分词', 'Past participle form and syntactic function.'),
('KP-GRA-NF-LOGSUBJ', 'non_finite_logical_subject', 'KP-GRA-NONFINITE', 'grammar', 'Non-finite logical subject', '非谓语逻辑主语', 'Identify the understood subject of a non-finite form.'),
('KP-GRA-NF-VOICE', 'non_finite_voice', 'KP-GRA-NONFINITE', 'grammar', 'Non-finite voice', '非谓语主动被动', 'Choose active or passive non-finite form from its logical subject.'),
('KP-GRA-NF-SEQUENCE', 'non_finite_sequence', 'KP-GRA-NONFINITE', 'grammar', 'Non-finite action sequence', '非谓语动作先后', 'Choose simple, progressive, or perfect non-finite form from action sequence.'),
('KP-GRA-DERIV', 'word_derivation', 'KP-GRA', 'grammar', 'Part-of-speech derivation', '词性派生', 'Derive the required part of speech from sentence position.'),
('KP-GRA-DERIV-N', 'noun_derivation', 'KP-GRA-DERIV', 'grammar', 'Noun derivation', '名词派生', 'Derive a noun form.'),
('KP-GRA-DERIV-ADJ', 'adjective_derivation', 'KP-GRA-DERIV', 'grammar', 'Adjective derivation', '形容词派生', 'Derive an adjective form.'),
('KP-GRA-DERIV-ADV', 'adverb_derivation', 'KP-GRA-DERIV', 'grammar', 'Adverb derivation', '副词派生', 'Derive an adverb form.'),
('KP-GRA-NUMBER', 'noun_number', 'KP-GRA', 'grammar', 'Noun number', '名词单复数', 'Choose singular or plural noun form.'),
('KP-GRA-COMPARATIVE', 'comparative_degree', 'KP-GRA', 'grammar', 'Comparative and superlative', '比较级与最高级', 'Choose positive, comparative, or superlative degree.'),
('KP-GRA-NEG-PREFIX', 'negative_prefix', 'KP-GRA-DERIV', 'grammar', 'Negative prefix', '否定前缀', 'Create a contextually required negative derivative.'),
('KP-GRA-PRONFORM', 'pronoun_form', 'KP-GRA-PRONOUN', 'grammar', 'Pronoun form', '代词词形', 'Choose person, case, number, reflexive, possessive, or indefinite form.'),
('KP-GRA-COORD', 'coordinating_conjunction', 'KP-GRA', 'grammar', 'Coordinating conjunctions', '并列连词', 'Connect coordinate words, phrases, or clauses.'),
('KP-GRA-CONNECTOR', 'connector_function_clause_completeness', 'KP-GRA', 'grammar', 'Connector function and clause completeness', '连接词功能与从句完整性', 'Choose a connector from clause function and missing sentence element.'),
('KP-GRA-EMPHASIS', 'emphasis', 'KP-GRA', 'grammar', 'Emphasis', '强调句', 'Cleft and related emphatic structures.'),
('KP-GRA-SUBJUNCTIVE', 'subjunctive', 'KP-GRA', 'grammar', 'Subjunctive mood', '虚拟语气', 'Subjunctive and counterfactual forms.'),
('KP-GRA-FIXED', 'fixed_structure', 'KP-GRA', 'grammar', 'Fixed structures', '固定句式', 'Grammar patterns that behave as fixed sentence structures.');

UPDATE knowledge_points
SET parent_id = 'KP-GRA-BACKBONE'
WHERE knowledge_point_id = 'KP-GRA-PRED';
