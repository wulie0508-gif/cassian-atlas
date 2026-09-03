# Cassian Atlas Codex guide

Cassian Atlas is a Codex-first, local-first learning evidence system: an
Evidence OS for agent-native tutoring. The
public repository contains software, schemas, skills, documentation, and
synthetic examples only. Private learner evidence and licensed teaching content
must stay outside the repository.

## Start here

Before changing the platform, read:

1. `README.md`
2. `docs/CODEX_APP.md`
3. `docs/CODEX_FIRST_WORKFLOW.md`
4. `docs/PRIVACY_BOUNDARY.md`

Inspect the runtime through the control plane:

```powershell
cassian info
cassian data check
```

If `cassian info` reports packaged pending migrations, run
`cassian upgrade`. Stop on checksum mismatches or unknown migrations.

## Operating rules

- Use `cassian` (with `opentutor` retained as a compatibility alias) and the
  audited HTTP contracts. Do not write directly to a
  learning database.
- Require an explicit `STU-*` learner identifier for learner-scoped work.
- Keep the dashboard read-only; mutations belong to the CLI and specialist
  skills.
- Keep question banks, source libraries, answer keys, learner files, exports,
  databases, logs, and provider credentials outside the repository.
- Model output is a candidate, not a learning fact. Image-derived answers cross
  into the evidence ledger only after the complete teacher-confirmation gate.
- Public examples must be manually authored synthetic data, never transformed
  or anonymized learner data.
- Treat optional providers and cloud projections as adapters. Never assume a
  credential, account, tenant, or remote target.

## Specialist skills

Install the bundled skills with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_codex_skills.ps1
```

Route ordinary work through `$route-learning-task`; use the smallest specialist
skill returned by the router. Platform installation, migrations, deployment,
and runtime changes belong to `$manage-learning-system`.

## Release gate

Before any public commit or release, run:

```powershell
python -m unittest discover -s tests -v
python scripts/release_privacy_audit.py
python scripts/release_privacy_audit.py --history
git diff --check
```

Do not publish if any check fails. Never weaken a privacy or learner-isolation
test merely to make a release pass.
