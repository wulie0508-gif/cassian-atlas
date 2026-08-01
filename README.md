# English Learning Tracker

A privacy-first, local-first learning record system for vocabulary, grammar, reading, translation, writing, homework, tests, and classroom review. It stores immutable attempt evidence in SQLite, links to external content by ID, builds due-review queues, and reports weaknesses with explicit sample size and confidence.

## What is open and what stays private

This repository contains only code, migrations, schemas, tests, and anonymous examples. It must not contain student names, student answers, scores, original test papers, databases, backups, or machine-specific paths.

The private data directory contains the real SQLite database, import inboxes, backups, exports, and logs. External question banks and calibrated vocabulary databases stay read-only and are referenced by stable IDs. Question-bank content is not copied wholesale. The grammar catalog stores IDs, source metadata, raw/normalized tags, and mappings—but no stem or answer text; actually used items may receive a minimal historical snapshot.

## Requirements

- Python 3.11 or newer
- No runtime packages outside the Python standard library
- SQLite with WAL support (bundled with normal Python builds)

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

## Data contracts

- `schemas/session-import.schema.json`
- `schemas/attempts-import.schema.json`
- `schemas/progress-import.schema.json`
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
