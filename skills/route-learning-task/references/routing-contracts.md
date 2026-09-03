# Routing contracts

Base URL: `http://127.0.0.1:8788`

## Route and register

`POST /api/agent/route`

```json
{
  "request_text": "Record today's reading results and diagnose captured wrong answers.",
  "student_id": "STU-001",
  "subject_code": "english",
  "source_thread": "courseware",
  "idempotency_key": "courseware:2026-08-03:reading:v1",
  "title": "Reading lesson evidence",
  "register": true
}
```

Reuse the idempotency key only for an identical logical request.

## Append status

`POST /api/agent/runs/{run_id}/events`

```json
{
  "student_id": "STU-001",
  "event_type": "completed",
  "idempotency_key": "RUN-EXAMPLE-001:completed:v1",
  "capability_key": "evidence-recording",
  "actor": "courseware-agent",
  "message": "Item-level attempts were stored and re-read.",
  "summary": "Stored 10 attempts; 8 correct and 2 wrong.",
  "result_ref": "/api/performance/sessions"
}
```

Reuse the event idempotency key only for an identical logical event on the same owning learner. `student_id` and `event_type` are required; capability, actor, and message may inherit safe run defaults when omitted.

Terminal states are `completed`, `failed`, and `cancelled`. `needs_input` is a resumable waiting state.
