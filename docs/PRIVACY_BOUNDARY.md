# Privacy and content boundary

OpenTutor Ledger separates reusable software from private learning evidence and licensed teaching content.

## Allowed in the public repository

- source code, SQL migrations, JSON schemas, and documentation;
- anonymous synthetic examples using IDs such as `STU-001`;
- an empty question-bank schema with no questions;
- generic bilingual taxonomies and deterministic algorithms;
- tests built from synthetic prompts.

## Never publish

- a learner's name, answers, scores, notes, recordings, or OCR output;
- SQLite databases, backups, exports, inbox files, logs, or local path configuration;
- question papers, passages, answer keys, textbooks, images, or audio;
- hashes or indexes that would reconstruct restricted source content;
- absolute paths that reveal a local user or organization.

## Runtime layout

Private state lives outside the repository under `ENGLISH_TRACKER_DATA_DIR`. External question banks and source libraries are supplied through environment variables and opened read-only where the workflow permits. The repository's `.gitignore`, privacy unit test, and release audit script enforce this boundary before publication.

## Responsible disclosure

If you find a path that could expose private learning evidence, do not open a public issue containing the data. Follow [SECURITY.md](../SECURITY.md) and provide only the smallest reproducible description.
