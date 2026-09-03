---
name: prepare-courseware-context
description: Fetch compact, source-backed context for the active learner's courseware and lesson-planning conversation, including recent performance, weighted mastery, due reviews, question knowledge, passage coverage, teaching methods, and source materials. Use before creating a lesson, worksheet, slide deck, review plan, or post-class handoff so stable metrics are not recomputed in the prompt.
---

# Prepare Courseware Context

## Build the smallest context packet

1. Read `/api/context/courseware` first.
2. Add only evidence required by the lesson: `/api/mastery`, `/api/reports/weekly`, one question, one passage coverage matrix, or a focused `/api/library/search` query.
3. Use `$select-learning-practice` when the lesson needs automatic passage selection.
4. Preserve source IDs, verification states, sample sizes, and missing fields in the handoff.
5. Return a concise courseware brief: goal, evidence, selected material, teaching method, cautions, and post-class capture plan.

Do not write attempts from this skill. After class, invoke `$record-learning-evidence`; invoke `$diagnose-learning-mistakes` only when raw answers support it.

Read [courseware endpoints](references/courseware-endpoints.md) only for focused follow-up queries.
