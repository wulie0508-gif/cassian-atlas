# English Learning Tracker

A privacy-first, local-first learning record system for vocabulary, grammar, reading, translation, writing, homework, tests, and classroom review. It stores immutable attempt evidence in SQLite, links to external content by ID, builds due-review queues, and reports weaknesses with explicit sample size and confidence.

## What is open and what stays private

This repository contains only code, migrations, schemas, tests, and anonymous examples. It must not contain student names, student answers, scores, original test papers, databases, backups, or machine-specific paths.

The private data directory contains the real SQLite database, import inboxes, backups, exports, and logs. External question banks and calibrated vocabulary databases stay read-only and are referenced by stable IDs. Question-bank content is not copied wholesale. The grammar catalog stores IDs, source metadata, raw/normalized tags, and mappings—but no stem or answer text; actually used items may receive a minimal historical snapshot.

## Requirements

- Python 3.11 or newer
- SQLite with WAL support (bundled with normal Python builds)
- Core tracker and website: no third-party runtime packages
- Optional source parsing: `pypdf`, `pywin32`, Microsoft Word, Poppler and Windows Media OCR as appropriate to the source format

## Install

```powershell
python -m pip install -e .
$env:ENGLISH_TRACKER_DATA_DIR = 'C:\path\to\private-learning-data'
$env:ENGLISH_TRACKER_DB_NAME = 'learning.sqlite'
python -m english_tracker init --student STU-001 --display-name 'Private Local Name'
```

The display name is written only to the private database. Public examples always use `STU-001`.

## Core commands

```powershell
python -m english_tracker session import --input session.json
python -m english_tracker attempts import --input attempts.json
python -m english_tracker progress import --input progress.json

python -m english_tracker weaknesses report --student STU-001 --days 30
python -m english_tracker review due --student STU-001
python -m english_tracker context export --student STU-001 --for courseware
python -m english_tracker context export --student STU-001 --for dictation

python -m english_tracker knowledge sync --question-bank QUESTION_BANK.sqlite
python -m english_tracker knowledge question --question Q-EXAMPLE-001
python -m english_tracker knowledge passage --passage PAS-EXAMPLE-001
python -m english_tracker knowledge matrix --passages PAS-EXAMPLE-001 PAS-EXAMPLE-002 --csv matrix.csv
python -m english_tracker select passages --knowledge tense noun_clause --student STU-001

python -m english_tracker report weekly --student STU-001 --week-start 2026-01-12
python -m english_tracker report trends --student STU-001 --start 2026-01-01 --end 2026-03-31

python -m english_tracker data check
python -m english_tracker backup --reason before-manual-change
python -m english_tracker ingest undo --event EVT-TO-REVERT --reason 'operator correction'
python -m english_tracker ingest correct --event EVT-TO-REVERT --kind attempts --input corrected-attempts.json
```

Every bulk import automatically creates an integrity-checked SQLite online backup. All writes use transactions. Repeating the same `event_id` or `idempotency_key` with the same payload is a no-op; reusing either identifier with a different payload fails loudly.

## Local management hub

```powershell
$env:ENGLISH_TRACKER_QUESTION_BANK = 'C:\path\to\read-only-question-bank.sqlite'
$env:ENGLISH_TRACKER_LIBRARY_ROOT = 'C:\path\to\source-library'
python -m english_tracker serve --host 127.0.0.1 --port 8788 --open-browser
```

The local-only site visualizes project work, the verified question bank, real classroom performance, evidence-weighted mastery, offline calibration, passage-level reading diagnosis, the source parsing ledger, deterministic dictation, and the three conversation contracts. Its API is the preferred handoff mechanism:

- `/api/home` returns the low-friction status surface: current evidence, automation health, and the smallest next action
- `/api/context/engineering`, `/api/context/courseware`, `/api/context/dictation`
- `/api/grammar/questions/{question_id}` and `/api/grammar/passages/{passage_id}/coverage`
- `/api/grammar/coverage-matrix?passage_id=...` and `POST /api/grammar/select-passages`
- `POST /api/classroom/attempts`, `POST /api/dictation/results`
- `/api/performance/sessions`, `/api/reading/passages/{passage_id}/performance`
- `/api/reading/error-types`, `POST /api/reading/diagnostics`
- `/api/reports/weekly` and `/api/reports/trends`

The website starts in **low-friction mode**. It answers three questions first: what is happening now, what the user needs to do next, and whether the Agent workflows are healthy. Exact tables, weights, audit evidence, and emergency manual forms remain available behind **查看专业数据**. Routine classroom, reading, test, and dictation records should be written by the responsible Agent; the site is not a second clerical workflow.

