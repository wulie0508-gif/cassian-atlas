# Codex-first multi-learner workflow

Cassian Atlas is operated through Codex and the legacy-compatible `opentutor` command-line interface. The local website is a read-only projection for checking learner progress, evidence, generation status, and system health. It is not a second data-entry surface.

## Operating boundary

- Keep the runtime configuration, learner database, source library, question bank, exports, and generated files outside the public repository.
- Give every learner a stable `STU-*` identifier. Pass `student_id` explicitly on every learner-scoped read or write; never infer it from a display name, the last command, or the dashboard selection.
- Confirm that the learner is actively enrolled in the requested subject before recording evidence or starting a generation.
- Treat the question bank and calibrated vocabulary sources as read-only. Store provenance and stable external IDs instead of copying private source content into the public repository.
- Use the CLI or an audited specialist contract for writes. Do not edit SQLite with ad hoc SQL.
- Reuse an idempotency key only for an exact retry of the same logical request. A changed payload receives a new key.
- Treat every model-extracted answer as a candidate, regardless of question type or apparent clarity. Every item must appear in the teacher's batch review, and no part of an uncommitted batch may affect attempts, mastery, errors, or review scheduling.
- Keep Feishu as an operational/read-model projection only. Version 0.5.0 validates the Cassian target and records a local outbox/readback ledger, but includes no live Feishu transport and performs no cloud write by itself.

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

If `info` reports packaged pending migrations, run `opentutor upgrade` before ordinary work; it creates a checked backup, changes the schema only, and never creates a learner. If `info` reports a checksum mismatch or unknown applied migration, stop and investigate instead of treating it as an ordinary upgrade.

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

Direct `assessment`, `dictation`, and `attempts import` commands are for already structured evidence supplied or verified by the teacher. If any answer comes from an image, OCR, or model transcription, use the extraction gate below; do not duplicate the same response through a direct import.

### 3a. Confirm extracted answers as one complete batch

Create a learner/session-owned batch whose attempt templates contain no pre-confirmation answer, capture state, evaluation, or error cause:

```powershell
opentutor extraction create --student STU-<STABLE-ID> --input '<batch.json>'
opentutor extraction provider-submit --student STU-<STABLE-ID> --batch <BATCH-ID> --input '<codex-results.json>'
opentutor extraction provider-submit --student STU-<STABLE-ID> --batch <BATCH-ID> --input '<doubao-results.json>'
opentutor extraction review --student STU-<STABLE-ID> --batch <BATCH-ID> --output '<review.json>'
```

The provider commands append immutable candidates; they do not write attempts. The review contains every item, groups ordinary matches separately from items needing attention, hides standard answers, and shows character-level differences. During cold start:

- `R0` deterministic structured capture may use one provider, but still needs a teacher decision.
- `R1`, `R2`, and `R3` require successful independent results from exactly Codex and Doubao. `R1` remains dual-provider until a persisted learner/question-type calibration gate exists.
- Translation and writing are `R3`: compare both candidates and explicitly adjudicate differences.
- `R4` is handled manually or terminally excluded as `not_captured` / `rejected_alignment`.

Request the Codex and Doubao first-round candidates independently and, when possible, concurrently. Both receive the same minimum source crop; neither receives the standard answer or the other provider's output.

The local write API assumes one trusted operator. Provider names and teacher actors are audited claims rather than cryptographically authenticated identities, so never expose these endpoints to an untrusted network or synthesize an approval payload from a model. The confirmation skill may translate only the teacher's explicit response to the displayed review; silence remains no decision.

Submit explicit decisions against the current `review_version`, then re-read and commit only when the full batch is terminal:

```powershell
opentutor extraction decide --student STU-<STABLE-ID> --batch <BATCH-ID> --input '<teacher-decisions.json>'
opentutor extraction review --student STU-<STABLE-ID> --batch <BATCH-ID>
opentutor extraction commit --student STU-<STABLE-ID> --batch <BATCH-ID> --input '<commit.json>'
opentutor extraction show --student STU-<STABLE-ID> --batch <BATCH-ID>
```

Valid terminal decisions are `human_confirmed`, `human_corrected`, `confirmed_blank`, `not_captured`, and `rejected_alignment`. The first three create formal attempts/evaluations; the last two remain audited exclusions, and an all-excluded batch has nothing to commit. `pending_review`, `needs_check`, silence, a stale `review_version`, missing Codex/Doubao evidence, or a partial batch blocks the atomic commit. `default_action=accept_prefill` may compactly accept only visible ordinary items; it cannot resolve attention items and is never inferred from silence.

