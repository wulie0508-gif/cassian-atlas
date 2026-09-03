# Privacy and content boundary

Cassian Atlas separates reusable software from private learning evidence and licensed teaching content.

## Allowed in the public repository

- source code, SQL migrations, JSON schemas, and documentation;
- anonymous synthetic examples using IDs such as `STU-LOCAL-001`;
- an empty question-bank schema with no questions;
- generic bilingual taxonomies and deterministic algorithms;
- tests built from synthetic prompts.

## Never publish

- a learner's name, answers, scores, notes, recordings, or OCR output;
- SQLite databases, backups, exports, inbox files, logs, or local path configuration;
- question papers, passages, answer keys, textbooks, images, or audio;
- hashes or indexes that would reconstruct restricted source content;
- absolute paths that reveal a local user, organization, corpus, or launcher;
- API keys, access tokens, provider responses, `.env` files, or credential-bearing configuration.

## Runtime layout

Private state lives outside the repository. Runtime configuration is resolved in
this order: global `--config`, `OPEN_TUTOR_CONFIG`, then
`%USERPROFILE%\.opentutor\config.json`. External question banks and source
libraries are referenced from that private configuration and opened read-only
where the workflow permits.

Optional model-provider credentials are stored separately from both the clone
and the learning ledger. They are never copied into project instructions,
logs, examples, public screenshots, or Feishu projections. The repository's
`.gitignore`, privacy unit tests, and release audit script enforce this boundary
before publication.

## Private commercial content

A private corpus, source-traceable index, or RAG adapter can be connected to
Cassian Atlas only when the operator has the rights to use that material.
Those assets are licensed separately and are not covered by the repository's
MIT License. Public demos and tests must remain synthetic and disconnected.

## Responsible disclosure

If you find a path that could expose private learning evidence, do not open a public issue containing the data. Follow [SECURITY.md](../SECURITY.md) and provide only the smallest reproducible description.