## Full source-library pipeline

The pipeline never deletes or edits originals. It inventories all files, hashes exact duplicates, extracts supported text, converts legacy Word files into a private cache, reuses source-backed textbook OCR, groups prompt/answer/audio versions, and stages passages, questions, answers, RAG chunks, knowledge suggestions, and review tasks.

```powershell
python -m english_tracker library scan --root C:\path\to\source-library
python -m english_tracker library hash
python -m english_tracker library extract --limit 0
python -m english_tracker library convert-doc --limit 100
python -m english_tracker library pair
python -m english_tracker library structure
python -m english_tracker library propagate-duplicates
python -m english_tracker library summary --output library-summary.json
python -m english_tracker library structure-summary --output structure-summary.json
```

`structured` means text has been split into auditable objects. Audio marked `indexed` has been paired with its paper/script where possible; it does not claim a word-for-word transcript. Machine-created candidates stay in staging with `suggested`/`needs_check` status and cannot enter the verified question bank without review.

## Data contracts

- `schemas/session-import.schema.json`
- `schemas/attempts-import.schema.json`
- `schemas/progress-import.schema.json`
- `schemas/reading-diagnostics.schema.json`
- Anonymous examples in `examples/`

The runtime performs high-value contract checks without adding a third-party dependency. Producers should also validate against the published JSON Schemas in their own pipeline.

## Weakness interpretation

The score combines error rate, sample size, recency, consecutive errors, latest review outcome, item difficulty, and response mode. It is not a total-score ranking.

- Fewer than two attempts or fewer than two distinct items: `tentative` or `insufficient_evidence`.
- Errors remain linked to attempt IDs and evidence items.
- Session observations never become fabricated item errors.
- `answer_capture_status = not_captured` is distinct from a captured blank answer.
- No specific error cause may be attached to `not_captured`; the known evaluation result remains valid evidence, while error-cause inference is rejected.
- Rule/model knowledge mappings remain suggestions until a human verifies them; model suggestions cannot be auto-promoted by database constraint.

## Grammar coverage and measurement

The source-checked grammar catalog is versioned by source SHA-256. A hierarchical knowledge tree supports primary, secondary, prerequisite, and trap mappings. Passage selection uses weighted greedy set-cover over complete source-checked passages and incorporates recent wrong/partial evidence without splitting passages.

Weekly reports include topic accuracy, measured duration, blank rate, not-captured count, retest recovery, and knowledge-point accuracy with sample size. Trend exports partition raw scores by assessment kind, reporting series, and maximum score, so unlike totals are never connected. Schedule checks cover biweekly closed mixed tests, four-week full papers, and the December-onward weekly full-paper target.

Every active item attempt with a current evaluation is real performance evidence, including ordinary classroom practice, reading, grammar cloze, dictation and homework. Offline closed mixed tests and full papers receive higher evidence weights because they calibrate transfer under controlled conditions; they are not the only records counted as scores.

Reading reports aggregate a complete passage without breaking it into isolated questions. They keep source-backed test points separate from attempt-specific error causes, expose missing diagnoses, block cause inference for `not_captured`, and return verified same-test-point practice. Model-created diagnoses remain `suggested` until a teacher explicitly confirms them.

The first scheduler is deliberately conservative (`simple-v1`). Historical FSRS state is preserved during migration, but this project does not claim FSRS compatibility until a versioned adapter is implemented and validated.

## Legacy migration

```powershell
python -m english_tracker migrate legacy `
  --student STU-001 `
  --legacy-db 'C:\read-only\legacy-review.sqlite' `
  --mastery-json 'C:\read-only\items.json' `
  --victor-db 'C:\read-only\calibrated-vocab.sqlite'
```

The migration:

- hashes source files before and after;
- opens SQLite sources in read-only/query-only mode;
- preserves legacy rows in `legacy_records`;
- normalizes error labels while keeping `raw_error_type`;
- links JSON mastery cards to existing legacy items deterministically;
- retains scheduler-only history without inventing attempt facts;
- is idempotent by source hash and student ID.

## Testing

```powershell
python -m unittest discover -s tests -v
python -m english_tracker data check
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [data dictionary](docs/DATA_DICTIONARY.md), and [cross-thread handoff](HANDOFF_FOR_THREADS.md).

## License

MIT. The repository has no third-party runtime dependency. Student data and external question/vocabulary content are outside the license scope because they are not distributed with the repository.