### 4. Track generated materials

Register a generation before creating a learner-specific artifact. The effective start request must include `student_id`, `title`, and an immutable `source_snapshot`; the CLI supplies `student_id` from `--student` and rejects a conflicting value in the JSON file. `subject_code` defaults to `english` and `artifact_type` defaults to `courseware`.

```powershell
opentutor generation start --student STU-<STABLE-ID> --input '<generation-start.json>'
opentutor generation update --student STU-<STABLE-ID> --generation <GENERATION-ID> --input '<generation-update.json>'
opentutor generation show --generation <GENERATION-ID> --student STU-<STABLE-ID>
opentutor generation list --student STU-<STABLE-ID>
```

Every update carries the same explicit `student_id`. A `completed` generation must resolve to a durable output path and SHA-256; a linked output artifact must belong to the same learner. New learning evidence marks prior completed outputs stale instead of silently presenting them as current.

### 4a. Select only verified reusable questions

The 0.5.0 manifest selector opens the configured question bank read-only. It accepts verified real sources, expands passage-based questions to the complete verified sibling group, compares exact and near duplicates against the current manifest and learner history, and records selections, exclusions, evidence references, and coverage in one immutable learner-owned manifest.

```powershell
opentutor selection create --student STU-<STABLE-ID> --input '<selection-request.json>' --output '<selection-manifest.json>'
opentutor selection show --student STU-<STABLE-ID> --manifest <MANIFEST-ID>
```

An exact historical retest must be explicitly allowed in `correction` mode. Current-manifest duplicates are never admitted. A finalized manifest is an auditable material decision, not a learning attempt and not mastery evidence.

Reusable public explanations are learner-free content. Their cache identity changes when the source snapshot, question, answer, knowledge mapping, rubric, policy, or schema changes:

```powershell
opentutor explanation lookup --question <QUESTION-ID>
opentutor explanation cache --input '<source-checked-explanation.json>'
opentutor explanation invalidate --question <QUESTION-ID> --reason '<reason>'
```

Only `source_checked` or `teacher_confirmed` explanations can enter or hit the reusable cache. Explanation JSON must not contain learner IDs, attempt IDs, private paths, answer submissions, or personalized diagnosis.

### 4b. Stage a Cassian-only operational Base projection

The safe user-facing operations are local inspection, target validation, staging, and readback of the ledger:

```powershell
opentutor projection contract --output '<projection-contract.json>'
opentutor projection target-check --student STU-<STABLE-ID> --input '<current-feishu-target.json>'
opentutor projection stage --student STU-<STABLE-ID> --input '<projection-run.json>' --target-config '<current-feishu-target.json>'
opentutor projection show --student STU-<STABLE-ID> --run <PROJECTION-RUN-ID>
```

`target-check` fails closed unless the configuration names `Cassian Learning Lab | 学习工作室`, app `Cassian Learning Ops`, CLI profile `cassian-learning-hub`, identity `user`, and the requested learner's exact same-tenant folder and Base. It returns a fingerprint, never the tokens or URLs. `stage` accepts only the seven operational projection whitelists and rejects question/answer/explanation/OCR content, learner display names, private paths, URLs, and credentials.

`opentutor projection claim` and `opentutor projection receipt` are low-level publisher handshakes. A separately authorized publisher may claim one learner-owned outbox row and record a sanitized retry/failure/success receipt; success requires a remote readback hash equal to the staged payload. These commands do not contain a Feishu client, do not perform a live cloud write, and must not be treated as authorization to add one. Any future transport must still use explicit `--profile cassian-learning-hub --as user` after the target file and identity are verified.

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

The lifecycle controller reuses a process only after verifying PID, package version, schema state, and database identity. Opening a browser is optional. If a check fails, stop or restart the verified process instead of assuming that any listener on the configured port is Cassian Atlas.

## Recovery rules

- Back up before migrations, legacy imports, or corrective writes.
- Never modify an already applied migration. Add a new numbered migration.
- Correct imported evidence by writing the replacement first and voiding the old event with audit history preserved.
- If an idempotency conflict reports a different payload, investigate it; do not bypass the conflict with an arbitrary key.
- Keep the old runtime intact until `opentutor data check`, learner-scoped smoke tests, and the read-only dashboard all pass against the upgraded runtime.
