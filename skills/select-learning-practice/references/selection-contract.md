# Selection contract

`POST /api/grammar/select-passages`

```json
{
  "student_id": "STU-001",
  "target_codes": ["tense", "non_finite_voice"],
  "recent_error_days": 30,
  "max_passages": 5
}
```

Return the service result without hiding `uncovered`, `suggested_only`, mapping confidence, or source snapshot. The selected unit is always a complete passage.
