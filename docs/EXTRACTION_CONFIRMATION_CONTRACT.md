# Extraction, Confirmation, and Commit Contract

Status: implemented contract for `extraction-v1`
Comparison policy: `transcription-compare-v1`
Audience: provider-integrator, CLI/API, review-UI, and ledger maintainers

## 1. Scope and governing invariant

This document defines the durable boundary between image-based transcription candidates and formal Cassian Atlas learning evidence. It covers batch creation, provider submissions, comparison and human review, atomic commit, the Doubao multimodal adapter, privacy, retries, idempotency, and failure handling.

The governing invariant is:

> A model result is only an immutable transcription candidate. It MUST NOT become an attempt, evaluation, error type, review event, or other formal learning fact until every item in its batch has a terminal human decision and the whole batch commits atomically.

In particular:

- A provider transcribes visible learner writing. It does not grade, diagnose, infer missing text, or correct toward a reference answer.
- Before commit, `attempts` and `evaluations` contain no facts from the extraction batch.
- A teacher-confirmed text or confirmed blank is the only source of a committed learner answer.
- `not_captured` and `rejected_alignment` close the review item without inventing an attempt or evaluation.
- A partially valid batch does not partially commit. Any commit failure rolls back the entire formal-fact write.

This contract does not define image acquisition, OCR-provider account provisioning, post-commit mistake diagnosis, or external dashboard projection.

## 2. Durable records and authority

| Record | Meaning | Mutation rule | Authority |
| --- | --- | --- | --- |
| `extraction_batches` | Learner-, subject-, session-, and source-scoped unit of work | State/review metadata may change while open; committed batches are immutable and batches cannot be deleted | Workflow state |
| `extraction_assets` | Private source reference, media metadata, and source SHA-256 | Append-only, no delete | Source provenance |
| `extraction_items` | Ordered question, risk gate, evidence locator, and pre-fact attempt template | Append-only, no delete | Review coverage |
| `extraction_provider_results` | One provider's candidate or explicit failure | Append-only, no update/delete | Machine evidence only |
| `extraction_confirmation_decisions` | Human action at a numbered revision | Append a new revision; never overwrite/delete | Human confirmation |
| `extraction_commit_links` | Exact lineage from item/decision to attempt/evaluation/ingest event | Immutable, no delete | Commit proof |
| `attempts`, `evaluations` | Formal learning evidence | Created only by atomic batch commit | Ledger fact |

The current decision for an item is its highest `revision_no`. Old provider results and old decisions remain available for audit; they do not silently change meaning.

Cassian Atlas 0.5.0 is a trusted local-operator system, not an authenticated multi-user service. `provider`, `model_version`, and `actor` are append-only provenance claims but are not cryptographically authenticated identities. Keep the write API local, use the provider adapter rather than hand-labeling results, and let the confirmation skill create a decision payload only after an explicit teacher response to the displayed review. This trust boundary never turns silence, model agreement, or a caller-supplied actor string into teacher approval.

Batch identity and ownership are immutable. Every operation MUST name the same explicit `student_id`; the batch's session MUST be active and belong to that learner, and the learner MUST have an active enrollment in the subject.

## 3. Batch state machine

The persisted status vocabulary is:

`draft`, `extracting`, `pending_review`, `ready_to_commit`, `committed`, `cancelled`, `failed`.

The implemented public workflow is:

```text
create
  |
  v
draft --provider-submit--> pending_review
  |                            |       ^
  +---------decide------------+       |
                               \      | teacher appends a new
                                v     | nonterminal revision
                         ready_to_commit
                                |
                                | commit (one transaction)
                                v
                            committed
```

| Event | Allowed source | Result | Required conditions |
| --- | --- | --- | --- |
| Create batch | No existing batch for the idempotency key | `draft` | Explicit learner/session, at least one asset and item, valid hashes/templates |
| Submit provider results | Any nonterminal batch | `pending_review` | Nonempty result list; each item belongs to the batch and has no current terminal decision |
| Submit human decisions | Any nonterminal batch | `ready_to_commit` or `pending_review` | Optimistic `review_version` matches; each submitted action is valid |
| Revise a pre-commit decision | `draft`, `pending_review`, or `ready_to_commit` | Recomputed as `ready_to_commit` or `pending_review` | Append a new decision revision; never edit the prior row |
| Commit | `ready_to_commit` | `committed` | Every item terminal and ready, at least one committable item, complete atomic readback |
| Any provider/decision/commit write | `committed`, `cancelled`, or `failed` | Rejected | Terminal states accept no further workflow evidence |

