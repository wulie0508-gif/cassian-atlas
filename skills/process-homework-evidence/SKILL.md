---
name: process-homework-evidence
description: Process photographed or scanned student answers with a privacy-minimized Doubao multimodal candidate, independent Codex checking when required, complete teacher confirmation, grading, and Cassian Atlas evidence recording. Use whenever this tutoring project receives handwritten homework, answer sheets, OCR text, translations, essays, or image-based grading requests.
---

# Process Homework Evidence

Use this skill as the single project entry point for answer images and model-derived OCR. Keep recognition, confirmation, grading, diagnosis, and publishing as separate stages.

## 1. Check the private provider configuration

Run:

```powershell
python scripts/doubao_extract.py status
```

If Doubao is not configured, tell the teacher to run `scripts/configure_doubao.ps1` in a local terminal. Never request, display, store in chat, or commit an API key. The private settings file belongs at `%USERPROFILE%\.opentutor\doubao.env`.

## 2. Choose the evidence path

- If the teacher supplied clean, typed, item-aligned answers, skip OCR and use `$record-learning-evidence`.
- If the source is a photo, scan, screenshot, OCR transcript, or model-produced extraction, continue here.
- If the learner, session, question alignment, or page is ambiguous, stop before grading and resolve the ambiguity.

## 3. Extract candidates without grading

Create a small manifest using [the batch manifest contract](references/batch-manifest.md), then run one page-level request:

```powershell
python scripts/doubao_extract.py extract --image '<page-image>' --manifest '<manifest.json>' --output '<candidate.json>'
```

The provider receives only the page image, anonymous item labels, item type, risk level, and safe region locator. Never send the learner name, answer key, private local path, grades, diagnoses, or another provider's output.

Use cached results when the image hash, model, prompt version, and manifest match. Use `--force` only when the source or extraction prompt has materially changed.

## 4. Apply the Cassian Atlas confirmation gate

Invoke `$confirm-learning-evidence` and follow its current risk contract exactly:

- R0 clear constrained marks may use one reliable extraction candidate.
- During cold start, R1 and all R2/R3 items require independent Codex and Doubao candidates from the same evidence without sharing the answer key or each other's output.
- R4 unreadable, cut-off, or misaligned evidence must not be guessed.
- Show every extracted item in a compact human-review table. Ordinary rows may be accepted as one explicitly named batch; anomalies and long text must be expanded.
- Silence or provider agreement is never human confirmation.

Do not bypass `opentutor extraction create`, `provider-submit`, `review`, `decide`, and `commit`. Never write directly to SQLite.

## 5. Grade only confirmed text

- Objective items: grade deterministically against the aligned answer and scoring rule after confirmation.
- Translation, Summary, and writing: use the stated rubric only after the exact transcript is confirmed; preserve both the original response and the evaluation.
- Diagnosis is a suggested interpretation until teacher-reviewed. Record observable error evidence before assigning a weakness label.

## 6. Publish the result

After commit, re-read the extraction batch and formal attempt counts. Local Cassian Atlas remains the source of truth. Feishu receives only the allowed operational or parent-facing projection under the repository's explicit Cassian profile rules; never upload raw answer images, answer keys, item-level answers, or private evidence.

Read [provider configuration](references/provider-config.md) before changing endpoint or model settings.
