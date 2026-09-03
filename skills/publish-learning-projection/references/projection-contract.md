# Projection contract

Inspect the current whitelist rather than inventing fields:

```powershell
opentutor projection contract
```

Minimal staging envelope:

```json
{
  "idempotency_key": "projection:STU-001:overview:2026-09-02:v1",
  "projection_name": "student_overview",
  "student_id": "STU-001",
  "subject_code": "english",
  "data_as_of": "2026-09-02T09:00:00+08:00",
  "publisher": "opentutor_local_publisher",
  "records": [
    {
      "metric_version": "overview-v1",
      "freshness_status": "FRESH",
      "sample_size": 12,
      "is_active": true,
      "session_count": 4,
      "attempt_count": 12,
      "scored_attempt_count": 12,
      "accuracy": 0.75,
      "review_due_count": 3,
      "last_activity_at": "2026-09-02T08:30:00+08:00"
    }
  ]
}
```

The stable projection key is derived from the contract version, projection family, learner, subject, and family key fields. Retrying an identical idempotency key returns the same run; reusing it for different content is a conflict.

`claim`, `receipt`, and their retry states are transport handshakes. A successful receipt requires `readback_payload_sha256` to equal the staged payload hash exactly. `retryable_failed` records a bounded next-attempt time; `permanent_failed` closes the record. Neither outcome mutates learning evidence.
