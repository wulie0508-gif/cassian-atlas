<div align="center">
  <img src="docs/assets/logo.svg" width="420" alt="OpenTutor Ledger">
  <p><strong>Local-first learning evidence infrastructure for teachers, learners, and AI agents.</strong></p>
  <p>
    <a href="README.zh-CN.md">中文</a> |
    <a href="#quick-start">Quick start</a> |
    <a href="docs/PRIVACY_BOUNDARY.md">Privacy boundary</a> |
    <a href="CONTRIBUTING.md">Contributing</a>
  </p>
  <p>
    <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-244b3d?style=flat-square">
    <img alt="SQLite" src="https://img.shields.io/badge/store-SQLite-35606d?style=flat-square">
    <img alt="local first" src="https://img.shields.io/badge/privacy-local--first-b96a34?style=flat-square">
    <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-17221e?style=flat-square">
  </p>
</div>

## One evidence ledger for daily practice, controlled tests, and agent workflows.

OpenTutor Ledger records what a learner attempted, how it was evaluated, which
knowledge was involved, and what should be reviewed next. It gives teachers,
learners, and AI agents one durable answer to five recurring questions:

1. What did this learner actually do?
2. What was tested, and what evidence supports the diagnosis?
3. Which review is due next?
4. How much should this result influence the trend?
5. Can another agent reuse the answer without recomputing everything?

The system stores immutable attempts, revisioned evaluations, sample-aware
mastery, review queues, and auditable agent suggestions in a normalized SQLite
database. The local interface stays intentionally calm: current state, the
smallest next action, and automation health first. Exact weights and evidence
remain one click away.

> **Product boundary:** precise in the back, light in the front. Agents handle
> repetitive clerical work. Humans retain judgment.

## What ships

| Layer | What it does |
| --- | --- |
| Evidence ledger | Immutable item attempts, captured-answer semantics, revisioned grading, and audit history |
| Multi-learner workspaces | Switch learners without mixing sessions, attempts, mastery, or review queues |
| Multi-subject registry | English ships with a specialized adapter; geography, mathematics, Chinese, and science accept generic evidence today |
| Agent contracts | Stable JSON/HTTP boundaries for courseware, dictation, engineering, reports, and review planning |
| Evidence-weighted mastery | Controlled offline tests calibrate daily practice without erasing it |
| Knowledge graphs | Hierarchical bilingual concepts and many-to-many item mappings with source, confidence, role, and verification state |
| Deterministic review | Local grading and due queues reduce repeated model calls |
| Bilingual product UI | Switch between 简体中文 and English without changing stored facts |
| Local management app | A responsive, accessible dashboard with a low-friction default and an evidence view |

## The evidence boundary

OpenTutor Ledger deliberately stores different claims in different places:

```mermaid
flowchart LR
    Q["Item knowledge\nWhat is tested?"] --> A["Attempt\nWhat did the learner answer?"]
    A --> E["Evaluation\nHow was it scored?"]
    A --> C["Error evidence\nWhy might it be wrong?"]
    C --> V{"Verified by a human?"}
    V -->|No| S["suggested"]
    V -->|Yes| T["verified"]
```

- `answer_capture_status=not_captured` never becomes a guessed answer or a fabricated error cause.
- One failed item remains a tentative weak signal until sample size increases.
- Model-created knowledge mappings and diagnoses stay `suggested`.
- Question knowledge describes what an item tests; attempt error evidence describes what happened to one learner.
- Unlike exam totals are never connected as one raw-score trend.

## Agent-native by design

The website is not a second data-entry job. An agent starts with the compact context endpoint, performs the work, and writes through an idempotent contract.

```text
GET  /api/home
GET  /api/context/courseware
GET  /api/context/dictation
POST /api/classroom/attempts
POST /api/dictation/results
GET  /api/reports/weekly
GET  /api/reports/trends
```

Every write creates a checked backup, runs inside a transaction, and can be replayed safely with the same idempotency key. Agents never need ad hoc SQL access.

## Quick start

