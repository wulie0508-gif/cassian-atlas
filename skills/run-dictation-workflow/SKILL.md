---
name: run-dictation-workflow
description: "Run the active learner's fixed local vocabulary dictation workflow: fetch due words, preserve raw OCR or typed answers, grade deterministically, store results, and schedule retests. Use for dictation preparation, OCR answer intake, vocabulary grading, spelling review, or retest planning without re-designing the workflow in each conversation."
---

# Run Dictation Workflow

1. Read `/api/context/dictation`, then fetch the required batch from `/api/dictation/plan`.
2. If answers come from an image, OCR, or model, invoke `$confirm-learning-evidence`; preserve raw output and do not submit it directly as a formal result. The extraction commit is the sole formal attempt write for those items.
3. Submit ordered items to `/api/dictation/results` only for teacher-typed structured answers that have not already been written by an extraction commit; let the local service grade them.
4. Re-read `/api/dictation/plan` and the session performance to confirm storage and retest scheduling.

Never correct OCR text against the standard answer. A teacher-confirmed blank may become an empty captured answer; unreadable handwriting remains `not_captured` or `needs_check`. The website's manual table is recovery-only, not the normal workflow.

After an extraction commit, re-read the dictation plan and session performance to verify scheduling; never post the same confirmed answers to `/api/dictation/results` a second time.

Read [dictation contract](references/dictation-contract.md) only when submitting results.
