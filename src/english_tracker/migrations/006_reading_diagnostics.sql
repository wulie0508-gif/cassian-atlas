INSERT OR IGNORE INTO error_types(
    error_type_id, code, parent_id, label_en, label_cn, description
) VALUES
('ERR-READ', 'reading_error', 'ERR-METHOD', 'Reading diagnosis', '阅读诊断', '阅读理解中可由原始作答和证据链支持的错因总类。'),
('ERR-READ-LOCATE', 'reading_evidence_location_error', 'ERR-READ', 'Evidence location error', '证据定位错误', '未能定位题干对应的句段或定位到了错误证据。'),
('ERR-READ-SCOPE', 'reading_main_idea_scope_error', 'ERR-READ', 'Main-idea scope error', '主旨范围失当', '把局部细节当成全文主旨，或选项范围过大、过小。'),
('ERR-READ-INFER', 'reading_inference_overreach', 'ERR-READ', 'Inference overreach', '推断越界', '结论超出文本证据可支持的范围。'),
('ERR-READ-REFERENCE', 'reading_reference_resolution_error', 'ERR-READ', 'Reference resolution error', '指代识别错误', '对代词、替代表达或语篇照应的指向判断错误。'),
('ERR-READ-VOCAB', 'reading_vocabulary_context_error', 'ERR-READ', 'Vocabulary-in-context error', '语境词义误判', '忽略上下文限定，套用熟义或不恰当词义。'),
('ERR-READ-DISTRACTOR', 'reading_distractor_error', 'ERR-READ', 'Distractor trap', '干扰项误选', '被原词复现、偷换概念、无中生有或绝对化表述误导。'),
('ERR-READ-STEM', 'reading_question_stem_misread', 'ERR-READ', 'Question-stem misread', '审题失误', '忽略题干的否定、范围、对象或任务类型。'),
('ERR-READ-STRUCTURE', 'reading_text_structure_error', 'ERR-READ', 'Text-structure error', '篇章结构误判', '未能识别段落功能、信息组织或论证结构。'),
('ERR-READ-INTEGRATE', 'reading_information_integration_error', 'ERR-READ', 'Information integration error', '跨段整合失败', '未能整合多处证据完成比较、归纳或判断。'),
('ERR-READ-TIME', 'reading_time_allocation_error', 'ERR-READ', 'Time allocation error', '时间分配失当', '作答节奏导致证据检索或复核不充分；必须有过程观察支持。');

INSERT OR IGNORE INTO error_type_aliases(
    alias_normalized, raw_alias, error_type_id, source_system
) VALUES
('证据定位错误', '证据定位错误', 'ERR-READ-LOCATE', 'reading_diagnostics_v1'),
('主旨范围失当', '主旨范围失当', 'ERR-READ-SCOPE', 'reading_diagnostics_v1'),
('推断越界', '推断越界', 'ERR-READ-INFER', 'reading_diagnostics_v1'),
('指代识别错误', '指代识别错误', 'ERR-READ-REFERENCE', 'reading_diagnostics_v1'),
('语境词义误判', '语境词义误判', 'ERR-READ-VOCAB', 'reading_diagnostics_v1'),
('干扰项误选', '干扰项误选', 'ERR-READ-DISTRACTOR', 'reading_diagnostics_v1'),
('审题失误', '审题失误', 'ERR-READ-STEM', 'reading_diagnostics_v1'),
('篇章结构误判', '篇章结构误判', 'ERR-READ-STRUCTURE', 'reading_diagnostics_v1'),
('跨段整合失败', '跨段整合失败', 'ERR-READ-INTEGRATE', 'reading_diagnostics_v1'),
('时间分配失当', '时间分配失当', 'ERR-READ-TIME', 'reading_diagnostics_v1');
