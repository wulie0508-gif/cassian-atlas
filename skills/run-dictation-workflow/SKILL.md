---
name: run-dictation-workflow
description: "Run the active learner's fixed local vocabulary dictation workflow: fetch due words, preserve raw OCR or typed answers, grade deterministically, store results, and schedule retests. Use for dictation preparation, OCR answer intake, vocabulary grading, spelling review, or retest planning without re-designing the workflow in each conversation."
---

# Run Dictation Workflow

1. Read `/api/context/dictation`, then fetch the required batch from `/api/dictation/plan`.
2. If an OCR producer is used, follow `/api/contracts/dictation-ocr` and preserve its raw output exactly.
3. Submit ordered items to `/api/dictation/results`; let the local service grade them.
4. Re-read `/api/dictation/plan` and the session performance to confirm storage and retest scheduling.

Never correct OCR text against the standard answer before storage. Submit an empty string for a captured blank; do not guess unreadable handwriting. The website's manual table is recovery-only, not the normal workflow.

Read [dictation contract](references/dictation-contract.md) only when submitting results.