`extracting` is a schema-reserved status; the current public service does not set it. `cancelled` and `failed` are terminal schema states; the current public CLI/API exposes no cancellation or failure-transition command. Clients MUST NOT infer an undocumented transition into these states.

The database independently enforces `ready_to_commit -> committed`; calling code cannot bypass the guard by directly changing status. Direct production SQLite writes are outside this contract.

### Review-version concurrency

- A new batch starts at `review_version = 1`.
- Every non-duplicate provider submission increments the version once.
- Every non-duplicate decision submission increments the version once, regardless of how many item decisions it contains.
- Decision and commit requests MUST use the version returned by the latest review response.
- A stale version is a conflict, not an instruction to overwrite the newer review.

## 4. Item decision state machine

An unresolved item has no decision or has a current nonterminal decision:

| Action | Terminal | Creates a formal fact at commit | Requirements |
| --- | --- | --- | --- |
| `pending_review` | No | No | Carries no confirmed text or evaluation |
| `needs_check` | No | No | Carries no confirmed text or evaluation |
| `human_confirmed` | Yes | Yes | Selects a successful provider result; text is exactly its raw or normalized candidate |
| `human_corrected` | Yes | Yes | Requires explicit teacher-confirmed text; provider selection is optional lineage |
| `confirmed_blank` | Yes | Yes | Confirmed text is exactly empty; commits `answer_capture_status = captured_blank` |
| `not_captured` | Yes | No | Carries no text/evaluation; records that no trustworthy answer was captured |
| `rejected_alignment` | Yes | No | Carries no text/evaluation; records that crop/question alignment was rejected |

`human_confirmed` MUST NOT be used for edited text. If the teacher changes even one character from the selected raw/normalized candidate, the action is `human_corrected`.

An explicit pre-commit decision may append a newer revision, including reopening a previously terminal item with `needs_check` or `pending_review`. Provider submission itself is rejected while the item's current decision is terminal. When genuinely new model evidence is needed after a terminal decision, callers SHOULD follow the service error and create a new batch rather than use decision revision merely to bypass that gate.

## 5. Provider evidence, comparison, and review grouping

### Provider result vocabulary

`result_status` is one of:

- `succeeded`
- `failed`
- `unconfigured`
- `timeout`
- `rate_limited`

A successful result requires a `capture_status`. An unsuccessful result requires a nonempty, redacted `error_summary` and never counts as a transcription candidate.

`capture_status` is one of:

- `captured`
- `captured_blank`
- `not_captured`
- `needs_check`
- `blocked_image_quality`
- `blocked_alignment`

`captured` requires a transcription value. `captured_blank` requires an empty transcription. A provider's `not_captured` remains only machine evidence; only the human decision `not_captured` terminally excludes the item.

### Active evidence and comparison

Review and commit readiness use the newest appended result for each provider and only successful current results as candidates. Durable SQLite append order is the tie-breaker when timestamps are equal; caller-supplied result IDs are never freshness clocks. If a newer Codex or Doubao row is `failed`, `unconfigured`, `timeout`, or `rate_limited`, that provider is not ready even when an older successful row remains in the audit history. Provider failures remain visible and are counted, but never masquerade as success.

The comparison classification is one of:

| Classification | Meaning |
| --- | --- |
| `missing_candidate` | No successful provider candidate |
| `single_candidate` | One success and no second-provider gate |
| `blocked_second_model` | One success but two independent providers are required |
| `alignment_conflict` | A provider reports blocked alignment |
| `blank_conflict` | Providers disagree about blank/not-captured state |
| `uncertain` | A provider reports uncertainty or image-quality blockage |
| `exact_match` | Two candidates are byte-for-byte equal |
| `ignorable_difference` | Candidates match after the item's explicit comparison policy |
| `content_conflict` | Candidates differ materially; diff spans are returned |

