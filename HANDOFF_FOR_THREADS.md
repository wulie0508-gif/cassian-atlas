# Handoff for Codex Conversations

All public commands use the anonymous learner ID `STU-001`. Set the private runtime variables first or use a private wrapper script maintained outside this repository.

```powershell
$env:ENGLISH_TRACKER_DATA_DIR = 'C:\path\to\private-learning-data'
$env:ENGLISH_TRACKER_DB_NAME = 'learning.sqlite'
```

Do not open the SQLite database for ad hoc writes. Do not edit migrations already applied. New producers must send JSON through the CLI.

For the current CLI-first lifecycle, explicit multi-learner boundary, and artifact-generation ledger, read [Codex-first multi-learner workflow](docs/CODEX_FIRST_WORKFLOW.md). The website is a read-only projection; it is never the source of a write default.

## Default entry: route once, then load only the required skills

Do not load this entire handoff for routine tutoring work. Invoke `$route-learning-task` with the user's unchanged request, explicit learner, subject, and source conversation. The router calls `POST /api/agent/route` with `register=true`, then executes only the returned skills.

Installed specialist skills:

| Skill | Owns |
| --- | --- |
| `$record-learning-evidence` | Sessions, item-level answers, scores, blanks, duration, and offline calibration evidence |
| `$diagnose-learning-mistakes` | Evidence-backed reading/exercise causes; suggestions stay unverified |
| `$select-learning-practice` | Due reviews, weighted mastery, and complete-passage weighted set-cover |
| `$prepare-courseware-context` | Compact lesson/slide context and source-backed teaching-method retrieval |
| `$run-dictation-workflow` | Due words, raw OCR/typed answers, deterministic grading, and retests |
| `$sync-learning-dashboard` | Agent-run status only; never learning evidence |

Router request example:

```powershell
$body = @{
  request_text = '记录今天的阅读成绩，并分析错题，然后按薄弱点选下次练习'
  student_id = 'STU-001'
  subject_code = 'english'
  source_thread = 'courseware'
  idempotency_key = 'courseware:2026-08-03:reading-review:v1'
  register = $true
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8788/api/agent/route `
  -Method Post `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

Execute the returned `steps` in order. Independent steps may use subagents in parallel; dependent evidence must be stored before its attempt is diagnosed. Each specialist appends `started`, material `progress`, and one terminal event. The website reads the same run ledger automatically, so no handwritten status handoff is required.

## Select the learner and subject explicitly

The local app supports multiple learners in one normalized database. Every learner-specific read accepts `student_id` as a query parameter, and every write accepts the same field or the `X-Student-ID` header. Never reuse one learner's cached context for another learner.

English uses the specialized question-bank, grammar, reading, and dictation adapter. `geography`, `mathematics`, `chinese`, and `science` use the generic evidence adapter until a subject-specific adapter is installed. New item payloads set `item.subject_code`; importing evidence automatically activates that learner-subject relation.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8788/api/students'
Invoke-RestMethod 'http://127.0.0.1:8788/api/subject-overview?student_id=STU-001&subject_code=geography'
```

The website language switch (`zh-CN` / `en`) changes presentation only. Stored facts, codes, and audit history are unchanged.

## Preferred live handoff: local HTTP API

When the management hub is running, read the audience-specific context first. This replaces repeatedly rewriting a handoff document:

```powershell
Invoke-RestMethod http://127.0.0.1:8788/api/context/engineering
Invoke-RestMethod http://127.0.0.1:8788/api/context/courseware
Invoke-RestMethod http://127.0.0.1:8788/api/context/dictation
```

Agent operating rule: the default product mode is `low_friction_v1`. When the user provides classroom, reading, dictation, or test results, the responsible conversation performs the relevant API call itself. Do not ask the user to repeat the same information in a website form. Reply with the current state, the smallest next action, and any decision that genuinely needs user confirmation. Full audited state is still available to the Agent and through the website's **查看专业数据** switch.

The responses contain current weaknesses, due reviews, project status, question-bank counts and the applicable endpoint map. Stable endpoints:

