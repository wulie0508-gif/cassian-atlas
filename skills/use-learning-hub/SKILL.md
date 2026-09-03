---
name: use-learning-hub
description: Compatibility entry for prompts that explicitly invoke the former combined learning-hub skill or ask for the learning hub as one tool. Use only as a legacy alias; immediately route the request through route-learning-task instead of loading all learning workflows into one context.
---

# Use Learning Hub

Invoke `$route-learning-task` with the user's unchanged task, active student, subject, and source conversation. Execute only the specialist skills returned by the router.

Do not load the old combined API contract, recalculate stable metrics, write SQLite, or handle courseware, dictation, diagnosis, selection, and engineering in one context. Preserve the router's run ID so the dashboard is updated automatically.
