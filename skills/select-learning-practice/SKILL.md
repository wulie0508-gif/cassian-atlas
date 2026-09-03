---
name: select-learning-practice
description: Select the active learner's next English review using deterministic due queues, weighted mastery, verified real-question manifests, duplicate/history controls, and complete source-checked passages. Use for targeted practice, retests, grammar-cloze coverage, minimal passage sets, weak-point review, public explanation reuse, or deciding what to practise next.
---

# Select Learning Practice

Use existing algorithms; do not recreate ranking in the prompt.

1. Resolve the explicit learner, target knowledge codes, training mode, evidence references, and recent-history window.
2. Read `/api/mastery` when targets come from current weakness evidence. Keep evidence references opaque; never place a raw answer, diagnosis, prompt, or local path in selection metadata.
3. For a question-bank exercise, call `opentutor selection create --student <STU-ID> --input '<selection.json>'`. Accept only `source_checked` or `verified` real questions from the read-only configured snapshot.
4. Keep complete source-checked passages intact. Never return isolated blanks. Reject exact or near duplicates within the same manifest; exact-retest permission applies only to an explicit recent-history item in correction mode.
5. Re-read the immutable manifest with `opentutor selection show --student <STU-ID> --manifest <MANIFEST-ID>` and report selected groups, exclusions, coverage, duplicate decisions, source snapshot, and sample size.
6. For the older grammar-catalog set-cover workflow, submit targets to `POST /api/grammar/select-passages` and preserve its uncovered and suggested-only fields.
7. For vocabulary, use `/api/dictation/plan`; do not apply question or grammar set-cover to words.

Before generating a reusable explanation, run `opentutor explanation lookup --question <QUESTION-ID>`. A hit may be reused across learners because it contains public question knowledge only. On a miss, keep AI output outside the reusable cache until a source reviewer or teacher explicitly confirms it, then store it with `opentutor explanation cache --input '<public-explanation.json>'`. Never cache a learner answer, attempt, diagnosis, history, ID, or private locator.

Treat model-suggested mappings as discounted suggestions, not verified coverage. Read [selection contract](references/selection-contract.md) when constructing a manifest or explanation request.
