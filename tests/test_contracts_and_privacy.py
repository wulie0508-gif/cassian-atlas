from __future__ import annotations

import json
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
            "C:\\Users\\" + "huawei",
            "D:\\" + "找回的文件",
            "hunan" + "_learning",
        )
        allowed_suffixes = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sql", ".txt"}
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
        self.assertEqual(violations, [])

    def test_json_contracts_are_valid_json(self):
        root = Path(__file__).resolve().parents[1]
        for path in list((root / "schemas").glob("*.json")) + list((root / "examples").glob("*.json")):
            json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