An unresolved item is in the `ordinary` review group only when its classification is `single_candidate`, `exact_match`, or `ignorable_difference` and its second-model gate is satisfied. Every other unresolved item is `attention`.

`default_action = accept_prefill` may confirm unresolved ordinary items that have a prefill candidate. It cannot decide attention items. In a mixed batch, provide explicit decisions for all attention items and use the default only for the remaining ordinary items.

The review endpoint returns `standard_answers_hidden = true`. It exposes candidates and evidence for confirmation, not the answer key stored inside the local attempt template.

### Second-provider gate

Provider identity is closed to `codex`, `doubao`, and `deterministic`. Items explicitly marked `second_model_required`, every risk-level `R2`/`R3` item, and current cold-start `R1` items require the current Codex result and the current Doubao result both to be successful before a committable decision can become ready. An `R1`/`R2`/`R3` create request that tries to set the flag false is rejected. R1 may move to anomaly-triggered dual extraction only after a future persisted learner-and-question-type calibration gate proves the agreed threshold. Two calls to the same provider—or relabeling a deterministic result—do not satisfy the gate, and a newer unsuccessful row revokes that provider's earlier readiness.

Submit both providers before a terminal committable decision. Otherwise the item remains unready, and provider submission is then blocked by the terminal-decision guard. The terminal exclusion actions `not_captured` and `rejected_alignment` do not require two provider successes because they create no formal fact.

## 6. Evaluation and commit gate

The workflow separates transcription from grading:

1. Providers transcribe without a standard answer.
2. A human confirms, corrects, confirms blank, or excludes the item.
3. Only confirmed text is graded.
4. The batch commits the confirmed answer and its evaluation together.

For grading contracts with `mode = deterministic_exact` or `exact`, the service may derive `correct` or `wrong` after confirmation using the local acceptable-answer policy. Subjective contracts, including teacher rubrics, require an explicit human evaluation with result `correct`, `partial`, or `wrong`.

`can_commit` is true only when:

- the number of extraction items equals `expected_item_count`;
- every item has a current terminal human decision;
- every committable decision has a valid evaluation;
- every second-model-required committable item has successful current independent Codex and Doubao results; and
- at least one item is committable.

Commit then performs one SQLite transaction that:

1. Re-reads learner ownership, status, and `review_version` inside the transaction.
2. Converts only `human_confirmed`, `human_corrected`, and `confirmed_blank` into attempt payloads.
3. Imports one idempotent ingest envelope for the whole batch.
4. Reads back each active attempt and its current evaluation.
5. Inserts one exact commit link per committable item.
6. Verifies that the readback count equals the committable count and that there are no extra unlinked attempts in the ingest event.
7. Marks the batch `committed` with commit hash, ingest event, and timestamp.

Any validation, ingest, link, or readback failure rolls back all seven effects. If a library caller already owns a wider SQLite transaction, extraction commit uses savepoints: it neither commits unrelated caller work on success nor rolls unrelated caller work back on failure. Excluded items remain auditable extraction decisions but have no attempt, evaluation, or commit link.

## 7. Doubao multimodal adapter boundary

`DoubaoMultimodalAdapter` is a mock-first adapter. It has no default HTTP transport and no default sleeper; callers must inject both. Tests inject a fake transport and MUST NOT perform real network calls.

### Configuration

`DoubaoConfig.from_env` reads only these variables:

| Variable | Required | Validation/default |
| --- | --- | --- |
| `OPEN_TUTOR_DOUBAO_API_KEY` | Yes for calls | No default; private field, omitted from `repr` and summaries |
| `OPEN_TUTOR_DOUBAO_BASE_URL` | Yes for calls | HTTPS URL, no embedded username/password |
| `OPEN_TUTOR_DOUBAO_MODEL` | Yes for calls | No default |
| `OPEN_TUTOR_DOUBAO_TIMEOUT_SECONDS` | No | Default `30`, range `0.1..300` |
| `OPEN_TUTOR_DOUBAO_MAX_RETRIES` | No | Default `2`, range `0..5` |
| `OPEN_TUTOR_DOUBAO_RETRY_BASE_SECONDS` | No | Default `0.5`, range `0..60` |

If key, base URL, or model is missing, extraction returns `unconfigured` with a safe reason and invokes the injected transport zero times. Configuration must not fall back to generic provider variables or credentials for another service.

