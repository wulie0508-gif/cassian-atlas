# Data Dictionary

## Identity, activity, and materials

| Table | Grain | Important fields |
| --- | --- | --- |
| `students` | One anonymous learner | `student_id`, private `display_name`, timezone, target retention |
| `subjects` | One registered subject adapter | stable `subject_code`, bilingual names, `ready/generic` adapter status |
| `student_subjects` | One learner-to-subject enrollment | active flag and first enrollment time; evidence import activates the relation automatically |
| `learning_sessions` | One class, dictation, homework, practice, or test activity | source thread, type, start/end, planned/completed counts, status |
| `artifacts` | One material used by a session | type, title, private path/URI, SHA-256, verification status |
| `session_observations` | One narrative observation | evidence level prevents a general observation from becoming an item error |
| `session_progress` | One content segment within a session | label, optional external ID, completed/not-started state, counts |
| `session_assessments` | One optional measurement classification per session | assessment kind, reporting series, delivery mode, raw/max score, duration, blank count, validation status |

## Content and external references

| Table | Grain | Important fields |
| --- | --- | --- |
| `content_items` | One actually used question, word, phrase, sentence, or writing task | `subject_code`, domain, item type, prompt/answer snapshot, response mode, difficulty, validation status |
| `external_references` | One stable source ID linked to one item | namespace, reference type, external ID, optional passage/source parent, source metadata |
| `knowledge_points` | One hierarchical skill/knowledge node | stable `code`, parent, English/Chinese name, domain |
| `item_knowledge_map` | One used-item-to-knowledge relation | primary/secondary/prerequisite/trap role, mapping source, confidence, verification status, rationale, source snapshot |
| `source_snapshots` | One immutable external-source/filter version | namespace, URI, SHA-256, size, filter, question/passage counts, current flag |
| `grammar_passage_catalog` | One grammar passage in a source snapshot | stable passage/source IDs, title metadata, question counts, complete-passage gate |
| `grammar_question_catalog` | One source-checked grammar question metadata row | stable IDs, original number, source descriptors, raw/normalized legacy tags, answer/explanation availability; no stem or answer text |
| `question_knowledge_map` | One question-to-knowledge relation | role, `legacy/rule/model_suggested/manual`, confidence, verification, rationale, snapshot ID |

External source namespaces currently include `legacy_review`, `mastery_json`, `victor_vocab`, and `shanghai_question_bank`. The student database never copies the full external database.

## Attempts and evaluation

| Table | Grain | Important fields |
| --- | --- | --- |
| `attempts` | One immutable real response event | global `event_id`, student/session/item, time, raw answer, standard-answer snapshot, capture status, first/review phase, validation, record status |
| `evaluations` | One evaluation revision | correct/partial/wrong/needs_check, score, evaluator, current flag, superseded revision |
| `error_types` | One canonical error category | stable code and bilingual labels |
| `error_type_aliases` | One historical or producer alias | normalized alias, original alias, canonical error type |
| `attempt_error_map` | One attempt-to-error-cause relation | canonical type, retained raw label, source, confidence, verification, rationale, active/voided state |

`answer_capture_status` values:

- `captured`: a non-empty answer was saved.
- `captured_blank`: the response was explicitly blank.
- `not_captured`: the outcome is known but the raw answer was not saved.
- `unknown_legacy`: old provenance cannot distinguish the capture state.

An SQL `NULL` answer never means “blank” by itself.

Question knowledge coverage and student error causes are different grains. A wrong result can contribute performance evidence to the mapped knowledge point, but it does not establish a specific error cause. When `answer_capture_status=not_captured`, active `attempt_error_map` rows are prohibited; historical inferred rows are retained as `voided/rejected` audit evidence.

`question_knowledge_map.role`: `primary`, `secondary`, `prerequisite`, `trap`. `mapping_source`: `legacy`, `rule`, `model_suggested`, `manual`. A database constraint prevents `model_suggested` mappings from being stored as `source_checked` or `verified`.

## Review and mastery

| Table | Grain | Important fields |
| --- | --- | --- |
| `review_state` | One student/item scheduling state | due time, interval, stability/difficulty, repetitions/lapses, consecutive errors, last result, algorithm/version, manual override |
| `review_tasks` | One actionable review request | source attempt, reason, due time, priority, status, completing attempt |
| `mastery_snapshots` | One optional reproducible profile snapshot | as-of time, window, JSON result, algorithm version |

