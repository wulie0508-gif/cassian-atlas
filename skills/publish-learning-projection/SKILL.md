---
name: publish-learning-projection
description: Validate and stage learner-scoped Cassian Atlas operational projections for the Cassian Learning Lab Feishu Base outbox. Use for Base dashboards, parent-facing operational summaries, projection freshness, retry/readback status, or a requested Feishu learning-data sync; never use it to publish questions, answers, explanations, raw submissions, or diagnoses.
---

# Publish Learning Projection

Treat the local Cassian Atlas ledger as the only fact source and Feishu Base as an operational projection. The default action is local validation and staging, not an online write.

## Stage safely

1. Resolve one explicit `STU-*` learner and subject. Never infer ownership from a display name, folder title, or active browser page.
2. Read the repository's current target config at `00_总览与模板/飞书同步/当前飞书同步目标.json` immediately before every stage or publish attempt.
3. Run the redacted preflight with `opentutor projection target-check --student <STU-ID> --input '<target-config.json>'`. Stop on any tenant, app, profile, identity, folder, Base, host, or fingerprint mismatch.
4. Build only one of the versioned operational projection shapes. Use the exact freshness values `FRESH`, `DELAYED`, `STALE`, or `FAILED` and an explicit `data_as_of` timestamp.
5. Stage with `opentutor projection stage --student <STU-ID> --input '<projection.json>' --target-config '<target-config.json>'`. Re-read the returned run with `opentutor projection show --student <STU-ID> --run <RUN-ID>`.

Allowed projection families are student overview, period metrics, knowledge performance, retest summary, data quality, generation runs, and the teacher policy/correction inbox. The whitelist accepts aggregate operational fields only. Never add question text, passages, answers, answer keys, explanations, raw model output, images, audio, OCR, student names, diagnoses, or local/private paths.

## Keep online delivery separate

Do not claim an outbox record merely to inspect it: claiming changes retry state. The current repository provides a local outbox/readback contract but no authorized live Feishu transport. Leave staged rows pending unless the user separately requests an online publish and a verified publisher is available.

Before any later online write:

- verify `lark-cli profile list` and the read-only identity for `cassian-learning-hub`;
- require the target app `Cassian Learning Ops`, tenant `Cassian Learning Lab | 学习工作室`, identity `user`, and write flags `--profile cassian-learning-hub --as user`;
- obtain explicit approval for the actual remote mutation;
- accept success only after a persisted remote readback normalizes to the exact staged payload SHA-256 and the remote record ID agrees with prior state.

Never fall back to another profile, tenant, folder, or Base. A failed, stale, or ambiguous projection does not change local learning facts.

Read [the projection contract](references/projection-contract.md) before constructing a payload or interpreting delivery state.
