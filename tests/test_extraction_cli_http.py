from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from english_tracker import cli
from english_tracker.db import connect, database_path, initialize_database
from english_tracker.webapp import LearningHubHandler, LearningHubServer


class ExtractionCliContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_path = self.root / "payload.json"
        self.input_path.write_text(
            json.dumps({"idempotency_key": "extraction:test:v1"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _args(self, *, batch: bool = False) -> Namespace:
        values = {
            "student": "stu-001",
            "input": str(self.input_path),
            "output": None,
        }
        if batch:
            values["batch"] = "EXT-BATCH-001"
        return Namespace(**values)

    def test_parser_exposes_the_six_extraction_commands(self) -> None:
        parser = cli.build_parser()
        commands = {
            "create": ["--student", "STU-001", "--input", "batch.json"],
            "provider-submit": [
                "--student",
                "STU-001",
                "--batch",
                "EXT-1",
                "--input",
                "provider.json",
            ],
            "review": ["--student", "STU-001", "--batch", "EXT-1"],
            "decide": [
                "--student",
                "STU-001",
                "--batch",
                "EXT-1",
                "--input",
                "decisions.json",
            ],
            "commit": [
                "--student",
                "STU-001",
                "--batch",
                "EXT-1",
                "--input",
                "commit.json",
            ],
            "show": ["--student", "STU-001", "--batch", "EXT-1"],
        }
        expected = {
            "create": cli.cmd_extraction_create,
            "provider-submit": cli.cmd_extraction_provider_submit,
            "review": cli.cmd_extraction_review,
            "decide": cli.cmd_extraction_decide,
            "commit": cli.cmd_extraction_commit,
            "show": cli.cmd_extraction_show,
        }
        for command, arguments in commands.items():
            with self.subTest(command=command):
                parsed = parser.parse_args(["extraction", command, *arguments])
                self.assertIs(parsed.func, expected[command])

    def test_write_commands_inject_student_and_backup_before_dispatch(self) -> None:
        conn = MagicMock()
        cases = (
            ("create", cli.cmd_extraction_create, "create_extraction_batch", False),
            (
                "provider-submit",
                cli.cmd_extraction_provider_submit,
                "submit_provider_results",
                True,
            ),
            ("decide", cli.cmd_extraction_decide, "submit_human_decisions", True),
            ("commit", cli.cmd_extraction_commit, "commit_extraction_batch", True),
        )
        for label, command, core_name, needs_batch in cases:
            with self.subTest(command=label):
                conn.reset_mock()
                with (
                    patch.object(cli, "_open", return_value=(self.root, conn)),
                    patch.object(cli, "_backup", return_value="backup.sqlite") as backup,
                    patch.object(cli, "_emit") as emit,
                    patch.object(cli, core_name, return_value={"status": "applied"}) as core,
                ):
                    self.assertEqual(command(self._args(batch=needs_batch)), 0)
                backup.assert_called_once()
                if needs_batch:
                    self.assertEqual(core.call_args.args[1], "EXT-BATCH-001")
                    payload = core.call_args.args[2]
                    self.assertEqual(core.call_args.kwargs["student_id"], "STU-001")
                else:
                    payload = core.call_args.args[1]
                self.assertEqual(payload["student_id"], "STU-001")
                if label == "commit":
                    self.assertEqual(core.call_args.kwargs["backup_path"], "backup.sqlite")
                self.assertEqual(emit.call_args.args[0]["backup"], "backup.sqlite")
                conn.close.assert_called_once()


class ExtractionHttpContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "private"
        self.library_root = self.root / "library"
        self.library_root.mkdir()
        self.question_bank = self.root / "question-bank.sqlite"
        sqlite3.connect(self.question_bank).close()
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner")
        self.server = LearningHubServer(
            ("127.0.0.1", 0),
            LearningHubHandler,
            data_dir=self.data_dir,
            question_bank=self.question_bank,
            library_root=self.library_root,
            student_id="STU-001",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.env.stop()
        self.temp.cleanup()

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_extraction_gets_require_explicit_student_and_use_readonly_connections(self) -> None:
        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base_url + "/api/extraction/batches/EXT-1/review", timeout=5)
        self.assertEqual(missing.exception.code, 400)
        self.assertIn("student_id is required", missing.exception.read().decode("utf-8"))

        with (
            patch("english_tracker.webapp.connect", wraps=connect) as tracked,
            patch(
                "english_tracker.webapp.extraction_review",
                return_value={"batch_id": "EXT-1", "items": []},
            ) as review,
        ):
            with urlopen(
                self.base_url
                + "/api/extraction/batches/EXT-1/review?student_id=STU-001",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["batch_id"], "EXT-1")
        self.assertEqual(review.call_args.kwargs["student_id"], "STU-001")
        self.assertTrue(tracked.call_args_list)
        self.assertTrue(
            all(call.kwargs.get("readonly") is True for call in tracked.call_args_list)
        )

        with patch(
            "english_tracker.webapp.extraction_batch_detail",
            return_value={"batch_id": "EXT-1", "status": "pending_review"},
        ) as detail:
            with urlopen(
                self.base_url
                + "/api/extraction/batches/EXT-1?student_id=STU-001",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["status"], "pending_review")
        self.assertEqual(detail.call_args.kwargs["student_id"], "STU-001")

    def test_extraction_posts_dispatch_with_explicit_student_and_backup(self) -> None:
        cases = (
            (
                "/api/extraction/batches",
                "create_extraction_batch",
                "created",
                "web-extraction-create",
            ),
            (
                "/api/extraction/batches/EXT-1/provider-results",
                "submit_provider_results",
                "applied",
                "web-extraction-provider-submit",
            ),
            (
                "/api/extraction/batches/EXT-1/decisions",
                "submit_human_decisions",
                "applied",
                "web-extraction-decide",
            ),
            (
                "/api/extraction/batches/EXT-1/commit",
                "commit_extraction_batch",
                "committed",
                "web-extraction-commit",
            ),
        )
        for path, core_name, result_status, reason in cases:
            with self.subTest(path=path):
                with (
                    patch(
                        f"english_tracker.webapp.{core_name}",
                        return_value={"status": result_status},
                    ) as core,
                    patch(
                        "english_tracker.webapp.create_backup",
                        return_value=Path("backup.sqlite"),
                    ) as backup,
                ):
                    status, payload = self._post(
                        path,
                        {
                            "student_id": "STU-001",
                            "idempotency_key": f"http:{core_name}:v1",
                        },
                    )
                self.assertEqual(status, 201)
                self.assertEqual(payload["backup"], "backup.sqlite")
                self.assertEqual(backup.call_args.args[2], reason)
                if core_name == "create_extraction_batch":
                    submitted = core.call_args.args[1]
                else:
                    self.assertEqual(core.call_args.args[1], "EXT-1")
                    submitted = core.call_args.args[2]
                    self.assertEqual(core.call_args.kwargs["student_id"], "STU-001")
                self.assertEqual(submitted["student_id"], "STU-001")
                if core_name == "commit_extraction_batch":
                    self.assertEqual(
                        core.call_args.kwargs["backup_path"],
                        "backup.sqlite",
                    )

    def test_http_end_to_end_commits_only_after_full_human_confirmation(self) -> None:
        _, session = self._post(
            "/api/sessions",
            {
                "student_id": "STU-001",
                "event_id": "EVT-HTTP-XTR-SESSION",
                "idempotency_key": "http:xtr:session:v1",
                "source_thread": "courseware",
                "session": {
                    "session_id": "SES-HTTP-XTR",
                    "session_type": "homework",
                    "title": "Anonymous HTTP extraction fixture",
                    "started_at": "2026-09-02T10:00:00+08:00",
                },
            },
        )
        self.assertEqual(session["result"]["status"], "applied")

        batch_id = "XBAT-HTTP-E2E"
        item_id = "XITEM-HTTP-E2E"
        status, created = self._post(
            "/api/extraction/batches",
            {
                "student_id": "STU-001",
                "idempotency_key": "http:xtr:batch:v1",
                "extraction_batch_id": batch_id,
                "session_id": "SES-HTTP-XTR",
                "title": "One clear answer",
                "source_images": [
                    {
                        "source_id": "PAGE-1",
                        "private_path": "private://anonymous/page-1.png",
                        "sha256": "a" * 64,
                        "media_type": "image/png",
                        "byte_size": 100,
                        "page_number": 1,
                    }
                ],
                "items": [
                    {
                        "extraction_item_id": item_id,
                        "source_id": "PAGE-1",
                        "ordinal": 1,
                        "question_ref": "Q-1",
                        "question_type": "multiple_choice",
                        "risk_level": "R0",
                        "evidence_locator": {"page": 1, "region": [0, 0, 10, 10]},
                        "attempt_template": {
                            "attempted_at": "2026-09-02T10:05:00+08:00",
                            "standard_answer": "A",
                            "response_mode": "recognition",
                            "validation_status": "verified",
                            "grading_contract": {
                                "mode": "deterministic_exact",
                                "acceptable_answers": ["A"],
                                "max_score": 1,
                            },
                            "item": {
                                "item_id": "ITEM-HTTP-E2E",
                                "domain": "grammar",
                                "item_type": "multiple_choice",
                                "prompt_snapshot": "Anonymous prompt",
                                "answer_snapshot": "A",
                            },
                        },
                    }
                ],
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["result"]["status"], "created")

        status, provider = self._post(
            f"/api/extraction/batches/{batch_id}/provider-results",
            {
                "student_id": "STU-001",
                "idempotency_key": "http:xtr:provider:v1",
                "provider": "codex",
                "model_version": "test-model",
                "prompt_version": "transcription-v1",
                "request_sha256": "b" * 64,
                "completed_at": "2026-09-02T10:06:00+08:00",
                "results": [
                    {
                        "extraction_item_id": item_id,
                        "result_status": "succeeded",
                        "raw_transcription": "A",
                        "normalized_transcription": "A",
                        "capture_status": "captured",
                        "uncertain_spans": [],
                        "candidate_alternatives": [],
                        "confidence": 0.99,
                        "evidence_locator": {"page": 1, "region": [0, 0, 10, 10]},
                    }
                ],
            },
        )
        self.assertEqual(status, 201)
        review = provider["result"]["review"]
        self.assertEqual(review["counts"]["total"], 1)
        self.assertTrue(review["standard_answers_hidden"])

        conn = connect(database_path(self.data_dir), readonly=True)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 0)
        finally:
            conn.close()

        status, decided = self._post(
            f"/api/extraction/batches/{batch_id}/decisions",
            {
                "student_id": "STU-001",
                "idempotency_key": "http:xtr:decisions:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher",
                "default_action": "accept_prefill",
            },
        )
        self.assertEqual(status, 201)
        confirmed = decided["result"]["review"]
        self.assertTrue(confirmed["can_commit"])

        commit_payload = {
            "student_id": "STU-001",
            "idempotency_key": "http:xtr:commit:v1",
            "expected_review_version": confirmed["review_version"],
            "actor": "teacher",
        }
        status, committed = self._post(
            f"/api/extraction/batches/{batch_id}/commit",
            commit_payload,
        )
        self.assertEqual(status, 201)
        self.assertEqual(committed["result"]["status"], "applied")
        self.assertEqual(committed["result"]["attempts_inserted"], 1)
        self.assertEqual(committed["result"]["readback"]["count"], 1)

        duplicate_status, duplicate = self._post(
            f"/api/extraction/batches/{batch_id}/commit",
            commit_payload,
        )
        self.assertEqual(duplicate_status, 200)
        self.assertEqual(duplicate["result"]["status"], "duplicate")
        self.assertEqual(duplicate["result"]["readback"]["count"], 1)

        with urlopen(
            self.base_url
            + f"/api/extraction/batches/{batch_id}?student_id=STU-001",
            timeout=5,
        ) as response:
            reread = json.loads(response.read().decode("utf-8"))
        self.assertEqual(reread["status"], "committed")
        self.assertEqual(len(reread["commit_links"]), 1)


if __name__ == "__main__":
    unittest.main()
