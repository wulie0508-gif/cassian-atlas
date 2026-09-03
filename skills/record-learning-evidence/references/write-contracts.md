# Evidence write contracts

These direct contracts accept teacher-supplied facts that have not already been written. Image/OCR/model-derived answers must use `$confirm-learning-evidence`; its commit operation creates attempts transactionally after full-batch review. Never resubmit those committed items to the direct attempts endpoint.

## Session

`POST /api/sessions`

```json
{
  "event_id": "EVT-UNIQUE",
  "idempotency_key": "courseware:date:session:v1",
  "source_thread": "courseware",
  "student_id": "STU-001",
  "session": {
    "session_id": "SES-UNIQUE",
    "session_type": "lesson",
    "title": "Reading lesson",
    "started_at": "2026-08-03T09:00:00+08:00"
  }
}
```

## Item attempts

`POST /api/classroom/attempts`

```json
{
  "event_id": "EVT-UNIQUE",
  "idempotency_key": "courseware:date:attempts:v1",
  "source_thread": "courseware",
  "student_id": "STU-001",
  "session_id": "SES-UNIQUE",
  "attempts": [{
    "event_id": "ATT-EVENT-UNIQUE",
    "attempted_at": "2026-08-03T09:05:00+08:00",
    "student_answer": null,
    "standard_answer": "A",
    "answer_capture_status": "not_captured",
    "evaluation": {"result": "wrong", "score": 0, "max_score": 1},
    "error_types": [],
    "item": {
      "subject_code": "english",
      "domain": "reading",
      "item_type": "multiple_choice",
      "answer_snapshot": "A",
      "external_references": [{
        "namespace": "shanghai_question_bank",
        "reference_type": "question_id",
        "external_id": "Q-...",
        "external_parent_id": "PAS-...",
        "source_validation_status": "source_checked"
      }]
    }
  }]
}
```

## Assessment total

Use `POST /api/assessments` with `assessment_kind`, `delivery_mode`, `raw_score`, `max_score`, duration, and blanks. Use `biweekly_mixed_test` or `full_exam` plus `offline_closed` only when those facts are explicit.
