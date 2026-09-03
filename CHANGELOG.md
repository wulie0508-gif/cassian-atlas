# Changelog

All notable changes to Cassian Atlas are documented in this file.

## [0.5.0] - 2026-09-03

### Added

- A complete Codex-first application bundle with root project policy, a thin
  router, twelve independently installable specialist skills, and an audited
  CLI/local HTTP control plane.
- Teacher-confirmed extraction batches: immutable provider candidates,
  complete-batch review, revisioned decisions, and atomic evidence commits.
- Optional privacy-minimized multimodal provider integration. Credentials and
  provider outputs remain outside the repository.
- Verified whole-passage selection manifests with duplicate, learner-history,
  source-quality, and knowledge-coverage exclusions.
- Learner-free public explanation caching with deterministic content identity.
- A local Feishu Base projection outbox, delivery ledger, and hash-readback
  contract. Live cloud transport is not included.
- A public Codex application guide and a commercial private-corpus / RAG
  integration contact path.

### Changed

- Rebranded the public product as Cassian Atlas, an Evidence OS for
  agent-native tutoring, while preserving stable `opentutor`, `.opentutor`, and
  `OPEN_TUTOR_*` runtime identifiers for compatibility.
- Schema upgrades are explicit and checksum-verified rather than silently
  applied during ordinary commands.
- Existing scored attempts can be replaced only through a teacher-verified,
  auditable command that preserves the assessment baseline.
- Public product copy now distinguishes open-source workflow code, private
  learner evidence, optional model adapters, and separately licensed content.

### Security and privacy

- Expanded ignore rules for credentials, `.env` files, local Cassian Atlas state,
  source documents, archives, and media.
- Removed a private corpus directory from the public router fallback.
- Preserved the hard rule that unconfirmed OCR/model output cannot affect
  scores, mastery, error evidence, or review scheduling.

[0.5.0]: https://github.com/wulie0508-gif/cassian-atlas/releases/tag/v0.5.0
