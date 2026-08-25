# Run events

Create with `POST /api/agent/runs` using `request_text`, `student_id`, `subject_code`, `source_thread`, `idempotency_key`, and a short `title`.

Append to `POST /api/agent/runs/{run_id}/events`:

```json
{
  "event_type": "progress",
  "capability_key": "practice-selection",
  "actor": "selection-agent",
  "message": "Coverage targets resolved; selecting complete passages.",
  "details": {"target_count": 4}
}
```

Use `summary` and `result_ref` on terminal events. Allowed event types: `started`, `progress`, `needs_input`, `completed`, `failed`, `cancelled`.
