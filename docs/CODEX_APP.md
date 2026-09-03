# Cassian Atlas as a Codex-first application

> **Map every attempt. Navigate the next lesson.**

Cassian Atlas is an Evidence OS for agent-native tutoring, packaged as a
Codex-first application bundle: independently
loadable skills provide the agent interface, `cassian` provides the audited
control plane, SQLite stores private learning evidence, and the local Teacher
Console presents a read-only decision surface. It is not a hosted SaaS product
and the Cassian Atlas core makes no outbound learner-data request by default.
Content intentionally supplied to Codex or to an operator-configured model or
cloud adapter is governed by that provider and account's data terms.

The previous `opentutor` command remains an exact compatibility alias, and
existing `.opentutor` configuration and data paths are intentionally preserved.

## The application bundle

```text
Codex task
  -> AGENTS.md project policy
  -> route-learning-task
  -> smallest specialist skill
  -> cassian CLI / local HTTP contract
  -> private evidence ledger
  -> read-only Teacher Console
```

| Layer | Included in this repository | Private at runtime |
| --- | --- | --- |
| Codex interface | Project policy, router, specialist skills, installation script | Agent conversation history |
| Control plane | `cassian` CLI, schemas, migrations, audited contracts | Runtime configuration |
| Evidence system | Database schema, deterministic workflows, tests | Learner attempts, evaluations, review state |
| Content adapter | Empty question-bank shell and integration contracts | Licensed question banks, source files, RAG indexes |
| Model adapter | Privacy-minimized provider code and configuration guide | API keys and provider responses |
| Interface | Local read-only Teacher Console and synthetic public tour | Live learner projections |

## Install for Codex

```powershell
git clone https://github.com/wulie0508-gif/cassian-atlas.git
cd cassian-atlas
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
powershell -ExecutionPolicy Bypass -File scripts/install_codex_skills.ps1
```

Open the repository as a Codex project after installing the skills. Codex reads
the root `AGENTS.md`, routes the request, and uses the relevant skill contract.
A typical first instruction is:

```text
Use $manage-learning-system to initialize a private Cassian Atlas runtime and show
me the health check before creating any learner.
```

Create the private runtime outside the clone:

```powershell
$privateRoot = "$env:USERPROFILE\CassianAtlasData"
cassian config set data_dir $privateRoot
cassian config set db_name "learning.sqlite"
cassian config set question_bank "$privateRoot\question-bank.sqlite"
cassian config set library_root "$privateRoot\source-library"
python scripts/create_empty_question_bank.py --output "$privateRoot\question-bank.sqlite"
New-Item -ItemType Directory -Force "$privateRoot\source-library" | Out-Null
cassian init
cassian info
cassian data check
```

Adding a learner is always a separate, explicit action:

```powershell
cassian student add --student STU-LOCAL-001 --display-name "Local learner"
```

## What Codex can operate

- capture teacher-confirmed classroom, assessment, reading, and dictation
  evidence;
- compare image-transcription candidates while keeping the teacher as the final
  confirmation gate;
- diagnose stable mistakes without turning one wrong answer into a permanent
  label;
- retrieve source-backed courseware context and select complete, verified
  practice groups;
- maintain deterministic retest queues and comparable trend series;
- generate learner-owned artifact records and stage privacy-whitelisted
  operational projections;
- start, inspect, upgrade, back up, and verify the local platform.

The website is deliberately not a second data-entry application. Codex and the
CLI perform audited operations; the interface explains current state and the
smallest useful next action.

## Optional multimodal extraction

The bundled `process-homework-evidence` skill can use a separately configured
vision provider to produce transcription candidates. Credentials belong only
in the private file described by
[`provider-config.md`](../skills/process-homework-evidence/references/provider-config.md).
The public repository contains no credential, endpoint entitlement, learner
image, or provider output.

Recognition, confirmation, grading, diagnosis, and publishing remain separate
stages. A model candidate never becomes a score or mastery signal without the
complete teacher-confirmed commit.

## Shanghai English corpus and private RAG integration

The open-source application ships no exam content. A separately maintained,
provenance-aware index and retrieval corpus is built for Shanghai English exam
materials from the past three years. Exact years, paper types, question types,
and licensable content are defined in a written content schedule. For materials
covered by documented rights, or supplied by customers who are authorized to
use them, we can provide private deployment, RAG integration, knowledge-point
retrieval, complete-passage selection, retesting, and assessment-assembly
workflow adaptation.

Commercial licensing and partnerships:
[wulie0508@gmail.com](mailto:wulie0508@gmail.com)

Coverage, source rights, delivery format, and permitted uses are governed by an
agreed content schedule and license. The private corpus is not included in this
repository's MIT License. Cassian Atlas is not an official product of, or
partnership with, the Shanghai Municipal Educational Examinations Authority,
any school, or any publisher.

## Public-release boundary

Before publishing any change:

```powershell
python -m unittest discover -s tests -v
python scripts/release_privacy_audit.py
python scripts/release_privacy_audit.py --history
git diff --check
```

The release gate rejects learner identifiers, private paths, databases, source
documents, media, archives, and other content that does not belong in the
public application. Read [`PRIVACY_BOUNDARY.md`](PRIVACY_BOUNDARY.md) for the
complete contract.

Cassian Atlas is an independent open-source project by Cassian Learning Lab and is not affiliated
with or endorsed by OpenAI.
