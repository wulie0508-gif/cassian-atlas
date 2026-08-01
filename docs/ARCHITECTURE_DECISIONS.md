# Architecture Decisions and Risks

## Decisions

1. SQLite is the private system of record because the workload is local, single-learner, auditable, and should remain easy to back up and inspect.
2. Other conversations write only through CLI + JSON contracts; schema ownership stays with the engineering conversation.
3. Attempts are immutable facts. Correction creates a replacement import and voids the old event, retaining both payloads and an audit record.
4. External source databases stay read-only. Used items receive stable external IDs and a minimal historical snapshot.
5. Weakness confidence is separate from weakness score. One failed question cannot become a stable diagnosis.
6. Scheduler-only legacy history without an answer/evaluation is retained in `legacy_records`, not promoted to an attempt.
7. The MVP has no third-party runtime dependency, reducing license and deployment risk.
8. Grammar source snapshots store metadata and mappings, not the full question text. Raw and normalized source labels coexist so reversible mojibake repair is auditable.
9. Question coverage and student error cause are separate facts. `not_captured` preserves the result but prohibits a specific active error-cause mapping.
10. Rule/model mappings remain suggested until manual review. A database constraint prevents model-generated mappings from becoming verified automatically.
11. Passage selection uses weighted greedy set-cover over complete source-checked passages; it never optimizes individual blanks outside their passage.
12. Assessment raw scores are partitioned by kind, reporting series, and maximum score. Schedule compliance and learning outcomes are separate measurements.
13. Offline closed evidence calibrates everyday practice: formal full papers use weight 1.60, biweekly mixed tests 1.40, offline topic tests 1.20, and offline dictation 1.10 before item/evidence-quality multipliers.
14. Full-library parsing is a staging layer in the unified learning system, not a second student database. Automated candidates remain `suggested`/`needs_check` until source review.
15. Text parsing and audio indexing are separate completion claims. An indexed audio file is paired and searchable by metadata but is never described as transcribed without transcript evidence.
16. Three conversations exchange live context and writes through a local HTTP API; the Markdown handoff remains the durable contract and fallback.
17. Multiple learners use one normalized store but every performance query and write resolves an explicit `student_id`.
18. Subject registration is generic. English owns a specialized adapter; other subjects can record evidence without inheriting English question-bank assumptions.
19. Interface locale is presentation state only. Switching Chinese/English never mutates evidence facts.

## Risks and controls

| Risk | Impact | Control |
| --- | --- | --- |
| Duplicate imports from multiple conversations | Inflated error rates and review queues | payload SHA-256, unique idempotency key, unique attempt event ID |
| Missing raw answers | Blank and not-captured could be confused | mandatory `answer_capture_status` |
| Correct answer used to invent a student error cause | False diagnosis | contract rejection, separate mapping tables, quality check, historical rows voided/rejected |
| General classroom feedback fabricated as item errors | False weakness diagnosis | separate `session_observations` table and evidence level |
| OCR or external-source drift | Wrong standard answers | validation status and read-only source references |
| Manual correction overwritten | Lost teacher judgment | immutable attempts, revisioned evaluations, manual override guard |
| Legacy label inconsistency | Split error categories | bilingual alias map plus retained raw label |
| One-question overdiagnosis | Bad content selection | confidence gate based on attempts and distinct items |
| SQLite file corruption or WAL-copy mistakes | Data loss | online backup API plus post-backup integrity check |
| Coarse or incorrect legacy grammar tags | Misleading fine-grained coverage | source snapshot, raw/normalized fields, confirmed-vs-suggested matrix, rationale and confidence |
| Model suggestion treated as reviewed truth | Automated label contamination | SQL CHECK constraint plus quality gate |
| Unlike exam totals joined as one trend | False score trend | series key includes assessment kind, reporting series, and maximum score |
| Machine-split source material mistaken for verified questions | Unreliable lessons and analytics | separate staging tables, visible review queue, verification boundary and SQL guards |
| Audio indexing reported as transcription | False completeness claim | separate resource state, counts and interface labels |
| Multiple conversations write SQLite directly | corruption and conflicting semantics | idempotent CLI/HTTP import contracts and automatic backups |
