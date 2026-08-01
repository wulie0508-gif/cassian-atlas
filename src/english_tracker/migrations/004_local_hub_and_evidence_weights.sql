CREATE TABLE assessment_weight_policies (
    assessment_kind TEXT NOT NULL CHECK (assessment_kind IN (
      'lesson', 'topic_quiz', 'biweekly_mixed_test', 'full_exam', 'dictation', 'homework', 'other'
    )),
    delivery_mode TEXT NOT NULL CHECK (delivery_mode IN (
      'offline_closed', 'offline_open', 'online', 'home', 'unspecified'
    )),
    evidence_weight REAL NOT NULL CHECK (evidence_weight > 0),
    is_calibration_anchor INTEGER NOT NULL DEFAULT 0 CHECK (is_calibration_anchor IN (0, 1)),
    rationale TEXT NOT NULL,
    policy_version TEXT NOT NULL DEFAULT 'evidence-v1',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(assessment_kind, delivery_mode, policy_version)
);

INSERT INTO assessment_weight_policies(
    assessment_kind, delivery_mode, evidence_weight, is_calibration_anchor,
    rationale, policy_version, updated_at
) VALUES
('full_exam','offline_closed',1.60,1,'正式线下闭卷最接近真实考试，是最高权重校准锚点。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('full_exam','offline_open',1.35,1,'正式线下开卷仍有较强环境控制，但弱于闭卷。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('full_exam','online',1.00,0,'线上整卷保留完整分数，但环境控制较弱。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('full_exam','home',0.80,0,'家庭整卷容易受到提示与暂停影响。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('full_exam','unspecified',1.10,0,'整卷但环境未知，使用保守中间权重。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('biweekly_mixed_test','offline_closed',1.40,1,'双周线下封闭混合测用于校准专题训练迁移。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('biweekly_mixed_test','offline_open',1.20,1,'线下混合测但允许查阅，校准力略降。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('biweekly_mixed_test','online',0.95,0,'线上混合测有综合性但环境控制有限。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('biweekly_mixed_test','home',0.75,0,'家庭混合测作为辅助证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('biweekly_mixed_test','unspecified',1.00,0,'混合测环境未知时不额外放大。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('topic_quiz','offline_closed',1.20,0,'线下闭卷专题小测可靠，但覆盖面小于混合测。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('topic_quiz','offline_open',1.05,0,'线下开卷专题测仍可用于诊断。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('topic_quiz','online',0.90,0,'线上专题测用于日常跟踪。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('topic_quiz','home',0.70,0,'家庭专题测只作为辅助证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('topic_quiz','unspecified',0.90,0,'专题测环境未知时采用常规权重。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('lesson','offline_closed',0.95,0,'课堂闭卷练习有即时诊断价值，但可能刚完成教学。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('lesson','offline_open',0.85,0,'课堂开放练习可能包含提示。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('lesson','online',0.75,0,'线上课堂练习作为形成性证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('lesson','home',0.60,0,'课后课堂任务环境控制较弱。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('lesson','unspecified',0.75,0,'普通课堂记录使用形成性权重。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('dictation','offline_closed',1.10,0,'线下闭卷听写直接测量主动提取。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('dictation','offline_open',0.95,0,'开放环境听写仍有主动产出证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('dictation','online',0.80,0,'线上听写用于高频跟踪。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('dictation','home',0.65,0,'家庭听写存在提示风险。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('dictation','unspecified',0.80,0,'听写环境未知时使用常规形成性权重。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('homework','offline_closed',0.80,0,'作业即便闭卷仍不是标准校准测。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('homework','offline_open',0.75,0,'作业通常允许查阅。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('homework','online',0.65,0,'线上作业作为辅助练习证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('homework','home',0.55,0,'家庭作业不用于高置信校准。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('homework','unspecified',0.65,0,'作业环境未知时保守计权。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('other','offline_closed',0.90,0,'未分类线下闭卷保留较高可信度。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('other','offline_open',0.85,0,'未分类线下开卷为中等证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('other','online',0.75,0,'未分类线上活动为形成性证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('other','home',0.60,0,'未分类家庭活动为辅助证据。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('other','unspecified',0.75,0,'信息不足时使用保守默认权重。','evidence-v1',strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE question_weight_rules (
    rule_id TEXT PRIMARY KEY,
    dimension TEXT NOT NULL CHECK (dimension IN ('difficulty', 'verification_status', 'answer_capture_status')),
    match_value TEXT NOT NULL,
    multiplier REAL NOT NULL CHECK (multiplier > 0),
    rationale TEXT NOT NULL,
    policy_version TEXT NOT NULL DEFAULT 'evidence-v1',
    UNIQUE(dimension, match_value, policy_version)
);

INSERT INTO question_weight_rules(rule_id,dimension,match_value,multiplier,rationale) VALUES
('QW-DIFF-FOUNDATION','difficulty','基础',0.90,'基础题错误反映底层缺口，但单题区分度略低。'),
('QW-DIFF-MEDIUM','difficulty','中等',1.00,'中等题作为默认区分度。'),
('QW-DIFF-HIGH','difficulty','中高',1.10,'较难题提高少量区分度，避免难题一票否决。'),
('QW-DIFF-HARD','difficulty','高难',1.15,'高难题仅温和放大，控制噪声。'),
('QW-VERIFY-VERIFIED','verification_status','verified',1.00,'人工核验题可完整计权。'),
('QW-VERIFY-SOURCE','verification_status','source_checked',1.00,'来源核对题可完整计权。'),
('QW-VERIFY-OCR','verification_status','ocr_only',0.65,'纯OCR题在回看原页前只作低权重证据。'),
('QW-VERIFY-INCOMPLETE','verification_status','incomplete',0.50,'题干或答案不完整时显著降权。'),
('QW-VERIFY-CONFLICT','verification_status','conflict',0.35,'答案冲突题只用于定位复核。'),
('QW-CAPTURE-CAPTURED','answer_capture_status','captured',1.00,'保存原始答案时可分析作答与错因。'),
('QW-CAPTURE-BLANK','answer_capture_status','captured_blank',1.00,'确认空白也是完整作答证据。'),
('QW-CAPTURE-NOT','answer_capture_status','not_captured',0.85,'只保留对错证据，不能诊断具体错因。'),
('QW-CAPTURE-LEGACY','answer_capture_status','unknown_legacy',0.70,'旧记录的作答捕获状态不明。');

INSERT OR IGNORE INTO knowledge_points(
    knowledge_point_id,code,parent_id,domain,name_en,name_cn,description
) VALUES
('KP-READ-PURPOSE','reading_purpose_attitude','KP-READ','reading','Purpose and attitude','写作目的与态度','判断作者、人物或材料的目的、立场与语气。'),
('KP-READ-STRUCTURE','reading_text_structure','KP-READ','reading','Text structure','篇章结构','识别段落功能、信息组织和论证结构。'),
('KP-READ-REFERENCE','reading_reference','KP-READ','reading','Reference resolution','指代关系','定位代词、替代表达及其语篇指向。'),
('KP-READ-SENTENCE','reading_sentence_in_context','KP-READ','reading','Sentence in context','长难句与语境释义','结合句法和上下文解释句意。'),
('KP-READ-INTEGRATE','reading_information_integration','KP-READ','reading','Information integration','跨段信息整合','整合多处证据完成比较、归纳与推断。'),
('KP-READ-RHETORIC','reading_rhetorical_function','KP-READ','reading','Rhetorical function','修辞与例证功能','判断举例、对比、引用等手段的作用。'),
('KP-CLOZE','cloze','KP-READ','reading','Cloze','完形填空','在篇章语境中综合判断词义、搭配和逻辑。'),
('KP-CLOZE-SEM','cloze_context_semantics','KP-CLOZE','reading','Context semantics','完形语境词义','依据上下文语义选择词项。'),
('KP-CLOZE-COLLOC','cloze_collocation','KP-CLOZE','reading','Cloze collocation','完形固定搭配','依据搭配和惯用表达选择。'),
('KP-CLOZE-LOGIC','cloze_logic','KP-CLOZE','reading','Cloze logic','完形逻辑关系','识别因果、转折、递进、让步等逻辑。'),
('KP-CLOZE-COHESION','cloze_cohesion','KP-CLOZE','reading','Cloze cohesion','完形篇章衔接','利用照应、复现和段落推进判断。'),
('KP-LISTEN','listening',NULL,'listening','Listening','听力','听取并处理英语口语信息。'),
('KP-LISTEN-GIST','listening_gist','KP-LISTEN','listening','Listening gist','听力主旨','提取对话或独白主旨。'),
('KP-LISTEN-DETAIL','listening_detail','KP-LISTEN','listening','Listening detail','听力细节','定位人物、地点、时间、数字和事实。'),
('KP-LISTEN-INFER','listening_inference','KP-LISTEN','listening','Listening inference','听力推断','根据语气和上下文推断关系、意图和结论。'),
('KP-LISTEN-ATTITUDE','listening_attitude','KP-LISTEN','listening','Speaker attitude','说话者态度','判断态度、情绪和观点。'),
('KP-TRANS-LEX','translation_lexical_choice','KP-TRANS','translation','Lexical choice','翻译词汇选择','选择准确、合乎搭配的英文表达。'),
('KP-TRANS-GRAMMAR','translation_grammar_accuracy','KP-TRANS','translation','Grammar accuracy','翻译语法准确性','控制时态、语态、主谓一致和非谓语等。'),
('KP-TRANS-IDIOM','translation_idiomaticity','KP-TRANS','translation','Idiomaticity','翻译地道性','避免逐词直译并使用自然表达。'),
('KP-TRANS-COMPLEX','translation_clause_complexity','KP-TRANS','translation','Clause complexity','翻译复杂句组织','组织从句、并列和信息焦点。'),
('KP-WRITE-ARG','writing_argument','KP-WRITE','writing','Argument','写作论点','形成明确观点并持续回应任务。'),
('KP-WRITE-EVIDENCE','writing_evidence','KP-WRITE','writing','Evidence','写作论据','使用具体理由、例子和解释支持观点。'),
('KP-WRITE-COHESION','writing_cohesion','KP-WRITE','writing','Cohesion','写作衔接','使用指代、连接和主题推进保持连贯。'),
('KP-WRITE-VARIETY','writing_sentence_variety','KP-WRITE-LANG','writing','Sentence variety','句式多样性','控制简单句、复合句和强调结构的组合。'),
('KP-WRITE-ACCURACY','writing_language_accuracy','KP-WRITE-LANG','writing','Language accuracy','语言准确性','减少词汇、语法、拼写和标点错误。'),
('KP-WRITE-REGISTER','writing_register','KP-WRITE-LANG','writing','Register','语域与得体性','根据对象与体裁控制正式度和语气。'),
('KP-VOC-MEANING','vocabulary_meaning','KP-VOC','vocabulary','Word meaning','词义','掌握核心义、语境义和一词多义。'),
('KP-VOC-COLLOC','vocabulary_collocation','KP-VOC','vocabulary','Vocabulary collocation','词汇搭配','掌握动宾、形名和介词搭配。'),
('KP-VOC-USAGE','vocabulary_usage','KP-VOC','vocabulary','Vocabulary usage','词汇用法','掌握句法框架、语域和常见限制。'),
('KP-VOC-PRON','vocabulary_pronunciation','KP-VOC','vocabulary','Pronunciation','发音辨识','把音形义关联到可提取的词汇表征。');

CREATE TABLE library_resources (
    resource_id TEXT PRIMARY KEY,
    library_key TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    media_kind TEXT NOT NULL CHECK (media_kind IN ('document', 'pdf', 'audio', 'image', 'data', 'other')),
    subject_scope TEXT NOT NULL CHECK (subject_scope IN ('english', 'non_english', 'unknown')),
    source_group TEXT NOT NULL,
    year_hint INTEGER,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    modified_at TEXT NOT NULL,
    sha256 TEXT,
    duplicate_of_resource_id TEXT,
    is_canonical INTEGER NOT NULL DEFAULT 1 CHECK (is_canonical IN (0, 1)),
    parse_status TEXT NOT NULL CHECK (parse_status IN (
      'indexed', 'queued', 'extracting', 'extracted', 'structured', 'ingested',
      'needs_ocr', 'needs_conversion', 'needs_review', 'excluded_non_english', 'failed'
    )),
    extraction_method TEXT,
    extracted_text_path TEXT,
    extracted_char_count INTEGER CHECK (extracted_char_count IS NULL OR extracted_char_count >= 0),
    page_count INTEGER CHECK (page_count IS NULL OR page_count >= 0),
    question_count INTEGER NOT NULL DEFAULT 0 CHECK (question_count >= 0),
    passage_count INTEGER NOT NULL DEFAULT 0 CHECK (passage_count >= 0),
    knowledge_mapping_count INTEGER NOT NULL DEFAULT 0 CHECK (knowledge_mapping_count >= 0),
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN (
      'verified', 'source_checked', 'ocr_only', 'unverified', 'needs_check', 'rejected'
    )),
    last_error TEXT,
    indexed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(library_key, relative_path)
);

CREATE INDEX idx_library_resources_scope_status
ON library_resources(subject_scope, parse_status, extension);

CREATE INDEX idx_library_resources_hash
ON library_resources(sha256) WHERE sha256 IS NOT NULL;

CREATE TABLE library_parse_runs (
    parse_run_id TEXT PRIMARY KEY,
    library_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('inventory', 'hash', 'extract', 'structure', 'enrich', 'full')),
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'completed_with_errors', 'failed', 'cancelled')),
    total_resources INTEGER NOT NULL DEFAULT 0 CHECK (total_resources >= 0),
    processed_resources INTEGER NOT NULL DEFAULT 0 CHECK (processed_resources >= 0),
    successful_resources INTEGER NOT NULL DEFAULT 0 CHECK (successful_resources >= 0),
    failed_resources INTEGER NOT NULL DEFAULT 0 CHECK (failed_resources >= 0),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    options_json TEXT NOT NULL,
    summary_json TEXT,
    log_path TEXT
);

CREATE TABLE library_parse_events (
    parse_event_id TEXT PRIMARY KEY,
    parse_run_id TEXT NOT NULL REFERENCES library_parse_runs(parse_run_id),
    resource_id TEXT REFERENCES library_resources(resource_id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'skipped', 'failed', 'needs_review')),
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_library_parse_events_run
ON library_parse_events(parse_run_id, status, created_at);

CREATE TABLE question_enrichments (
    question_id TEXT NOT NULL,
    enrichment_type TEXT NOT NULL CHECK (enrichment_type IN (
      'knowledge_detail', 'grammar_structure', 'solving_method', 'trap_analysis',
      'prerequisite', 'rag_chunk', 'answer_reasoning'
    )),
    enrichment_key TEXT NOT NULL,
    content_json TEXT NOT NULL,
    mapping_source TEXT NOT NULL CHECK (mapping_source IN ('legacy', 'rule', 'model_suggested', 'manual')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_status TEXT NOT NULL CHECK (verification_status IN (
      'suggested', 'source_checked', 'verified', 'needs_check', 'rejected'
    )),
    source_snapshot_id TEXT REFERENCES source_snapshots(source_snapshot_id),
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(question_id, enrichment_type, enrichment_key),
    CHECK (NOT (mapping_source = 'model_suggested' AND verification_status IN ('source_checked', 'verified')))
);

CREATE TABLE knowledge_search_documents (
    search_document_id TEXT PRIMARY KEY,
    question_id TEXT,
    passage_id TEXT,
    document_type TEXT NOT NULL CHECK (document_type IN (
      'question', 'passage', 'teaching_method', 'knowledge_point', 'source'
    )),
    title TEXT NOT NULL,
    search_text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    mapping_source TEXT NOT NULL CHECK (mapping_source IN ('legacy', 'rule', 'model_suggested', 'manual')),
    verification_status TEXT NOT NULL CHECK (verification_status IN (
      'suggested', 'source_checked', 'verified', 'needs_check', 'rejected'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_knowledge_search_question
ON knowledge_search_documents(question_id, document_type);

CREATE TABLE question_deep_knowledge_map (
    question_id TEXT NOT NULL,
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
    PRIMARY KEY(question_id,knowledge_point_id,role),
    CHECK (NOT (mapping_source = 'model_suggested' AND verification_status IN ('source_checked', 'verified')))
);

CREATE INDEX idx_question_deep_knowledge
ON question_deep_knowledge_map(knowledge_point_id,verification_status,question_id);

CREATE TABLE workflow_channels (
    channel_key TEXT PRIMARY KEY CHECK (channel_key IN ('engineering', 'courseware', 'dictation')),
    display_name TEXT NOT NULL,
    responsibility TEXT NOT NULL,
    reads_from TEXT NOT NULL,
    writes_through TEXT NOT NULL,
    handoff_document TEXT,
    context_endpoint TEXT NOT NULL,
    last_activity_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('ready', 'attention', 'blocked')),
    notes TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO workflow_channels(
    channel_key,display_name,responsibility,reads_from,writes_through,handoff_document,
    context_endpoint,status,notes,updated_at
) VALUES
('engineering','工程与数据','数据库架构、迁移、解析、接口、质量检查和本地站点。','统一数据库、题库和解析台账','CLI/API；禁止绕过审计直接写事实表','HANDOFF_FOR_THREADS.md','/api/context/engineering','ready','负责维护结构和可复现工具。',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('courseware','课件生成','读取薄弱点、完整语篇、教学方法与知识覆盖，生成课堂材料。','课件上下文、题库只读接口、自动选题接口','课堂结束后批量写入session与attempts','HANDOFF_FOR_THREADS.md','/api/context/courseware','ready','规则或模型建议标签不能冒充人工核验。',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('dictation','单词听写','生成听写清单、保存原始作答、自动批改并安排复测。','听写上下文、到期复习队列','听写session与attempts导入接口','HANDOFF_FOR_THREADS.md','/api/context/dictation','ready','OCR/API生产者后续接入同一写入契约。',strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE project_work_items (
    work_item_id TEXT PRIMARY KEY,
    area TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'in_progress', 'completed', 'blocked', 'needs_review')),
    completed_units INTEGER NOT NULL DEFAULT 0 CHECK (completed_units >= 0),
    total_units INTEGER NOT NULL DEFAULT 0 CHECK (total_units >= 0),
    unit_label TEXT NOT NULL DEFAULT '项',
    evidence_path TEXT,
    blocker TEXT,
    owner_channel TEXT REFERENCES workflow_channels(channel_key),
    updated_at TEXT NOT NULL
);

INSERT INTO project_work_items(
    work_item_id,area,title,status,completed_units,total_units,unit_label,evidence_path,owner_channel,updated_at
) VALUES
('WORK-UNIFIED-DB','工程','统一学习数据库与审计接口','completed',1,1,'套','HANDOFF_FOR_THREADS.md','engineering',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('WORK-GRAMMAR-COVERAGE','题库','语法填空知识树与完整语篇覆盖','completed',1249,1249,'题','exports/grammar_catalog_sync_report.json','engineering',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('WORK-LOCAL-HUB','工程','本地学习管理站','in_progress',0,1,'套',NULL,'engineering',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('WORK-FULL-PARSE','解析','英语资料全量解析与深层知识索引','in_progress',0,0,'文件',NULL,'engineering',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
('WORK-OCR-INTEGRATION','听写','识图解析API接入主体','planned',0,1,'套',NULL,'dictation',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
