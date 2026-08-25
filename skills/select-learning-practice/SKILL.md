---
name: select-learning-practice
description: Select the active learner's next English review using deterministic due queues, weighted mastery, recent wrong-answer evidence, and weighted set-cover over complete source-checked passages. Use for targeted practice, retests, grammar-cloze coverage, minimal passage sets, weak-point review, or deciding what to practise next.
---

# Select Learning Practice

Use existing algorithms; do not recreate ranking in the prompt.

1. Resolve explicit target knowledge codes and recent-error window.
2. Read `/api/mastery` when targets come from current weakness evidence.
3. For grammar cloze, submit targets to `POST /api/grammar/select-passages`.
4. Keep complete source-checked passages intact. Never return isolated blanks.
5. Report selected passage IDs, confirmed coverage, suggested-only coverage, uncovered targets, recent-error contribution, and sample size.
6. For vocabulary, use `/api/dictation/plan`; do not apply grammar set-cover to words.

Treat model-suggested mappings as discounted suggestions, not verified coverage. Read [selection contract](references/selection-contract.md) when constructing the request.
