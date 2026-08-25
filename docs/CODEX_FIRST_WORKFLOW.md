# Codex-first multi-learner workflow

OpenTutor Ledger is operated through Codex and the `opentutor` command-line interface. The local website is a read-only projection for checking learner progress, evidence, generation status, and system health. It is not a second data-entry surface.

## Operating boundary

- Keep the runtime configuration, learner database, source library, question bank, exports, and generated files outside the public repository.
- Give every learner a stable `STU-*` identifier. Pass `student_id` explicitly on every learner-scoped read or write; never infer it from a display name, the last command, or the dashboard selection.
- Confirm that the learner is actively enrolled in the requested subject before recording evidence or starting a generation.
- Treat the question bank and calibrated vocabulary sources as read-only. Store provenance and stable external IDs instead of copying private source content into the public repository.
- Use the CLI or an audited specialist contract for writes. Do not edit SQLite with ad hoc SQL.
- Reuse an idempotency key only for an exact retry of the same logical request. A changed payload receives a new key.

## One-time private setup

Install the package and Codex skills from the repository, then create a private configuration:

```powershell
python -m pip install -e .
powershell -ExecutionPolicy Bypass -File scripts/install_codex_skills.ps1

opentutor config set data_dir '<private-data-directory>'
opentutor config set db_name 'learning.sqlite'
opentutor config set question_bank '<read-only-question-bank.sqlite>'
opentutor config set library_root '<read-only-source-library>'
opentutor init
opentutor info
```

`opentutor init` creates the private layout and schema only. It does not create a learner. Add each learner explicitly:

```powershell
opentutor student add --student STU-<STABLE-ID> --display-name '<private-display-name>'
opentutor student list
```

Use global `--config '<private-config.json>'` for a non-default configuration. Discovery order is global `--config`, `OPEN_TUTOR_CONFIG`, then `%USERPROFILE%\.opentutor\config.json`.

## Standard Codex task lifecycle

### 1. Check the selected runtime

```powershell
opentutor info
opentutor data check
opentutor server status
```

If `info` reports pending migrations, make a checked backup and run `opentutor upgrade` before ordinary work. `upgrade` changes the schema only and never creates a learner.

### 2. Route and register the request once

```powershell
opentutor agent route `
  --request '<unchanged-user-request>' `
  --student STU-<STABLE-ID> `
  --subject english `
  --source-thread '<conversation-role>' `
  --idempotency-key '<conversation>:<date>:<task>:v1' `
  --register
```

Execute only the returned specialist steps. Record operational state under the returned run ID; do not turn run events into scores, mistakes, or mastery evidence.

```powershell
opentutor agent event `
  --run <RUN-ID> `
  --student STU-<STABLE-ID> `
  --event-type started `
  --idempotency-key '<run-id>:started:v1' `
  --capability '<CAPABILITY-KEY>' `
  --message '<concise-progress-message>'
```

### 3. Use the narrow deterministic workflow

The specialist validates the input contract, creates a backup for a private-data write, writes transactionally, and re-reads the durable result. Core workflow entry points are:

```powershell
opentutor assessment record --student STU-<STABLE-ID> --input '<assessment-payload.json>'
opentutor dictation record --student STU-<STABLE-ID> --input '<dictation-payload.json>'
opentutor reading diagnostics record --student STU-<STABLE-ID> --input '<reading-diagnostics-payload.json>'
```

Use the existing session, attempt, report, review, selection, and context commands for their narrower responsibilities. A run event only reports operational progress; the corresponding learning contract remains the sole source of learning evidence.

### 4. Track generated materials

Register a generation before creating a learner-specific artifact. The effective start request must include `student_id`, `title`, and an immutable `source_snapshot`; the CLI supplies `student_id` from `--student` and rejects a conflicting value in the JSON file. `subject_code` defaults to `english` and `artifact_type` defaults to `courseware`.

```powershell
opentutor generation start --student STU-<STABLE-ID> --input '<generation-start.json>'
opentutor generation update --student STU-<STABLE-ID> --generation <GENERATION-ID> --input '<generation-update.json>'
opentutor generation show --generation <GENERATION-ID> --student STU-<STABLE-ID>
opentutor generation list --student STU-<STABLE-ID>
```

Every update carries the same explicit `student_id`. A `completed` generation must resolve to a durable output path and SHA-256; a linked output artifact must belong to the same learner. New learning evidence marks prior completed outputs stale instead of silently presenting them as current.

### 5. Verify and close the run

Re-read the learner-scoped result and then append exactly one terminal event:

```powershell
opentutor agent show --run <RUN-ID> --student STU-<STABLE-ID>
opentutor agent dashboard --student STU-<STABLE-ID>
opentutor agent event `
  --run <RUN-ID> `
  --student STU-<STABLE-ID> `
  --event-type completed `
  --idempotency-key '<run-id>:completed:v1' `
  --capability '<CAPABILITY-KEY>' `
  --message '<concise-completion-message>' `
  --summary '<durable-result-summary>' `
  --result-ref '<durable-result-reference>'
```

Use `failed`, `cancelled`, or `needs_input` only when that state is true. Terminal runs reject later mutations; an exact replay with the same idempotency key is safe.

## Multi-learner isolation checklist

Before every write, verify all four identifiers:

1. the runtime configuration and database identity;
2. the explicit `student_id`;
3. the active `subject_code` enrollment;
4. the owner of any linked session, attempt, review, artifact, or generation.

Dashboard filters are presentation only. Changing the selected learner in the website never changes a CLI default and never authorizes a write. Cross-learner references are rejected both by application checks and database integrity triggers.

## Service lifecycle

```powershell
opentutor server start
opentutor server status
opentutor server restart
opentutor server stop
```

The lifecycle controller reuses a process only after verifying PID, package version, schema state, and database identity. Opening a browser is optional. If a check fails, stop or restart the verified process instead of assuming that any listener on the configured port is OpenTutor Ledger.

## Recovery rules

- Back up before migrations, legacy imports, or corrective writes.
- Never modify an already applied migration. Add a new numbered migration.
- Correct imported evidence by writing the replacement first and voiding the old event with audit history preserved.
- If an idempotency conflict reports a different payload, investigate it; do not bypass the conflict with an arbitrary key.
- Keep the old runtime intact until `opentutor data check`, learner-scoped smoke tests, and the read-only dashboard all pass against the upgraded runtime.