The authoritative evidence remains attempts and evaluation revisions. Review state and snapshots are derived and can be rebuilt.

## Assessment calibration and project management

| Table | Grain | Important fields |
| --- | --- | --- |
| `assessment_weight_policies` | One assessment-kind and delivery-mode weight | policy version, evidence weight, calibration-anchor flag and rationale |
| `question_weight_rules` | One deterministic item/evidence multiplier rule | dimension, match value, multiplier, priority and rationale |
| `workflow_channels` | One engineering/courseware/dictation contract | read source, write contract, context endpoint, current status |
| `project_work_items` | One tracked deliverable | owner, status, completed/total units, evidence path and blocker |

The effective attempt weight multiplies assessment environment, question difficulty, verification quality and answer-capture quality, then clamps the product to the published safe range. It never invents extra attempts.

## Source-library parsing and staging

| Table | Grain | Important fields |
| --- | --- | --- |
| `library_resources` | One physical source file | path, hash/duplicate lineage, subject scope, parse status, extraction cache and source verification |
| `library_parse_runs` | One inventory/extract/OCR/structure run | mode, status, counts, options, summary and timing |
| `library_source_sets` | One logical paper/book and its prompt/answer/audio variants | pairing status, preferred resources, candidate/chunk counts |
| `library_source_set_resources` | One file membership in a logical source set | role, preferred flag and deterministic grouping rationale |
| `library_text_chunks` | One provenance-preserving RAG chunk | heading, text, source locator, parser version and verification status |
| `staged_passages` | One machine-split passage candidate | source set, text, question range/count, confidence and review status |
| `staged_questions` | One machine-split question candidate | passage, type, stem/options/answer/explanation, source locator, confidence and review status |
| `staged_question_knowledge_map` | One staged question-to-knowledge suggestion | role, mapping source, confidence, rationale and source snapshot |
| `library_structure_reviews` | One unresolved/resolved structural issue | target resource/question, problem type, severity, detail and source locator |

Staging rows are queryable for discovery but are not attempts and are not the verified question bank. `mapping_source=rule` or `model_suggested` stays unverified until an explicit human action.

## Ingestion, migration, and audit

| Table | Grain | Important fields |
| --- | --- | --- |
| `ingest_events` | One batch envelope | idempotency key, payload hash, source thread, status, counts, backup path |
| `ingest_event_rows` | One entity action caused by an ingest event | entity type/ID, insert/link/supersede/void/skip, before/after JSON |
| `audit_log` | One operator or system correction action | actor, action, entity, before/after, reason |
| `legacy_records` | One preserved old row or JSON sub-record | source system, record type/key, raw JSON, target mapping, migration status |
| `schema_migrations` | One applied SQL migration | version and time |
| `passage_selection_runs` | Optional audit record for a materialized selection run | target knowledge JSON, student/error window, algorithm, result JSON |

## Canonical knowledge-point codes

The initial taxonomy covers:

- Vocabulary: `active_recall`, `spelling`, `fixed_phrase`, `word_form`, `word_family`, `near_synonym`.
- Grammar backbone and finite verbs: `sentence_backbone`, `predicate_count`, `predicate_vs_non_predicate`, `tense`, `voice`, `passive_voice`, `subject_verb_agreement`, `modal_verb`.
- Non-finite grammar: `non_finite`, `infinitive`, `gerund`, `participle`, `present_participle`, `past_participle`, `non_finite_logical_subject`, `non_finite_voice`, `non_finite_sequence`.
- Derivation and form: `word_derivation`, `noun_derivation`, `adjective_derivation`, `adverb_derivation`, `noun_number`, `comparative_degree`, `negative_prefix`, `pronoun_form`.
- Function words and clauses: `article`, `pronoun`, `preposition_collocation`, `coordinating_conjunction`, `relative_clause`, `noun_clause`, `adverbial_clause`, `connector_function_clause_completeness`.
- Special structures: `inversion`, `emphasis`, `subjunctive`, `fixed_structure`.
- Reading: main idea, detail, inference, vocabulary in context.
- Translation: sentence structure.
- Writing: task fulfillment, organization, language.

## Canonical error codes

`knowledge_gap`, `method_gap`, `active_recall_failure`, `spelling_error`, `fixed_phrase_missing`, `near_synonym_substitution`, `word_form_error`, `word_family_confusion`, `tense_voice_confusion`, `non_finite_error`, `clause_connector_error`, `inversion_error`, `sentence_structure_error`, `source_wording_mismatch`, and `needs_check`.
