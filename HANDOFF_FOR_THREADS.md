# Handoff for Codex Conversations

All public commands use the anonymous learner ID `STU-001`. Set the private runtime variables first or use a private wrapper script maintained outside this repository.

```powershell
$env:ENGLISH_TRACKER_DATA_DIR = 'C:\path\to\private-learning-data'
$env:ENGLISH_TRACKER_DB_NAME = 'learning.sqlite'
```

Do not open the SQLite database for ad hoc writes. Do not edit migrations already applied. New producers must send JSON through the CLI.

## Dictation conversation: record a session and results

Shortest commands:

```powershell
python -m english_tracker session import --input .\session.json
python -m english_tracker attempts import --input .\attempts.json
```

Minimal session JSON:

```json
{
  "event_id": "EVT-DICTATION-20260802-SESSION-01",
  "idempotency_key": "dictation:2026-08-02:session:v1",
  "source_thread": "dictation",
  "student_id": "STU-001",
  "session": {
    "session_id": "SES-DICTATION-20260802-01",
    "session_type": "dictation",
    "title": "Vocabulary retest",
    "started_at": "2026-08-02T09:00:00+08:00"
  }
}
```

Minimal attempt JSON:

```json
{
  "event_id": "EVT-DICTATION-20260802-ATTEMPTS-01",
  "idempotency_key": "dictation:2026-08-02:attempts:v1",
  "source_thread": "dictation",
  "student_id": "STU-001",
  "session_id": "SES-DICTATION-20260802-01",
  "attempts": [
    {
      "event_id": "ATT-EVT-DICTATION-20260802-001",
      "item_id": "EXISTING-ITEM-ID",
      "attempted_at": "2026-08-02T09:05:00+08:00",
      "student_answer": "captured raw answer",
      "standard_answer": "source-checked answer",
      "answer_capture_status": "captured",
      "attempt_phase": "review",
      "response_mode": "active_recall",
      "validation_status": "verified",
      "evaluation": {"result": "wrong", "score": 0, "max_score": 1, "evaluated_by": "teacher"},
      "error_types": ["spelling"]
    }
  ]
}
```

If the raw answer was not saved, use `"student_answer": null` and `"answer_capture_status": "not_captured"`. Never use an empty/NULL value to guess that the learner skipped the item.

Get the next vocabulary queue:

```powershell
python -m english_tracker context export --student STU-001 --for dictation --output dictation-context.json
```

## Courseware conversation: query weaknesses

```powershell
python -m english_tracker weaknesses report --student STU-001 --days 30 --output weaknesses.json
python -m english_tracker review due --student STU-001 --output due-reviews.json
python -m english_tracker context export --student STU-001 --for courseware --output courseware-context.json
```

Select only `verified` or `source_checked` question-bank content by default. Preserve passage grouping. A `tentative` weakness is a diagnostic retest target, not a stable diagnosis.

## Courseware conversation: record classroom attempts

Create the session first, then send one attempts batch. For a new question-bank item, include a minimal item snapshot and a question reference:

```json
{
  "event_id": "ATT-EVT-COURSE-001",
  "attempted_at": "2026-08-02T10:10:00+08:00",
  "student_answer": null,
  "standard_answer": "source-checked answer",
  "answer_capture_status": "not_captured",
  "response_mode": "production",
  "validation_status": "source_checked",
  "evaluation": {"result": "wrong", "score": 0, "max_score": 1, "evaluated_by": "teacher"},
  "error_types": [{"code": "clause_connector_error", "raw_error_type": "teacher wording"}],
  "item": {
    "domain": "grammar",
    "item_type": "cloze",
    "prompt_snapshot": "minimal historical prompt snapshot",
    "answer_snapshot": "source-checked answer",
    "knowledge_points": ["noun_clause"],
    "external_references": [
      {
        "namespace": "shanghai_question_bank",
        "reference_type": "question_id",
        "external_id": "Q-EXAMPLE-001",
        "external_parent_id": "PAS-EXAMPLE-001",
        "source_validation_status": "source_checked"
      }
    ]
  }
}
```

Record broad classroom feedback in the session payload's `observations`; do not create fake item attempts.

## Courseware conversation: record progress only

```powershell
python -m english_tracker progress import --input progress.json
```

```json
{
  "event_id": "EVT-PROGRESS-20260802-01",
  "idempotency_key": "courseware:progress:2026-08-02:v1",
  "source_thread": "courseware",
  "student_id": "STU-001",
  "session_id": "SES-COURSE-20260802-01",
  "progress": [
    {"content_label": "Passage 1", "progress_status": "completed", "completed_count": 10, "total_count": 10},
    {"content_label": "Passage 2", "progress_status": "not_started"}
  ]
}
```

## Engineering conversation: migrate, check, back up, and repair

```powershell
python -m english_tracker backup --reason before-migration
python -m english_tracker migrate legacy --student STU-001 --legacy-db OLD.sqlite --mastery-json items.json --victor-db vocab.sqlite
python -m english_tracker data check
python -m english_tracker info
```

Undo a bad import while retaining all evidence:

```powershell
python -m english_tracker ingest undo --event EVT-BAD-IMPORT --reason 'verified operator correction'
```

Replace a bad import. The replacement file must use a new event ID and idempotency key; it is imported first, then the old event is voided:

```powershell
python -m english_tracker ingest correct --event EVT-BAD-IMPORT --kind attempts --input corrected-attempts.json
```

After any correction, run `data check` and regenerate both context exports.

