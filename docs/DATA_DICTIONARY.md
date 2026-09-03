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

## Candidate extraction and teacher confirmation

| Table | Grain | Important fields |
| --- | --- | --- |
| `extraction_batches` | One learner/session-owned extraction and review batch | request/idempotency hashes, source thread, expected item count, optimistic `review_version`, draft/review/commit status, committed ingest event |
| `extraction_assets` | One immutable source image or page reference in a batch | private source URI, SHA-256, media type, byte size, page number |
| `extraction_items` | One answer region that must appear in the batch review | ordinal, question reference/type, `R0`-`R4` risk, second-model requirement/reason, evidence locator, answer-free attempt template hash |
| `extraction_provider_results` | One append-only provider candidate for one item | exact provider (`codex`, `doubao`, or `deterministic`), model/prompt/request/response identity, raw/normalized transcription, capture state, uncertainty, alternatives, confidence, error state |
| `extraction_confirmation_decisions` | One append-only teacher decision revision for one item | review/revision number, terminal or blocking action, confirmed text, selected provider result, evaluation, actor, reason |
| `extraction_commit_links` | One committed candidate-to-fact lineage row | item and decision, resulting attempt/evaluation, ingest event, commit time |

These tables are a pre-fact audit layer. Provider agreement, a review prefill, and a partial decision submission are not attempts. Every item must have a current terminal teacher decision before the batch can commit; `pending_review` and `needs_check` block the whole batch. `not_captured` and `rejected_alignment` are terminal audited exclusions and create no attempt. `human_confirmed`, `human_corrected`, and `confirmed_blank` are the only committable actions.

Cold-start `R1`, `R2`, and `R3` items require successful independent `codex` and `doubao` rows. This cannot be satisfied by two results from the same provider or by `deterministic + codex`. `R0` deterministic capture may use one provider but still requires teacher confirmation; `R4` is manual or excluded. Unconfirmed rows are not inputs to mastery, weakness, error diagnosis, or review scheduling.

## Review and mastery

| Table | Grain | Important fields |
| --- | --- | --- |
| `review_state` | One student/item scheduling state | due time, interval, stability/difficulty, repetitions/lapses, consecutive errors, last result, algorithm/version, manual override |
| `review_tasks` | One actionable review request | source attempt, reason, due time, priority, status, completing attempt |
| `mastery_snapshots` | One optional reproducible profile snapshot | as-of time, window, JSON result, algorithm version |

The authoritative evidence remains attempts and evaluation revisions. Review state and snapshots are derived and can be rebuilt.

## Operational Base projection

| Table | Grain | Important fields |
| --- | --- | --- |
| `base_projection_runs` | One learner/subject/view delivery bundle | Cassian target fingerprint, projection name, data-as-of time, payload hash, record count, publisher, staged/publishing/retry/terminal state |
| `base_projection_outbox` | One immutable whitelisted operational record | stable upsert key, flat payload and hash, learner/subject owner, attempt count, retry time, failure class, delivery state |
| `base_projection_delivery_attempts` | One append-only sanitized publisher receipt | idempotency/result hashes, attempt number, success/retry/permanent-failure outcome, remote record ID, readback payload hash, failure category/code |
| `base_projection_state` | One last successfully read-back remote record per stable upsert key | learner/subject/view identity, remote record ID, last payload hash, cited outbox and delivery attempt, first/last publication time |

The only projection names are `student_overview`, `period_metrics`, `knowledge_performance`, `retest_summary`, `data_quality`, `generation_runs`, and `teacher_policy_correction_inbox`. Every payload uses an exact field whitelist and carries `data_as_of`, `metric_version`, `sample_size`, and freshness `FRESH`, `DELAYED`, `STALE`, or `FAILED`. Question content, answers, explanations, OCR/model output, learner names, private paths, URLs, and credentials are outside this schema.

