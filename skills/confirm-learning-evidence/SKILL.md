---
name: confirm-learning-evidence
description: Extract learner answers from images or model/OCR candidates, compare independent providers when risk requires it, and obtain complete batch-level teacher confirmation before committing formal Cassian Atlas learning facts. Use for handwritten answer sheets, answer-card photos, translation or writing transcription, multi-model extraction, disputed blanks, or any model-derived answer evidence.
---

# Confirm Learning Evidence

Treat every provider output as a candidate, never as a learning fact. Use the `opentutor extraction` control plane; do not edit SQLite or post image-derived answers directly to the ordinary attempts endpoint.

The local control plane trusts its caller and does not cryptographically authenticate provider or teacher labels. Submit results produced by the named adapter/current Codex run; never manufacture a `provider=codex|doubao` payload. Create teacher decisions only from the teacher's explicit response to the displayed review, never from an actor label, silence, or model agreement.

Classify risk before requesting providers, and choose the stricter level when evidence is borderline:

- `R0`: a clear constrained mark such as a legible multiple-choice bubble or answer-grid cell.
- `R1`: a clear low-ambiguity short response. The current cold-start contract still requires Codex and Doubao; do not switch to single-model until a future learner-and-question-type calibration registry proves the agreed error threshold.
- `R2`: handwritten or ambiguous short free text, including a handwritten cloze or spelling answer; successful independent Codex and Doubao results are mandatory.
- `R3`: translation, writing, or other long free text; successful independent Codex and Doubao results are mandatory.
- `R4`: unreadable, cut-off, or misaligned evidence; do not infer text, and use the human exclusion path when recapture is unavailable.

Never downgrade risk because a second provider is missing or unconfigured.

## Run the confirmation gate

1. Resolve the explicit `STU-*` learner and active session, hash each private source image, and create one extraction batch containing every expected answer region.
2. Submit immutable provider results. R0 may use one deterministic, Codex, or Doubao result. The current cold-start R1 contract and all R2/R3 items require successful independent results from both Codex and Doubao; their first-round prompts must use the same source crop and must not include the standard answer or the other provider's output. Repeated calls, aliases, or deterministic output never substitute for either named provider.
3. Read the complete compact review. Show every item, including clear multiple-choice answers, captured blanks, provider failures, uncertain spans, and long-text differences.
4. Convert the teacher's response into structured decisions for the current `review_version`. Silence is not acceptance. Use batch `accept_prefill` only when the teacher explicitly accepts the displayed ordinary-item set.
5. Commit only when every item has a terminal human decision and all risk gates pass. Re-read the batch and formal counts after commit.

`pending_review` and `needs_check` block the whole commit. `not_captured` and `rejected_alignment` remain auditable but create no attempt, error cause, mastery change, or retest. Never overwrite provider output with a human correction.

If Doubao is unconfigured or fails, preserve that status. Do not represent the batch as dual-model confirmed; keep affected R1/R2/R3 items blocked until a real second result or an explicit safer recapture workflow is available.

Read [the extraction contract](references/extraction-contract.md) when constructing payloads or translating a teacher response into decisions.
