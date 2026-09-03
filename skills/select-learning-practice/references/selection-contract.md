# Selection contract

## Verified question manifest

```json
{
  "student_id": "STU-001",
  "subject_code": "english",
  "training_mode": "transfer",
  "data_as_of": "2026-09-02T09:00:00+08:00",
  "candidate_question_ids": ["Q-001", "Q-002"],
  "candidate_context": {
    "Q-001": {
      "reason_codes": ["same_skill_new_item"],
      "knowledge_codes": ["tense"],
      "evidence_references": [
        {"entity_type": "knowledge_evidence", "entity_id": "KEV-001", "as_of": "2026-09-02T09:00:00+08:00"}
      ],
      "priority": 10
    }
  },
  "target_knowledge_codes": ["tense"],
  "max_questions": 10,
  "max_groups": 5,
  "duplicate_window_days": 30,
  "near_duplicate_threshold": 0.92,
  "allow_exact_retests": false,
  "idempotency_key": "selection:STU-001:2026-09-02:transfer:v1"
}
```

Only documented top-level and candidate-context fields are accepted. Selection opens the configured question bank read-only, hashes its snapshot, expands passage questions as one complete group, records every exclusion, and finalizes an immutable learner-owned manifest. `recent_question_ids` and `recent_passage_ids` are explicit inputs and part of the idempotent request identity.

`allow_exact_retests=true` is valid only in `correction` mode and only for exact items found in the supplied or durable recent learner history. It never permits duplicates inside the new manifest.

## Public explanation reuse

Lookup uses the current source snapshot, question and answer hashes, verified knowledge mapping, rubric, policy, and schema versions. Only `source_checked` and `teacher_confirmed` entries are reusable. The cache has no learner foreign key and must contain no student-specific fields or values. AI drafts and pending-review content stay outside this reusable API.

## Grammar catalog set-cover

`POST /api/grammar/select-passages`

```json
{
  "student_id": "STU-001",
  "target_codes": ["tense", "non_finite_voice"],
  "recent_error_days": 30,
  "max_passages": 5
}
```

Return the service result without hiding `uncovered`, `suggested_only`, mapping confidence, or source snapshot. The selected unit is always a complete passage.
