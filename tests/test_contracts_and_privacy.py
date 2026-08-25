from __future__ import annotations

import hashlib
import json
import re
import tomllib
import unittest
from pathlib import Path

from english_tracker.contracts import ContractError, validate_attempts_payload
from english_tracker.util import payload_hash


class ContractUnitTest(unittest.TestCase):
    def test_canonical_payload_hash_ignores_key_order(self):
        self.assertEqual(payload_hash({"a": 1, "b": 2}), payload_hash({"b": 2, "a": 1}))

    def test_attempt_capture_status_is_required(self):
        payload = {
            "event_id": "EVT-1",
            "idempotency_key": "KEY-1",
            "source_thread": "manual",
            "student_id": "STU-001",
            "session_id": "SES-1",
            "attempts": [{"event_id": "ATT-1", "attempted_at": "2026-01-01", "item_id": "ITEM-1", "evaluation": {"result": "wrong"}}],
        }
        with self.assertRaises(ContractError):
            validate_attempts_payload(payload)

    def test_duplicate_attempt_event_in_one_payload_is_rejected(self):
        attempt = {
            "event_id": "ATT-1",
            "attempted_at": "2026-01-01",
            "item_id": "ITEM-1",
            "answer_capture_status": "not_captured",
            "evaluation": {"result": "wrong"},
        }
        payload = {
            "event_id": "EVT-1",
            "idempotency_key": "KEY-1",
            "source_thread": "manual",
            "student_id": "STU-001",
            "session_id": "SES-1",
            "attempts": [attempt, dict(attempt)],
        }
        with self.assertRaises(ContractError):
            validate_attempts_payload(payload)

    def test_not_captured_rejects_inferred_error_type(self):
        payload = {
            "event_id": "EVT-1",
            "idempotency_key": "KEY-1",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "session_id": "SES-1",
            "attempts": [
                {
                    "event_id": "ATT-1",
                    "attempted_at": "2026-01-01",
                    "item_id": "ITEM-1",
                    "student_answer": None,
                    "answer_capture_status": "not_captured",
                    "evaluation": {"result": "wrong"},
                    "error_types": ["clause connector error"],
                }
            ],
        }
        with self.assertRaises(ContractError):
            validate_attempts_payload(payload)


class RepositoryPrivacyTest(unittest.TestCase):
    def test_public_text_files_do_not_contain_private_identifiers(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = (
            "胡" + "楠",
            "C:\\Users\\" + "sample-user",
            "D:\\" + "".join(chr(code) for code in (0x627E, 0x56DE, 0x7684, 0x6587, 0x4EF6)),
            "hu" + "nan" + "_learning",
        )
        private_romanization = re.compile("hu" + r"[\s_-]*" + "nan", re.IGNORECASE)
        allowed_suffixes = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sql", ".txt", ".js", ".css", ".html", ".svg", ".ps1", ".cmd", ".sh"}
        violations = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            if any(part in {".git", "__pycache__"} or part.endswith(".egg-info") for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8-sig")
            for token in forbidden:
                if token in text:
                    violations.append(f"{path.relative_to(root)}: {token}")
            if private_romanization.search(text):
                violations.append(f"{path.relative_to(root)}: private learner romanization")
        self.assertEqual(violations, [])

    def test_repository_contains_no_private_content_artifacts(self):
        root = Path(__file__).resolve().parents[1]
        blocked = {".sqlite", ".sqlite3", ".db", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".wav", ".mp3", ".zip", ".7z", ".rar"}
        public_binary_assets = {
            Path("site/assets/social-card.png"): "97a6132b47be9c63836afa6b2d2eb1e9148fc28e9a38807b560a9d78b4a64c7e",
        }
        violations = []
        for path in root.rglob("*"):
            if not path.is_file() or any(part in {".git", "__pycache__"} or part.endswith(".egg-info") for part in path.parts):
                continue
            if path.suffix.lower() in blocked:
                relative = path.relative_to(root)
                expected_hash = public_binary_assets.get(relative)
                if expected_hash is None:
                    violations.append(str(relative))
                elif hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    violations.append(f"{relative}: hash mismatch")
        self.assertEqual(violations, [])

    def test_json_contracts_are_valid_json(self):
        root = Path(__file__).resolve().parents[1]
        for path in list((root / "schemas").glob("*.json")) + list((root / "examples").glob("*.json")):
            json.loads(path.read_text(encoding="utf-8"))

    def test_agent_and_generation_contracts_keep_explicit_student_ownership(self):
        root = Path(__file__).resolve().parents[1]
        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in (root / "schemas").glob("*.json")
        }

        event = schemas["agent-run-event.schema.json"]
        self.assertEqual(set(event["required"]), {"student_id", "event_type"})
        self.assertIn("idempotency_key", event["properties"])
        self.assertFalse(event["additionalProperties"])

        generation_start = schemas["artifact-generation-start.schema.json"]
        self.assertEqual(
            set(generation_start["required"]),
            {"student_id", "title", "source_snapshot"},
        )
        self.assertEqual(generation_start["properties"]["subject_code"]["default"], "english")
        self.assertEqual(generation_start["properties"]["artifact_type"]["default"], "courseware")

        generation_update = schemas["artifact-generation-update.schema.json"]
        self.assertEqual(set(generation_update["required"]), {"student_id", "status"})
        self.assertFalse(generation_update["additionalProperties"])

    def test_packaging_and_runtime_versions_match(self):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        namespace: dict[str, object] = {}
        exec((root / "src" / "english_tracker" / "__init__.py").read_text(encoding="utf-8"), namespace)
        self.assertEqual(project["project"]["version"], namespace["__version__"])


if __name__ == "__main__":
    unittest.main()
