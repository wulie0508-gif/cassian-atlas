---
name: sync-learning-dashboard
description: Register and update specialist task runs in an OpenTutor Ledger dashboard without changing learning evidence. Use when a router or specialist agent starts, progresses, completes, fails, is cancelled, or needs user input, and when the dashboard must show current automation status without a handwritten handoff.
---

# Sync Learning Dashboard

Use the run ledger only for operational status.

1. Register a task once through `POST /api/agent/runs`, or reuse the run returned by the router.
2. Append events through `POST /api/agent/runs/{run_id}/events`.
3. Send `started` before material work, `progress` only for meaningful milestones, and exactly one terminal state.
4. Keep titles and summaries concise. Put durable evidence in `result_ref` rather than copying large results into the run log.
5. Verify the result in `GET /api/agent/dashboard`.

Never use the run ledger as proof that attempts, scores, error causes, or question mappings were stored. Read [run events](references/run-events.md) for payload fields.