Target validation is local and fail-closed for `Cassian Learning Lab | 学习工作室`, app `Cassian Learning Ops`, CLI profile `cassian-learning-hub`, identity `user`, and the requested learner's exact same-tenant folder/Base pair. Tokens and URLs are not stored in these tables. A successful delivery is accepted only when its readback SHA-256 equals the staged payload and its remote record ID agrees with prior state. Version 0.5.0 includes no live Feishu transport; `claim` and `receipt` are publisher handshakes, not proof that a cloud write occurred by themselves.

## Verified question selection and public explanations

| Table | Grain | Important fields |
| --- | --- | --- |
| `question_selection_manifests` | One immutable learner-owned selection request/result | source namespace and snapshot hash, training mode, algorithm/policy versions, target knowledge, selected/excluded counts, coverage, finalization time |
| `question_selection_groups` | One selected standalone item or complete passage | source locator, source/content hashes, expected and selected counts, completeness, reasons, knowledge/evidence references, duplicate result |
| `question_selection_items` | One verified real question within a selected group | stable source/question/passage IDs, content and answer hashes, verification status, mapping evidence, expected public explanation cache key/status |
| `question_selection_exclusions` | One auditable rejected candidate/reason | unknown/unverified/incomplete/not-real/duplicate/limit reason, matched item, similarity, source locator and detail |
| `public_question_explanations` | One learner-free reusable explanation identity and immutable content row | deterministic cache key; source/question/answer/mapping/rubric/policy/schema hashes; review status; creator/confirmer; supersession and invalidation |

Selection opens the external question bank read-only, includes only verified real sources, expands passage questions to their complete verified sibling group, and checks exact/near duplicates against the current manifest and recent learner history. Exact historical retests require explicit correction-mode policy. Finalized manifests preserve selections, exclusions, reasons, evidence references, and coverage; they are not attempts and do not by themselves update mastery.

`public_question_explanations` deliberately has no `student_id`, attempt, answer-submission, or diagnosis foreign key. Only `source_checked` or `teacher_confirmed` entries are reusable. A source, question, answer, knowledge mapping, rubric, policy, or schema change produces a different cache identity and makes superseded content stale instead of silently reusing it.

## Assessment calibration and project management

| Table | Grain | Important fields |
| --- | --- | --- |
| `assessment_weight_policies` | One assessment-kind and delivery-mode weight | policy version, evidence weight, calibration-anchor flag and rationale |
| `question_weight_rules` | One deterministic item/evidence multiplier rule | dimension, match value, multiplier, priority and rationale |
| `workflow_channels` | One engineering/courseware/dictation contract | read source, write contract, context endpoint, current status |
| `project_work_items` | One tracked deliverable | owner, status, completed/total units, evidence path and blocker |
| `agent_runs` | One routed user task | idempotency key, learner/subject scope, request hash/excerpt, selected route JSON, primary capability, status, summary and durable result reference |
| `agent_run_events` | One append-only operational event within a run | globally unique retry key, sequence, capability, actor, planned/started/progress/terminal event type, message, optional payload and timestamp |
| `artifact_generation_runs` | One source-bound generated-material lifecycle | learner/subject owner, idempotency key, immutable source snapshot and hash, planned/running/terminal state, output artifact/path/hash, stale reason and timestamps |

The effective attempt weight multiplies assessment environment, question difficulty, verification quality and answer-capture quality, then clamps the product to the published safe range. It never invents extra attempts.

`agent_runs`, `agent_run_events`, and `artifact_generation_runs` are operational metadata only. They support the website's automation dashboard and cross-conversation continuity but are never input rows for accuracy, weakness, mastery, trends, or review scheduling. A new learner-evidence import can mark a completed generation stale; it does not mutate or delete the generated file.

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
| `schema_migrations` | One applied SQL migration | version, applied time and packaged SQL checksum |
| `passage_selection_runs` | Optional audit record for a materialized selection run | target knowledge JSON, student/error window, algorithm, result JSON |

Schema `012` adds the extraction-confirmation boundary, `013` adds the local Base projection ledger, and `014` adds verified selection manifests and public explanations. Ordinary commands require migration status `ready`; `opentutor upgrade` applies packaged pending migrations, while checksum mismatches or unknown applied versions stop for investigation.

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