### 1. Install

```bash
git clone https://github.com/wulie0508-gif/open-tutor-ledger.git
cd open-tutor-ledger
python -m venv .venv
python -m pip install -e .
```

### 2. Create a private runtime outside the repository

PowerShell:

```powershell
$env:ENGLISH_TRACKER_DATA_DIR = "$env:USERPROFILE\OpenTutorData"
$env:ENGLISH_TRACKER_DB_NAME = "learning.sqlite"
python -m english_tracker init --student STU-001 --display-name "Local learner"
python scripts/create_empty_question_bank.py --output "$env:USERPROFILE\OpenTutorData\question-bank.sqlite"
New-Item -ItemType Directory -Force "$env:USERPROFILE\OpenTutorData\source-library" | Out-Null
$env:ENGLISH_TRACKER_QUESTION_BANK = "$env:USERPROFILE\OpenTutorData\question-bank.sqlite"
$env:ENGLISH_TRACKER_LIBRARY_ROOT = "$env:USERPROFILE\OpenTutorData\source-library"
python -m english_tracker serve --host 127.0.0.1 --port 8788 --open-browser
```

The empty question-bank shell contains schema only-no exercises. Bring your own licensed or original content through an adapter and keep it outside the repository.

### 3. Record an anonymous learning event

```powershell
python -m english_tracker session import --input examples/session.example.json
python -m english_tracker attempts import --input examples/attempts.example.json
python -m english_tracker weaknesses report --student STU-001 --days 30
```

## Multi-learner and multi-subject model

Each learner owns sessions, attempts, review state, and reports. Content items belong to a registered subject through `subject_code`. The web app can create and switch learner workspaces without restarting the service.

Specialized adapters may add a subject-specific question bank, knowledge tree, selector, or grader. The generic ledger works without one:

```json
{
  "item": {
    "subject_code": "geography",
    "domain": "knowledge",
    "item_type": "multiple_choice",
    "prompt_snapshot": "Anonymous local prompt",
    "answer_snapshot": "A"
  }
}
```

## English adapter

The bundled English adapter adds:

- grammar knowledge trees and complete-passage coverage matrices;
- weighted greedy set-cover over complete source-checked passages;
- reading test-point and learner error-cause separation;
- deterministic vocabulary dictation and review queues;
- source-library staging, OCR provenance, and teaching-method retrieval;
- weekly reports and separated assessment trends.

No question bank, exam paper, passage, textbook, answer key, audio, or learner record is distributed with this project.

## Architecture

```mermaid
flowchart TB
    UI["Bilingual local app"] --> API["Local HTTP contracts"]
    AG["Courseware · Dictation · Engineering agents"] --> API
    API --> ING["Idempotent ingestion boundary"]
    ING --> DB[("Private SQLite evidence ledger")]
    DB --> REP["Mastery · Reviews · Reports"]
    EXT[("Private external content")] -->|"read-only adapters"| API
    PUB["Public repository"] -. "contains code only" .-> API
```

Read [ARCHITECTURE.md](ARCHITECTURE.md) and the [data dictionary](docs/DATA_DICTIONARY.md) for the complete model.

## Privacy release gate

Before publishing:

```bash
python -m unittest discover -s tests -v
python scripts/release_privacy_audit.py
```

The release gate rejects tracked databases, documents, spreadsheets, images, audio, archives, oversized files, private path markers, and known learner identifiers. See [the full boundary](docs/PRIVACY_BOUNDARY.md).

## Project status

The evidence store, multi-learner isolation, subject registry, Chinese/English UI, local app, agent contracts, English analytics, privacy tests, and backup workflow are implemented. The project is pre-1.0: internet-facing authentication, packaged subject adapters, and an FSRS-compatible scheduler remain future work.

## Contributing

Contributions are welcome-especially generic subject adapters, accessibility improvements, privacy tooling, and evidence-calibration research. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## License

Code and original documentation are licensed under the [MIT License](LICENSE). Learner data and third-party educational content are outside the repository and outside this license.
