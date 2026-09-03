# CLI control-plane commands

Prefer the `cassian` entry point. The retained `opentutor`, `open-tutor-ledger`, and `english-tracker` aliases plus `python -m english_tracker` accept the same arguments.

## Runtime selection

```powershell
opentutor --config '<private-config.json>' config show
opentutor --config '<private-config.json>' config set data_dir '<private-data-dir>'
opentutor --config '<private-config.json>' config set db_name '<database-name>.sqlite'
opentutor --config '<private-config.json>' config set question_bank '<question-bank.sqlite>'
opentutor --config '<private-config.json>' config set library_root '<source-library-root>'
```

`--config` has discovery priority, followed by `OPEN_TUTOR_CONFIG`, then `%USERPROFILE%\.opentutor\config.json`. The selected config populates the compatibility environment variables. Keep it outside the public repository.

## Inspect and protect

```powershell
opentutor info
opentutor upgrade
opentutor data check
opentutor backup --reason '<reason>'
opentutor agent capabilities
```

## Add a learner

```powershell
opentutor init
opentutor student add --student STU-<STABLE-ID> --display-name '<local display name>'
```

Initialization creates folders and schema only. `upgrade` applies pending migrations only. Learner creation is always an explicit `student add` operation.

## Route and track work

```powershell
opentutor agent route --request '<request>' --student STU-<ID> --register
opentutor agent runs --student STU-<ID>
opentutor agent show --run <RUN-ID> --student STU-<ID>
opentutor agent dashboard --student STU-<ID>
opentutor agent event --run <RUN-ID> --student STU-<ID> --event-type completed --idempotency-key '<RUN-ID>:completed:v1' --capability platform-engineering --message '<result>'
```

Every learner-scoped run read or event write requires the explicit owning student ID. `started`, material `progress`, and the terminal event should each use a stable, distinct idempotency key.

## Record deterministic learning workflows

```powershell
opentutor assessment record --student STU-<ID> --input '<assessment-payload.json>'
opentutor dictation record --student STU-<ID> --input '<dictation-payload.json>'
opentutor reading diagnostics record --student STU-<ID> --input '<reading-diagnostics-payload.json>'
```

## Track generated artifacts

```powershell
opentutor generation start --student STU-<ID> --input '<generation-start.json>'
opentutor generation update --student STU-<ID> --generation <GENERATION-ID> --input '<generation-update.json>'
opentutor generation show --generation <GENERATION-ID> --student STU-<ID>
opentutor generation list --student STU-<ID>
```

## Confirm model-derived answer evidence

```powershell
opentutor extraction create --student STU-<ID> --input '<batch.json>'
opentutor extraction provider-submit --student STU-<ID> --batch <BATCH-ID> --input '<provider-result.json>'
opentutor extraction review --student STU-<ID> --batch <BATCH-ID>
opentutor extraction decide --student STU-<ID> --batch <BATCH-ID> --input '<teacher-decisions.json>'
opentutor extraction commit --student STU-<ID> --batch <BATCH-ID> --input '<commit.json>'
```

The complete teacher-reviewed batch is the only path from provider candidates to formal attempts. Unconfirmed candidates never affect mastery, error causes, or review scheduling.

## Stage operational Base projections

```powershell
opentutor projection contract
opentutor projection target-check --student STU-<ID> --input '<current-target.json>'
opentutor projection stage --student STU-<ID> --input '<projection.json>' --target-config '<current-target.json>'
opentutor projection show --student STU-<ID> --run <PROJECTION-RUN-ID>
```

These commands validate and stage the local outbox only. They do not call Feishu. `claim` and `receipt` are publisher handshakes and must not be used without a separately approved, identity-verified transport and exact remote readback.

## Create verified selection manifests

```powershell
opentutor selection create --student STU-<ID> --input '<selection.json>'
opentutor selection show --student STU-<ID> --manifest <MANIFEST-ID>
opentutor explanation lookup --question <QUESTION-ID>
opentutor explanation cache --input '<reviewed-public-explanation.json>'
opentutor explanation invalidate --question <QUESTION-ID> --reason '<reason>'
```

Selection uses the configured question bank read-only. Public explanations are reusable only after source or teacher confirmation and remain structurally separate from learner answers and diagnoses.

## Start the local projection

```powershell
opentutor server start
opentutor server status
opentutor server restart
opentutor server stop

powershell -NoProfile -ExecutionPolicy Bypass -File '<start_learning_hub.ps1>' -Agent
powershell -NoProfile -ExecutionPolicy Bypass -File '<start_learning_hub.ps1>' -Agent -Restart
```

The lifecycle command stores a private PID record and verifies health PID, package version, schema state, and database identity before reuse or stop. The dashboard may be opened for inspection, but system mutations belong to the CLI and learning-data writes belong to audited specialist APIs.
