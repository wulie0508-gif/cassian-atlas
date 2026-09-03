# Courseware endpoints

| Need | Endpoint |
| --- | --- |
| Compact starting context | `GET /api/context/courseware` |
| Weighted mastery | `GET /api/mastery` |
| Weekly evidence | `GET /api/reports/weekly` |
| Search verified questions | `GET /api/questions?q=...&type=...&status=source_checked` |
| One question and mappings | `GET /api/questions/{question_id}` |
| Complete-passage coverage | `GET /api/grammar/passages/{passage_id}/coverage` |
| Teaching/material search | `GET /api/library/search?q=...` |

Keep formal exams and topic quizzes in separate reporting series. Include denominators for every weakness claim.
