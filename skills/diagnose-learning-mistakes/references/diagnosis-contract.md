# Diagnosis contract

`POST /api/reading/diagnostics`

```json
{
  "event_id": "EVT-UNIQUE",
  "idempotency_key": "courseware:reading-diagnostic:attempt-id:v1",
  "source_thread": "courseware",
  "student_id": "STU-001",
  "diagnostics": [{
    "attempt_id": "ATT-...",
    "error_types": [{
      "code": "reading_inference_overreach",
      "confidence": 0.82,
      "error_source": "model_suggested",
      "verification_status": "suggested",
      "rationale": "State the option claim, the passage evidence, and the unsupported leap."
    }]
  }]
}
```

Do not submit diagnoses for correct attempts or not-captured answers. Only explicit teacher confirmation may use `teacher_observation` and `verified`.
