# Data Dictionary

## Identity, activity, and materials

| Table | Grain | Important fields |
| --- | --- | --- |
| `students` | One anonymous learner | `student_id`, private `display_name`, timezone, target retention |
| `learning_sessions` | One class, dictation, homework, practice, or test activity | source thread, type, start/end, planned/completed counts, status |
| `artifacts` | One material used by a session | type, title, private path/URI, SHA-256, verification status |
| `session_observations` | One narrative observation | evidence level prevents a general observation from becoming an item error |
| `session_progress` | One content segment within a session | label, optional external ID, completed/not-started state, counts |

## Content and external references

| Table | Grain | Important fields |
| --- | --- | --- |
| `content_items` | One actually used question, word, phrase, sentence, or writing task | domain, item type, prompt/answer snapshot, response mode, difficulty, validation status |
| `external_references` | One stable source ID linked to one item | namespace, reference type, external ID, optional passage/source parent, source metadata |
| `knowledge_points` | One hierarchical skill/knowledge node | stable `code`, parent, English/Chinese name, domain |
| `item_knowledge_map` | One item-to-knowledge relation | primary/secondary/inferred role, weight, evidence source, validation status |

External source namespaces currently include `legacy_review`, `mastery_json`, `victor_vocab`, and `shanghai_question_bank`. The student database never copies the full external database.

## Attempts and evaluation

| Table | Grain | Important fields |
| --- | --- | --- |
| `attempts` | One immutable real response event | global `event_id`, student/session/item, time, raw answer, standard-answer snapshot, capture status, first/review phase, validation, record status |
| `evaluations` | One evaluation revision | correct/partial/wrong/needs_check, score, evaluator, current flag, superseded revision |
| `error_types` | One canonical error category | stable code and bilingual labels |
| `error_type_aliases` | One historical or producer alias | normalized alias, original alias, canonical error type |
| `attempt_error_map` | One attempt-to-error relation | canonical type, retained `raw_error_type`, confidence, note |

`answer_capture_status` values:

- `captured`: a non-empty answer was saved.
- `captured_blank`: the response was explicitly blank.
- `not_captured`: the outcome is known but the raw answer was not saved.
- `unknown_legacy`: old provenance cannot distinguish the capture state.

An SQL `NULL` answer never means “blank” by itself.

## Review and mastery

| Table | Grain | Important fields |
| --- | --- | --- |
| `review_state` | One student/item scheduling state | due time, interval, stability/difficulty, repetitions/lapses, consecutive errors, last result, algorithm/version, manual override |
| `review_tasks` | One actionable review request | source attempt, reason, due time, priority, status, completing attempt |
| `mastery_snapshots` | One optional reproducible profile snapshot | as-of time, window, JSON result, algorithm version |

The authoritative evidence remains attempts and evaluation revisions. Review state and snapshots are derived and can be rebuilt.

## Ingestion, migration, and audit

| Table | Grain | Important fields |
| --- | --- | --- |
| `ingest_events` | One batch envelope | idempotency key, payload hash, source thread, status, counts, backup path |
| `ingest_event_rows` | One entity action caused by an ingest event | entity type/ID, insert/link/supersede/void/skip, before/after JSON |
| `audit_log` | One operator or system correction action | actor, action, entity, before/after, reason |
| `legacy_records` | One preserved old row or JSON sub-record | source system, record type/key, raw JSON, target mapping, migration status |
| `schema_migrations` | One applied SQL migration | version and time |

## Canonical knowledge-point codes

The initial taxonomy covers:

- Vocabulary: `active_recall`, `spelling`, `fixed_phrase`, `word_form`, `word_family`, `near_synonym`.
- Grammar: `predicate_vs_non_predicate`, `tense`, `voice`, `subject_verb_agreement`, `non_finite`, `noun_clause`, `relative_clause`, `adverbial_clause`, `article`, `pronoun`, `preposition_collocation`, `inversion`.
- Reading: main idea, detail, inference, vocabulary in context.
- Translation: sentence structure.
- Writing: task fulfillment, organization, language.

## Canonical error codes

`knowledge_gap`, `method_gap`, `active_recall_failure`, `spelling_error`, `fixed_phrase_missing`, `near_synonym_substitution`, `word_form_error`, `word_family_confusion`, `tense_voice_confusion`, `non_finite_error`, `clause_connector_error`, `inversion_error`, `sentence_structure_error`, `source_wording_mismatch`, and `needs_check`.