### Outbound request allowlist

The adapter accepts JPEG, PNG, or WebP crop bytes and verifies an optional caller-supplied image SHA-256. Its provider-facing request contains only:

- configured model;
- prompt version;
- opaque question locator and question type;
- expected response format;
- allowlisted locator fields (`bbox`, `crop`, page, question label, region ID, or source image ID);
- image crop bytes encoded as a data URL; and
- an instruction to transcribe visible writing only and return structured JSON.

The adapter recursively rejects request fields for standard answers/answer keys, other or peer provider results, learner names/display names, and private/local paths. Unknown locator keys are dropped. Credentials appear only in the outbound authorization header and are hidden by request representations.

### Success mapping and cache identity

A valid success maps into the Stage 1 provider-result fields: provider/model/prompt identity, extraction item/question locator, raw and normalized transcription, capture status, uncertain spans, alternatives, confidence, evidence locator, request/response SHA-256, redacted raw output, completion time, and attempt count.

The cache key hashes:

`provider + model_version + prompt_version + image_sha256 + question_locator`

It contains neither the credential nor raw image bytes. A model, prompt, image, provider, or question-locator change therefore produces a distinct cache identity.

### Retry and fail-closed behavior

| Condition | Retry? | Final status |
| --- | --- | --- |
| Timeout | Up to configured retry limit | `timeout` when exhausted |
| HTTP 429 | Up to configured retry limit | `rate_limited` when exhausted |
| HTTP 5xx | Up to configured retry limit | `failed` when exhausted |
| HTTP 401/403 | No | `failed` with auth summary |
| Other HTTP 4xx | No | `failed` with HTTP summary |
| Malformed 2xx response | No | `failed` as malformed response |
| Other transport exception | No | `failed` with redacted transport summary |

Retry delay is bounded exponential backoff: `retry_base_seconds * 2^(attempt_number - 1)`. No failure path synthesizes a successful transcription.

## 8. Privacy and credential boundary

The local ledger is the system of record. The provider boundary is deliberately narrower than the local review boundary.

| Data | Local batch/review | External provider request | Public/external projection |
| --- | --- | --- | --- |
| Opaque `STU-*` ownership | Required | Not needed | Only when explicitly authorized |
| Learner name/contact | Not required by extraction | Forbidden | Outside this contract |
| Private source URI/path | Stored for local provenance and may appear in trusted local review | Forbidden | Forbidden |
| Image crop bytes | Local transient input/source asset | Allowed, minimum necessary crop | Forbidden |
| Standard answer/rubric | Local attempt template, hidden during review | Forbidden | Outside this contract |
| Other provider results | Local comparison only | Forbidden | Forbidden |
| Candidate raw output | Local immutable audit evidence, redacted | Returned by that provider | Not a formal fact; do not project |
| Confirmed answer/evaluation | Created at commit | Never sent for transcription | Governed by downstream privacy policy |

Operational requirements:

- Keep provider secrets in ignored environment configuration, never JSON fixtures, source, logs, exceptions, output payloads, or commit history.
- Do not place a real learner image, database, document, audio file, archive, or credential file in the repository.
- Treat review/detail API responses as trusted-local data: they can contain source URIs and candidate transcriptions and must not be exposed as a public web API. Stored raw provider output remains local audit data and is not returned by the compact review shape.
- Use explicit `STU-*` learner ownership for every CLI/API call. Never select a learner from a display name or an implicit “current learner.”
- CLI/API mutation paths create a database backup before writing. Reads use read-only connections.
- Never directly edit the production SQLite database. Use the CLI/API contract so idempotency, ownership, backup, validation, and database triggers all execute. The public application readiness check and `opentutor data check` quality gate are authoritative for current-provider semantics; migration-era triggers are defense in depth, not a supported direct-SQL interface.
- Error summaries and raw provider output must redact the exact API key, bearer tokens, authorization values, and common token/key fields before persistence or display.

## 9. Idempotency and failure semantics

