# Extraction confirmation contract

All commands require the owning learner ID. JSON files stay in private storage.

```powershell
opentutor extraction create --student STU-<ID> --input '<batch.json>'
opentutor extraction provider-submit --student STU-<ID> --batch <BATCH-ID> --input '<provider-result.json>'
opentutor extraction review --student STU-<ID> --batch <BATCH-ID> --output '<review.json>'
opentutor extraction decide --student STU-<ID> --batch <BATCH-ID> --input '<decisions.json>'
opentutor extraction commit --student STU-<ID> --batch <BATCH-ID> --input '<commit.json>'
opentutor extraction show --student STU-<ID> --batch <BATCH-ID>
```

The create payload owns `session_id`, `source_images`, and an ordered `items` array. Each item supplies `question_ref`, `question_type`, `risk_level`, `evidence_locator`, and an `attempt_template` without `student_answer`, `answer_capture_status`, `evaluation`, or `error_types`. Use R0 for a clear constrained mark, R1 for a clear low-ambiguity short response, R2 for handwritten or ambiguous short free text such as a handwritten cloze, R3 for translation/writing/long free text, and R4 for unreadable, cut-off, or misaligned evidence. During the current cold-start contract, R1/R2/R3 cannot disable `second_model_required`; R1 can move to anomaly-triggered dual extraction only after a future persisted learner-and-question-type calibration gate is implemented. Never downgrade risk to work around an unavailable provider.

Provider identity is closed to `deterministic`, `codex`, and `doubao`. Provider submissions are append-only and include provider/model/prompt versions, request hash, result status, raw and normalized transcription, capture status, uncertain spans, alternatives, confidence, locator, and completion time. Failed, unconfigured, timed-out, or rate-limited calls need an error summary. Review and commit use the newest appended row for each provider. Current R1 and all R2/R3 readiness specifically require the current Codex result and current Doubao result both to be successful; repeats, aliases, and deterministic output do not satisfy that pair, while a newer failed/unconfigured/timed-out/rate-limited result revokes an older success.

A decision submission names the current `review_version`, a stable idempotency key, the teacher actor, and per-item actions:

- `human_confirmed`: exact selected provider text.
- `human_corrected`: teacher-supplied text; raw provider rows remain unchanged.
- `confirmed_blank`: the teacher verified a captured blank.
- `not_captured` or `rejected_alignment`: exclude the item from formal facts.
- `pending_review` or `needs_check`: retain the item and block commit.

Subjective grading uses an explicit teacher-confirmed evaluation. Deterministic grading may run only after transcription confirmation. Commit is transactional and idempotent; a changed retry payload is a conflict, not a second release. Use only the public CLI/API and quality gate for readiness. Direct SQLite writes are unsupported, and a caller-owned transaction remains owned by that caller through nested extraction savepoints.
