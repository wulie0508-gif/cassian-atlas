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
