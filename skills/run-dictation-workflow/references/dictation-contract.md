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

Do not send an OCR/model candidate or corrected candidate to this endpoint. The extraction confirmation commit is the one formal write for image-derived items; after it commits, verify readback and do not submit those items again. Use this endpoint only for teacher-typed structured answers that have not already been written, and let the local exact matcher produce correct, wrong, or partial results.
