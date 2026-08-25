---
name: route-learning-task
description: Route OpenTutor Ledger requests to the smallest required specialist skill chain and track execution in the local dashboard. Use when a request spans platform engineering, learning evidence, mistake diagnosis, practice selection, courseware context, dictation, reports, or project status; when the correct specialist is unclear; or when another conversation would otherwise load the entire learning-system context.
---

# Route Learning Task

Use `http://127.0.0.1:8788` as the local control plane. Keep this skill thin: classify, dispatch, track, and consolidate. Do not redo specialist calculations.

## Route once

1. Request `GET /api/health` with a short timeout.
2. If unavailable, start the configured local launcher in a hidden PowerShell process. Prefer `$env:OPEN_TUTOR_LEDGER_LAUNCHER`; otherwise try `%USERPROFILE%\Desktop\近五年英语习题数据库\00_学习管理系统\start_learning_hub.ps1`. Poll health briefly.
3. Submit the user's request to `POST /api/agent/route` with `register=true`, the active `student_id`, `subject_code`, `source_thread`, and a stable idempotency key.
4. Execute only the returned `steps`, in order. Invoke each named `$skill` directly.

When subagents are available, dispatch independent steps in parallel. Keep dependent steps sequential: record evidence before diagnosing its attempt, and gather courseware targets before selecting passages.

## Track without burdening the user

Append `started`, material `progress`, and one terminal `completed`, `failed`, or `needs_input` event to the returned run. Do not ask the user to update the website. Keep the run summary short and point `result_ref` to the durable API, report, or artifact.

## Preserve boundaries

- Treat the run ledger as operational metadata, never as learning evidence.
- Route installation, CLI, deployment, migration, architecture, or multi-student platform changes through `$manage-learning-system`. The dashboard remains a read-only projection, not an operations console.
- Route all scores and answers through `$record-learning-evidence`.
- Route model-created error causes through `$diagnose-learning-mistakes`; keep them `suggested`.
- Do not load engineering, courseware, dictation, and full API documentation together.
- Ask for user input only when source facts are missing, student identity is ambiguous, or human verification is required.

Read [routing contracts](references/routing-contracts.md) only when constructing HTTP payloads.
