# Page batch manifest

Use UTF-8 JSON. One manifest describes the expected answer regions on one page.

```json
{
  "prompt_version": "opentutor-transcription-page-v1",
  "page_label": "page-1",
  "items": [
    {
      "item_id": "Q1",
      "question_label": "1",
      "question_type": "multiple_choice",
      "risk_level": "R0",
      "bbox": [120, 240, 360, 310]
    },
    {
      "item_id": "Q2",
      "question_label": "2",
      "question_type": "short_free_text",
      "risk_level": "R2",
      "bbox": [120, 330, 950, 460]
    }
  ]
}
```

Rules:

- `item_id` must be unique within the page and must not contain the learner's identity.
- `risk_level` is one of `R0`, `R1`, `R2`, `R3`, `R4`.
- `bbox` is optional and uses `[left, top, right, bottom]` pixel coordinates.
- Do not include answer keys, scores, learner names, diagnoses, provider outputs, or local file paths.
- The output is a provider candidate. It is not a verified answer and must not be graded or committed directly.