| Operation | Idempotency scope | Same key + same normalized payload | Same key + different payload | Other concurrency/failure rule |
| --- | --- | --- | --- | --- |
| Create batch | Global batch key; includes learner-owned normalized request hash | Returns `duplicate` and existing batch detail | Conflict, including cross-learner reuse | No second batch or assets/items are inserted |
| Provider submit | Submission key, bound to one batch; item rows derive stable row keys | Returns `duplicate` and current review | Conflict, including another batch | Nonempty results; operation is one transaction; review version increments once only when new |
| Human decisions | Submission key, bound to one batch and review version | Returns `duplicate` and current review | Conflict, including another batch | Stale `expected_review_version` conflicts; decision revisions append atomically |
| Commit | One commit key and normalized commit request per batch | Returns `duplicate` with original readback | Conflict if key or payload differs | Stale review conflicts; all formal facts and links commit or roll back together |

Top-level extraction mutations acquire a SQLite `BEGIN IMMEDIATE` boundary so the idempotency lookup and insert serialize across CLI/API connections. A caller-owned transaction uses nested savepoints instead. SQLite busy/locked outcomes are exposed as `ExtractionConflict`, never as raw lock errors; the caller retains ownership of its surrounding transaction.

Callers should classify errors as follows:

- **Validation error:** malformed field, invalid enum/hash, missing membership, inconsistent transcription/capture status, or impossible decision. Fix the request; do not retry unchanged.
- **Conflict:** idempotency mismatch, stale review version, terminal batch/item, unmet readiness, or ownership mismatch. Re-read batch/review before deciding what to do.
- **Provider operational failure:** persist a failed provider result with the correct status and redacted summary; do not convert it into success.
- **Transient provider failure:** only the adapter's timeout/429/5xx policy may retry, and only within its configured finite bound.
- **Commit failure:** assume no partial formal facts were committed; re-read the batch and ledger before retrying with the same idempotency key.

## 10. CLI and local HTTP surface

All CLI commands require explicit learner ownership:

```powershell
opentutor extraction create --student STU-001 --input <create.json>
opentutor extraction provider-submit --student STU-001 --batch <batch-id> --input <provider.json>
opentutor extraction review --student STU-001 --batch <batch-id> --output <review.json>
opentutor extraction decide --student STU-001 --batch <batch-id> --input <decisions.json>
opentutor extraction commit --student STU-001 --batch <batch-id> --input <commit.json>
opentutor extraction show --student STU-001 --batch <batch-id>
```