| Need | Method and endpoint |
| --- | --- |
| Product languages and subject registry | `GET /api/app-config` |
| Specialist capability manifest | `GET /api/agent/capabilities` |
| Route and register one task | `POST /api/agent/route` |
| Recent runs / automation dashboard | `GET /api/agent/runs`, `GET /api/agent/dashboard` |
| Append specialist progress | `POST /api/agent/runs/{run_id}/events` |
| Learner list / create a private learner | `GET /api/students`, `POST /api/students` |
| One learner's subject summary | `GET /api/subject-overview?student_id=...&subject_code=...` |
| Low-friction current state and next action | `GET /api/home` |
| One verified question and deep detail | `GET /api/questions/{question_id}` |
| One grammar question's normalized mappings | `GET /api/grammar/questions/{question_id}` |
| One complete passage coverage | `GET /api/grammar/passages/{passage_id}/coverage` |
| Several passages as a matrix | `GET /api/grammar/coverage-matrix?passage_id=PAS-1&passage_id=PAS-2` |
| Minimal complete-passage selection | `POST /api/grammar/select-passages` |
| Source-material RAG search | `GET /api/library/search?q=...` |
| Staged full-library candidates | `GET /api/library/candidates?q=...` |
| Create or confirm a learning session | `POST /api/sessions` |
| Batch classroom attempts | `POST /api/classroom/attempts` |
| Classroom scores summarized from attempts | `GET /api/performance/sessions?domain=reading` |
| One reading passage: score, test points, causes, similar items | `GET /api/reading/passages/{passage_id}/performance?session_id={optional}` |
| Reading error taxonomy and audited diagnosis | `GET /api/reading/error-types`, `POST /api/reading/diagnostics` |
| Weekly and trend data | `GET /api/reports/weekly`, `GET /api/reports/trends?start=YYYY-MM-DD&end=YYYY-MM-DD` |
| Dictation queue, OCR contract and results | `GET /api/dictation/plan`, `GET /api/contracts/dictation-ocr`, `POST /api/dictation/results` |

Example complete-passage selection body:

```json
{
  "target_codes": ["tense", "noun_clause", "preposition_collocation"],
  "max_passages": 5,
  "recent_error_days": 30
}
```

The web selector and API both use the same weighted greedy set-cover implementation and return whole passages only.

## Dictation conversation: record a session and results

For OCR integration, read `GET /api/contracts/dictation-ocr`. The OCR producer must submit the raw recognized answer unchanged; it must not compare with or rewrite toward the standard answer. Deterministic local grading then happens through `POST /api/dictation/results`, so normal repeated use consumes no model tokens.

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

## Courseware conversation: query one question's knowledge points

```powershell
python -m english_tracker knowledge question --question Q-EXAMPLE-001 --output question-knowledge.json
```

Read `mappings[].role`, `mapping_source`, `confidence`, `verification_status`, and `rationale`. Use `source_checked`/`verified` mappings as confirmed evidence. Treat `rule` or `model_suggested` rows with `verification_status=suggested` as review candidates only. The database rejects any attempt to auto-promote `model_suggested` to `source_checked` or `verified`.

## Courseware conversation: query a passage or a coverage matrix

One complete passage:

```powershell
python -m english_tracker knowledge passage --passage PAS-EXAMPLE-001 --output passage-coverage.json
```

Several complete passages, with a CSV that opens directly in Excel:

```powershell
python -m english_tracker knowledge matrix `
  --passages PAS-EXAMPLE-001 PAS-EXAMPLE-002 `
  --minimum 2 `
  --csv passage-matrix.csv `
  --output passage-matrix.json
```

The matrix keeps confirmed and suggested counts separate. Its default curriculum coverage list includes sentence backbone/predicate count, tense/voice/agreement/modals, fine non-finite forms and reasoning, derivation/inflection, determiners and prepositions, clause types/connectors, and special structures. `uncovered` means no mapping; `insufficient` includes only one confirmed item or suggested-only evidence.

## Courseware conversation: automatically select complete passages

```powershell
python -m english_tracker select passages `
  --knowledge tense noun_clause preposition_collocation non_finite_voice `
  --student STU-001 `
  --days 30 `
  --max-passages 5 `
  --output selected-passages.json
```

This is weighted greedy set-cover. Explicit targets start with the same base weight; recent wrong/partial attempts add a recency-weighted bonus. One-error evidence is labeled `tentative`. Candidates are restricted to complete `source_checked` passages and the selector returns whole `passage_id` values—never isolated blanks. Suggested mappings receive reduced selection value and remain `suggested_only` in the result.

## Courseware conversation: record classroom attempts

Create the session first, then send one attempts batch. For a new question-bank item, include a minimal item snapshot and a question reference:

Every active attempt with a current evaluation is real performance evidence. Classroom practice, grammar cloze, reading, dictation and homework do not need an offline-test label to count as scores. A formal offline closed mixed test or full paper is a higher-weight calibration anchor; it is not the definition of a real score.

```powershell
python -m english_tracker session import --input classroom-session.json
python -m english_tracker attempts import --input classroom-attempts.json
```

`classroom-attempts.json` is one idempotent envelope; put every classroom response row in its `attempts` array:

```json
{
  "event_id": "EVT-COURSE-ATTEMPTS-001",
  "idempotency_key": "courseware:attempts:001",
  "source_thread": "courseware",
  "student_id": "STU-001",
  "session_id": "SES-COURSE-001",
  "attempts": [
    "REPLACE_WITH_ATTEMPT_OBJECTS_SHOWN_BELOW"
  ]
}
```

Replace the placeholder with JSON objects (not a quoted string). The following is one attempt object:

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
  "error_types": [],
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

The example deliberately has no `error_types`: because the original response was not captured, the system may store the known `wrong` result but must not infer a specific cause from the standard answer. Question knowledge mappings and student error-cause mappings are separate tables and separate evidence claims.

When a total score, duration, environment or reporting series is also known, classify the session when it is created. This adds test metadata; it does not make the underlying attempts more or less real:

```json
{
  "event_id": "EVT-COURSE-SESSION-001",
  "idempotency_key": "courseware:session:001",
  "source_thread": "courseware",
  "student_id": "STU-001",
  "session": {
    "session_id": "SES-COURSE-001",
    "session_type": "test",
    "title": "Grammar topic quiz",
    "started_at": "2026-08-02T10:00:00+08:00"
  },
  "assessment": {
    "assessment_kind": "topic_quiz",
    "reporting_series": "grammar-fill",
    "delivery_mode": "offline_closed",
    "raw_score": 8,
    "max_score": 10,
    "duration_seconds": 900,
    "blank_count": 0,
    "validation_status": "verified"
  }
}
```

Use `assessment_kind=biweekly_mixed_test` for the two-week closed mixed test and `assessment_kind=full_exam` for a formal full paper. Do not label an ordinary lesson as a formal exam.

Record broad classroom feedback in the session payload's `observations`; do not create fake item attempts.

## Courseware conversation: analyze one reading passage

Request the full passage result rather than recomputing one question at a time:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8788/api/reading/passages/PAS-EXAMPLE-001/performance'
```

