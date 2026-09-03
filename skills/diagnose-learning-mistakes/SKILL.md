---
name: diagnose-learning-mistakes
description: Diagnose the active learner's reading and exercise mistakes from stored item evidence while separating question knowledge points from student error causes. Use for wrong-question review, reading passage analysis, distractor diagnosis, tentative weak points, or writing model-suggested error causes back to a Cassian Atlas database.
---

# Diagnose Learning Mistakes

## Diagnose from evidence

1. Read the relevant question or full passage report. For reading, use `/api/reading/passages/{passage_id}/performance`.
2. Preserve the question's test points as content metadata; do not relabel them as the student's cause.
3. Inspect the captured student answer, standard answer, passage evidence, option difference, and existing diagnoses.
4. Select only supported causes from `/api/reading/error-types` and explain the evidence chain.
5. Submit agent conclusions to `/api/reading/diagnostics` with `error_source=model_suggested` and `verification_status=suggested`.
6. Re-read the passage report and confirm the diagnosis remains suggested.

If `answer_capture_status=not_captured`, return `blocked_not_captured` and do not infer a cause from the correct answer. Label a weakness supported by one wrong item as tentative and always report attempt and distinct-item sample sizes.

Read [diagnosis contract](references/diagnosis-contract.md) only when writing a diagnosis.
