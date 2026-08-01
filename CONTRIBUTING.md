# Contributing to OpenTutor Ledger

Thank you for improving private, evidence-based learning infrastructure.

## Ground rules

1. Never commit real learner data or copyrighted question-bank content.
2. Keep learning facts auditable. Model output stays `suggested` until a human verifies it.
3. Preserve complete-passage boundaries and raw-answer capture semantics.
4. Add a migration instead of editing one that may already be applied.
5. Keep the core runtime dependency-free when practical.

## Local setup

```bash
python -m venv .venv
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/release_privacy_audit.py
```

## Pull requests

- Keep one coherent change per pull request.
- Explain the evidence boundary affected by the change.
- Add tests for schema, isolation, idempotency, or UI state changes.
- Update both English and Chinese product copy when visible behavior changes.
- Run the privacy release gate before pushing.

External datasets, textbooks, exam papers, answer keys, recordings, OCR output, and learner databases are not accepted in issues or pull requests.
