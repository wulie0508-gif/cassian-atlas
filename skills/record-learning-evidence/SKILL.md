---
name: record-learning-evidence
description: Validate and write the active learner's item-level classroom answers, reading or grammar results, homework, dictation evidence, and offline assessment totals through audited local APIs. Use whenever the user supplies scores, correct/wrong results, raw answers, duration, blanks, or calibration-test results, and when an agent must update a Cassian Atlas database without direct SQLite access.
---

# Record Learning Evidence

Write facts through `http://127.0.0.1:8788`; never edit SQLite directly.

If an answer comes from an image, OCR, or model transcription and has not already passed an explicit teacher confirmation batch, invoke `$confirm-learning-evidence` instead. Do not bypass that gate with `/api/classroom/attempts`. This skill remains the direct path for facts the teacher supplied as already-confirmed structured answers or scores.

An extraction batch commit already creates the formal attempts and evaluations. Do not submit those same confirmed items again through `/api/classroom/attempts`; verify the extraction commit readback instead.

## Record

1. Check `/api/health` and resolve the active student.
2. Create or confirm the learning session with `POST /api/sessions`.
3. Normalize item-level facts and submit them once to `POST /api/classroom/attempts`.
4. Use `POST /api/assessments` only for separately reported totals and controlled-test metadata.
5. Re-read `/api/performance/sessions` or the affected passage report and verify counts.

Use stable source question, passage, session, and item IDs when they exist. Use a fresh `event_id` and stable `idempotency_key` for each logical write; reuse both only for an identical retry.

## Protect evidence

- Set `answer_capture_status=not_captured` and `student_answer=null` when the raw answer was not saved.
- Do not attach a specific error cause to a not-captured answer.
- Keep everyday lessons as real evidence. Mark offline closed mixed tests and full papers as higher-weight calibration anchors, not the only real scores.
- Store item-level attempts when available; do not replace them with a total score.
- Never treat a provider candidate or an incomplete confirmation batch as an available item-level attempt.
- Send post-write diagnosis to `$diagnose-learning-mistakes`.

Read [write contracts](references/write-contracts.md) only for the payload being submitted.