The equivalent trusted-local HTTP endpoints are:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/extraction/batches` | Create batch |
| `POST` | `/api/extraction/batches/{batch_id}/provider-results` | Append provider evidence |
| `GET` | `/api/extraction/batches/{batch_id}/review?student_id=STU-001` | Read compact review |
| `POST` | `/api/extraction/batches/{batch_id}/decisions` | Append human decisions |
| `POST` | `/api/extraction/batches/{batch_id}/commit` | Atomically create formal facts |
| `GET` | `/api/extraction/batches/{batch_id}?student_id=STU-001` | Read full batch audit detail |

POST bodies also carry `student_id`; the service does not permit an implicit server-selected learner for these writes.

## 11. Anonymous end-to-end example

This example uses synthetic identifiers and hashes. It demonstrates one ordinary objective item, one two-provider subjective item, and one excluded unreadable item. The referenced learner enrollment and session already exist.

### 11.1 Create the batch

```json
{
  "idempotency_key": "example:extraction:create:v1",
  "extraction_batch_id": "XBAT-EXAMPLE-001",
  "student_id": "STU-001",
  "subject_code": "english",
  "session_id": "SES-EXAMPLE-001",
  "title": "Anonymous mixed extraction",
  "source_thread": "courseware",
  "source_images": [
    {
      "extraction_asset_id": "XAST-EXAMPLE-001",
      "source_uri": "private://anonymous-fixture/page-1.png",
      "sha256": "7777777777777777777777777777777777777777777777777777777777777777",
      "media_type": "image/png",
      "byte_size": 256,
      "page_number": 1
    }
  ],
  "items": [
    {
      "extraction_item_id": "XITEM-EXAMPLE-R0",
      "extraction_asset_id": "XAST-EXAMPLE-001",
      "ordinal": 1,
      "question_ref": "Q-1",
      "question_type": "multiple_choice",
      "risk_level": "R0",
      "second_model_required": false,
      "evidence_locator": {"page": 1, "region": [0, 0, 20, 20]},
      "attempt_template": {
        "attempted_at": "2026-09-02T10:10:00+08:00",
        "standard_answer": "B",
        "response_mode": "recognition",
        "validation_status": "verified",
        "grading_contract": {
          "mode": "deterministic_exact",
          "acceptable_answers": ["B"],
          "max_score": 1
        },
        "item": {
          "item_id": "ITEM-EXAMPLE-R0",
          "domain": "grammar",
          "item_type": "multiple_choice",
          "prompt_snapshot": "Anonymous objective prompt",
          "answer_snapshot": "B"
        }
      }
    },
    {
      "extraction_item_id": "XITEM-EXAMPLE-R3",
      "extraction_asset_id": "XAST-EXAMPLE-001",
      "ordinal": 2,
      "question_ref": "Q-2",
      "question_type": "translation",
      "risk_level": "R3",
      "second_model_required": true,
      "second_model_reason": "long_answer",
      "evidence_locator": {"page": 1, "region": [0, 20, 100, 80]},
      "attempt_template": {
        "attempted_at": "2026-09-02T10:11:00+08:00",
        "standard_answer": "A source-checked reference",
        "response_mode": "production",
        "validation_status": "verified",
        "grading_contract": {"mode": "teacher_rubric"},
        "item": {
          "item_id": "ITEM-EXAMPLE-R3",
          "domain": "translation",
          "item_type": "translation",
          "prompt_snapshot": "Anonymous translation prompt",
          "answer_snapshot": "A source-checked reference"
        }
      }
    },
    {
      "extraction_item_id": "XITEM-EXAMPLE-R4",
      "extraction_asset_id": "XAST-EXAMPLE-001",
      "ordinal": 3,
      "question_ref": "Q-3",
      "question_type": "short_answer",
      "risk_level": "R4",
      "second_model_required": false,
      "evidence_locator": {"page": 1, "region": [0, 80, 100, 100]},
      "attempt_template": {
        "attempted_at": "2026-09-02T10:12:00+08:00",
        "standard_answer": "reference",
        "response_mode": "production",
        "validation_status": "verified",
        "grading_contract": {"mode": "teacher_rubric"},
        "item": {
          "item_id": "ITEM-EXAMPLE-R4",
          "domain": "reading",
          "item_type": "short_answer",
          "prompt_snapshot": "Anonymous unreadable prompt region",
          "answer_snapshot": "reference"
        }
      }
    }
  ]
}
```

Expected state: `draft`, `review_version = 1`, and zero new attempts/evaluations.

### 11.2 Submit the first provider

```json
{
  "idempotency_key": "example:provider:codex:v1",
  "provider": "codex",
  "model_version": "example-model-v1",
  "prompt_version": "transcription-v1",
  "completed_at": "2026-09-02T02:13:00Z",
  "results": [
    {
      "provider_result_id": "XPR-EXAMPLE-CODEX-R0",
      "extraction_item_id": "XITEM-EXAMPLE-R0",
      "request_sha256": "8888888888888888888888888888888888888888888888888888888888888888",
      "response_sha256": "9999999999999999999999999999999999999999999999999999999999999999",
      "result_status": "succeeded",
      "raw_transcription": "B",
      "normalized_transcription": "B",
      "capture_status": "captured",
      "uncertain_spans": [],
      "candidate_alternatives": [],
      "confidence": 0.99,
      "evidence_locator": {"page": 1, "bbox": [0, 0, 20, 20]}
    },
    {
      "provider_result_id": "XPR-EXAMPLE-CODEX-R3",
      "extraction_item_id": "XITEM-EXAMPLE-R3",
      "request_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "response_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "result_status": "succeeded",
      "raw_transcription": "Candidate sentence one.",
      "normalized_transcription": "Candidate sentence one.",
      "capture_status": "captured",
      "uncertain_spans": [],
      "candidate_alternatives": [],
      "confidence": 0.91,
      "evidence_locator": {"page": 1, "bbox": [0, 20, 100, 80]}
    }
  ]
}
```

Expected state: `pending_review`, version incremented once. The R3 item is `blocked_second_model` and cannot yet commit.

### 11.3 Submit the independent second provider for R3

The real Doubao adapter receives only the R3 crop and minimal metadata. It does not receive any `standard_answer`, the Codex result, learner name, or private URI.

```json
{
  "idempotency_key": "example:provider:doubao:v1",
  "provider": "doubao",
  "model_version": "example-doubao-model-v1",
  "prompt_version": "transcription-v1",
  "completed_at": "2026-09-02T02:14:00Z",
  "results": [
    {
      "provider_result_id": "XPR-EXAMPLE-DOUBAO-R3",
      "extraction_item_id": "XITEM-EXAMPLE-R3",
      "request_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "response_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "result_status": "succeeded",
      "raw_transcription": "Candidate sentence won.",
      "normalized_transcription": "Candidate sentence won.",
      "capture_status": "captured",
      "uncertain_spans": [],
      "candidate_alternatives": [],
      "confidence": 0.88,
      "evidence_locator": {"page": 1, "bbox": [0, 20, 100, 80]}
    }
  ]
}
```

Expected review:

- R0: `single_candidate`, `ordinary`, with a prefill.
- R3: two distinct successes, `content_conflict`, `attention`, with diff spans.
- R4: `missing_candidate`, `attention`.
- `standard_answers_hidden = true` and still zero new attempts/evaluations.

The exact `review_version` MUST be read from this review response rather than inferred from the example.

### 11.4 Submit one review-version decision set

```json
{
  "idempotency_key": "example:decisions:v1",
  "expected_review_version": 3,
  "actor": "teacher",
  "decisions": [
    {
      "extraction_item_id": "XITEM-EXAMPLE-R0",
      "action": "human_confirmed",
      "selected_provider_result_id": "XPR-EXAMPLE-CODEX-R0",
      "reason": "Visible mark verified"
    },
    {
      "extraction_item_id": "XITEM-EXAMPLE-R3",
      "action": "human_corrected",
      "selected_provider_result_id": "XPR-EXAMPLE-CODEX-R3",
      "confirmed_text": "Teacher-confirmed sentence.",
      "evaluation": {
        "result": "partial",
        "score": 2,
        "max_score": 3,
        "evaluated_by": "teacher",
        "is_human_corrected": true,
        "note": "Meaning preserved; wording incomplete"
      },
      "reason": "Compared crop and both candidates"
    },
    {
      "extraction_item_id": "XITEM-EXAMPLE-R4",
      "action": "not_captured",
      "reason": "Image region is unreadable"
    }
  ]
}
```

Expected state: `ready_to_commit`; the version increments once; `can_commit = true`; still zero new attempts/evaluations. If another writer has changed the review, version `3` conflicts and the client must re-read rather than overwrite.

### 11.5 Commit and verify readback

Use the version returned by the decision response:

```json
{
  "idempotency_key": "example:commit:v1",
  "expected_review_version": 4,
  "actor": "teacher"
}
```

Expected outcome:

- batch state `committed`;
- `attempts_inserted = 2` for R0 and R3;
- `excluded_items = 1` for R4;
- readback count `2` with exactly two commit links;
- no attempt/evaluation/link for R4; and
- replaying the same commit payload returns `duplicate` with the same readback and inserts nothing new.

## 12. Contract verification checklist

An implementation change is compatible only if tests continue to prove:

- no attempts/evaluations exist before commit;
- mixed ordinary/attention review is complete and deterministic;
- default prefill never accepts attention items;
- second-provider-required items cannot commit without successful independent Codex and Doubao results;
- provider failures and malformed responses never become success;
- terminal exclusion creates no formal fact;
- stale review versions and idempotency-key conflicts fail closed;
- commit is atomic, read back, linked, and duplicate-safe;
- database triggers reject direct invalid commit and append-only mutation;
- the Doubao transport is always injected and unit tests make zero real network calls;
- provider requests exclude answer keys, peer outputs, learner names, and private paths;
- API keys and tokens never appear in `repr`, results, raw output, errors, repository text, or fixtures; and
- CLI/API mutations back up the local database while reads remain read-only.

The relevant automated coverage lives in `tests/test_extraction_schema.py`, `tests/test_extraction_confirmation.py`, `tests/test_extraction_cli_http.py`, `tests/test_doubao_adapter.py`, and `tests/test_contracts_and_privacy.py`.
