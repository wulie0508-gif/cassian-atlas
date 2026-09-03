---
name: manage-learning-system
description: Maintain or upgrade the Cassian Atlas platform through its Codex-first CLI control plane. Use for installation, launcher/configuration, database migrations, system architecture, deployment, read-only dashboard design, or adding and isolating multiple students; do not use for ordinary lesson evidence, diagnosis, practice selection, courseware, or dictation.
---

# Manage Learning System

Treat `cassian` as the primary control-plane command. The legacy `opentutor` alias remains valid for existing automation. The browser dashboard is a read-only projection of database and run-ledger state; never require the user to click through the site to operate the system.

## Work through the CLI

1. Resolve the runtime with global `--config`, `OPEN_TUTOR_CONFIG`, or `%USERPROFILE%\.opentutor\config.json`; environment variables remain a compatibility fallback. Do not embed a learner, database filename, or private path in source code.
2. Inspect current state with `cassian info`, `cassian data check`, and the narrow command relevant to the request.
3. If `info` reports pending migrations, run `cassian upgrade` before ordinary commands. `upgrade` changes schema only and never creates a learner.
4. Use audited CLI operations for initialization, backup, migrations, imports, reports, routing, and server lifecycle. Modify repository code only when the requested platform behavior is not already exposed by the CLI.
5. Verify the resulting command and re-read durable state. Record the platform run through the router's existing run ID; do not turn run status into learning evidence.

`opentutor`, `open-tutor-ledger`, `english-tracker`, `python -m english_tracker`, `.opentutor`, and `OPEN_TUTOR_*` remain stable compatibility identifiers for the Cassian Atlas runtime. Existing automation may continue to use them without migration.

## Keep students isolated

- Give each learner a stable `STU-*` identifier and pass it explicitly to student-scoped commands.
- Use `cassian init` only to create the private layout and schema. Add a learner explicitly with `cassian student add --student <id> --display-name <name>`.
- Keep shared question-bank sources separate from private learner evidence. Never infer the active learner from a display name or the most recent dashboard page.
- Back up before schema or corrective data changes and preserve idempotency keys for retried writes.
- Start and update generated-material records with an explicit learner ID. A generated artifact and every source evidence row must remain owned by that same learner.

## Start without UI work

Use `cassian server start|status|stop|restart` or the configured launcher with `-Agent` / `-Restart`. Reuse a server only after PID, application version, schema, and database checks pass; opening a browser is optional and only for viewing.

Read [CLI control-plane commands](references/cli-control-plane.md) when constructing or checking commands. Repository maintainers can consult [the full Codex-first workflow](../../docs/CODEX_FIRST_WORKFLOW.md) for the lifecycle and recovery boundary.