Add `?session_id=SES-COURSE-001` when the analysis must be limited to one class. The response includes:

- passage question count, attempted question count, correct, partial and wrong counts, blank count and accuracy;
- source test points and normalized knowledge mappings for each question;
- every captured student answer and its current evaluation;
- stored error causes with source, confidence and verification status;
- `pending_diagnosis` when an incorrect captured answer still needs analysis;
- `blocked_not_captured` when a specific cause is forbidden;
- verified same-primary-test-point questions from other passages.

Question knowledge and attempt error causes are different claims. A question can test inference while the student's actual cause is a stem misread or distractor trap; do not copy the test-point label into the error-cause field without evidence.

To save an agent suggestion, post an idempotent event:

```json
{
  "event_id": "EVT-READ-DIAG-001",
  "idempotency_key": "courseware:read-diag:001:v1",
  "source_thread": "courseware",
  "student_id": "STU-001",
  "diagnostics": [{
    "attempt_id": "ATT-EXAMPLE-001",
    "error_types": [{
      "code": "reading_inference_overreach",
      "confidence": 0.82,
      "error_source": "model_suggested",
      "verification_status": "suggested",
      "rationale": "The selected option adds a conclusion not supported by the located passage evidence."
    }]
  }]
}
```

Send it to `POST /api/reading/diagnostics`. Model-created diagnoses cannot be promoted beyond `suggested`. The local website can record an explicit teacher confirmation as verified. The endpoint rejects a specific diagnosis when the original answer was not captured.

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

## Courseware conversation: generate weekly and trend data

```powershell
python -m english_tracker report weekly `
  --student STU-001 `
  --week-start 2026-07-27 `
  --output weekly-report.json

python -m english_tracker report trends `
  --student STU-001 `
  --start 2026-07-01 `
  --end 2026-12-31 `
  --output trend-report.json
```

The weekly report returns topic-quiz accuracy, measured completion time, blank rate, not-captured count, retest recovery, and knowledge-point accuracy with attempt/item sample sizes. A knowledge point with exactly one error remains `tentative`.

Trend output partitions every raw-score series by `assessment_kind + reporting_series + max_score`; topic quizzes and formal papers, or tests with different maximum scores, are never connected as one raw-score line. Schedule checks report biweekly offline mixed tests, four-week offline full papers, and the December-onward target of one to two full papers per week.

## Engineering conversation: migrate, check, back up, and repair

```powershell
python -m english_tracker backup --reason before-migration
python -m english_tracker migrate legacy --student STU-001 --legacy-db OLD.sqlite --mastery-json items.json --victor-db vocab.sqlite
python -m english_tracker data check
python -m english_tracker info
```

Synchronize the source-checked grammar catalog only from a read-only question-bank path. The command creates a source hash snapshot, repairs reversible tag mojibake in the normalized field, retains the raw label, and does not copy stems or answers into the catalog:

```powershell
python -m english_tracker knowledge sync --question-bank QUESTION_BANK.sqlite --output grammar-sync.json
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

## Full-library candidate boundary

The source parsing ledger and official question bank are intentionally separate layers inside the same learning system—not parallel student databases:

- `library_resources` through `library_text_chunks` describe source files, exact duplicates, extraction and RAG provenance.
- `staged_passages`, `staged_questions`, and `staged_question_knowledge_map` contain machine-structured candidates.
- `library_structure_reviews` is the review queue for missing answers, options, passages and ambiguous OCR.
- The external verified question bank remains read-only. Only source-checked or manually verified content is treated as official.

Audio at `parse_status=indexed` is discoverable and paired, but is not claimed as transcribed. Text completion and audio indexing are reported separately by `/api/library`.
