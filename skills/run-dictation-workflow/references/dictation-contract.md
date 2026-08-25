# Dictation contract

Fetch `GET /api/dictation/plan?limit=20`, retaining each returned `item_id`.

Submit `POST /api/dictation/results`:

```json
{
  "student_id": "STU-001",
  "title": "Weekly dictation",
  "date": "2026-08-03",
  "delivery_mode": "offline_closed",
  "items": [
    {"item_id": "ITEM-...", "student_answer": "raw OCR or typed answer"}
  ]
}
```

Do not send a corrected answer as `student_answer`. Let the local exact matcher produce correct, wrong, or partial results.
