from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from english_tracker.cli import main
from english_tracker.db import connect, database_path
from english_tracker.ingest import import_attempts, import_session
from english_tracker.runtime import CONFIG_ENV, ENV_BY_KEY


class CliWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.data_dir = self.root / "private-data"
        self.saved_environment = {
            key: os.environ.get(key) for key in (CONFIG_ENV, *ENV_BY_KEY.values())
        }
        self.config.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "data_dir": str(self.data_dir),
                    "db_name": "test.sqlite",
                    "question_bank": str(self.root / "questions.sqlite"),
                    "library_root": str(self.root / "library"),
                    "project_root": str(self.root),
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(
            self.run_cli(
                "student",
                "add",
                "--student",
                "STU-001",
                "--display-name",
                "Learner One",
            )[0],
            0,
        )
        self.assertEqual(
            self.run_cli(
                "student",
                "add",
                "--student",
                "STU-002",
                "--display-name",
                "Learner Two",
            )[0],
            0,
        )

    def tearDown(self) -> None:
        for key, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--config", str(self.config), *arguments])
        result = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
        error = json.loads(stderr.getvalue()) if stderr.getvalue() else {}
        return status, result, error

    def write_payload(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def seed_items_and_wrong_reading_attempt(self) -> str:
        with closing(connect(database_path(self.data_dir))) as conn:
            import_session(
                conn,
                {
                    "event_id": "EVT-CLI-SEED-SESSION",
                    "idempotency_key": "cli-seed-session-v1",
                    "source_thread": "migration",
                    "student_id": "STU-001",
                    "session": {
                        "session_id": "SES-CLI-SEED",
                        "session_type": "migration",
                        "title": "Seed items",
                        "started_at": "2026-08-23T08:00:00+08:00",
                    },
                },
            )
            import_attempts(
                conn,
                {
                    "event_id": "EVT-CLI-SEED-ATTEMPTS",
                    "idempotency_key": "cli-seed-attempts-v1",
                    "source_thread": "migration",
                    "student_id": "STU-001",
                    "session_id": "SES-CLI-SEED",
                    "attempts": [
                        {
                            "event_id": "ATT-CLI-SEED-WORD",
                            "attempted_at": "2026-08-23T08:01:00+08:00",
                            "student_answer": "deliver",
                            "standard_answer": "deliver",
                            "answer_capture_status": "captured",
                            "evaluation": {"result": "correct", "score": 1, "max_score": 1},
                            "item": {
                                "item_id": "WORD-DELIVER",
                                "subject_code": "english",
                                "domain": "vocabulary",
                                "item_type": "word",
                                "prompt_snapshot": "递送",
                                "answer_snapshot": "deliver",
                            },
                        },
                        {
                            "event_id": "ATT-CLI-SEED-READING",
                            "attempted_at": "2026-08-23T08:02:00+08:00",
                            "student_answer": "B",
                            "standard_answer": "A",
                            "answer_capture_status": "captured",
                            "evaluation": {"result": "wrong", "score": 0, "max_score": 1},
                            "item": {
                                "subject_code": "english",
                                "domain": "reading",
                                "item_type": "multiple_choice",
                                "prompt_snapshot": "What can be inferred?",
                                "answer_snapshot": "A",
                            },
                        },
                    ],
                },
            )
            return conn.execute(
                "SELECT attempt_id FROM attempts WHERE event_id='ATT-CLI-SEED-READING'"
            ).fetchone()[0]

    def test_assessment_record_is_explicit_backed_up_and_idempotent(self) -> None:
        path = self.write_payload(
            "assessment.json",
            {
                "title": "Monthly check",
                "date": "2026-08-23",
                "assessment_kind": "topic_quiz",
                "raw_score": 72,
                "max_score": 100,
            },
        )
        first_status, first, _ = self.run_cli(
            "assessment", "record", "--student", "stu-001", "--input", str(path)
        )
        second_status, second, _ = self.run_cli(
            "assessment", "record", "--student", "STU-001", "--input", str(path)
        )
        self.assertEqual((first_status, first["status"]), (0, "applied"))
        self.assertEqual(first["student_id"], "STU-001")
        self.assertEqual((second_status, second["status"]), (0, "duplicate"))
        self.assertTrue(Path(first["backup"]).is_file())
        self.assertEqual(first["session_id"], second["session_id"])
        mismatch = self.write_payload("mismatch.json", {"student_id": "STU-002"})
        status, _, error = self.run_cli(
            "assessment", "record", "--student", "STU-001", "--input", str(mismatch)
        )
        self.assertEqual(status, 1)
        self.assertIn("conflicts", error["error"])

    def test_dictation_and_reading_diagnostics_use_deterministic_workflows(self) -> None:
        attempt_id = self.seed_items_and_wrong_reading_attempt()
        dictation = self.write_payload(
            "dictation.json",
            {
                "title": "Morning dictation",
                "date": "2026-08-23",
                "items": [{"item_id": "WORD-DELIVER", "student_answer": "deliver"}],
            },
        )
        status, first, _ = self.run_cli(
            "dictation", "record", "--student", "STU-001", "--input", str(dictation)
        )
        self.assertEqual(status, 0)
        self.assertEqual((first["correct"], first["total"]), (1, 1))
        _, replay, _ = self.run_cli(
            "dictation", "record", "--student", "STU-001", "--input", str(dictation)
        )
        self.assertEqual(replay["session_result"]["status"], "duplicate")
        self.assertEqual(replay["attempts_result"]["status"], "duplicate")

        diagnostics = self.write_payload(
            "diagnostics.json",
            {
                "diagnostics": [
                    {
                        "attempt_id": attempt_id,
                        "error_types": [
                            {
                                "code": "reading_inference_overreach",
                                "error_source": "model_suggested",
                                "verification_status": "suggested",
                                "confidence": 0.82,
                                "rationale": "The chosen option adds a conclusion not supported by the text.",
                            }
                        ],
                    }
                ]
            },
        )
        status, first, _ = self.run_cli(
            "reading",
            "diagnostics",
            "record",
            "--student",
            "STU-001",
            "--input",
            str(diagnostics),
        )
        self.assertEqual((status, first["status"]), (0, "applied"))
        _, replay, _ = self.run_cli(
            "reading",
            "diagnostics",
            "record",
            "--student",
            "STU-001",
            "--input",
            str(diagnostics),
        )
        self.assertEqual(replay["status"], "duplicate")

    def test_generation_and_agent_read_commands_are_student_scoped(self) -> None:
        start_payload = self.write_payload(
            "generation-start.json",
            {
                "subject_code": "english",
                "artifact_type": "courseware",
                "title": "Grammar review",
                "source_snapshot": {"evidence_as_of": "2026-08-23T19:00:00+08:00"},
                "idempotency_key": "cli:generation:test:v1",
            },
        )
        status, first, _ = self.run_cli(
            "generation", "start", "--student", "STU-001", "--input", str(start_payload)
        )
        self.assertEqual((status, first["status"]), (0, "created"))
        generation_id = first["generation"]["generation_id"]
        _, replay, _ = self.run_cli(
            "generation", "start", "--student", "STU-001", "--input", str(start_payload)
        )
        self.assertEqual(replay["status"], "duplicate")

        update_payload = self.write_payload("generation-update.json", {"status": "in_progress"})
        _, updated, _ = self.run_cli(
            "generation",
            "update",
            "--student",
            "STU-001",
            "--generation",
            generation_id,
            "--input",
            str(update_payload),
        )
        self.assertEqual(updated["status"], "updated")
        _, duplicate_update, _ = self.run_cli(
            "generation",
            "update",
            "--student",
            "STU-001",
            "--generation",
            generation_id,
            "--input",
            str(update_payload),
        )
        self.assertEqual(duplicate_update["status"], "duplicate")
        completed_payload = self.write_payload(
            "generation-complete.json",
            {
                "status": "completed",
                "output_path": str(self.root / "grammar-review.docx"),
                "output_sha256": "a" * 64,
                "summary": "Generated and verified.",
            },
        )
        _, completed, _ = self.run_cli(
            "generation",
            "update",
            "--student",
            "STU-001",
            "--generation",
            generation_id,
            "--input",
            str(completed_payload),
        )
        self.assertEqual(completed["generation"]["status"], "completed")
        self.assertEqual(completed["generation"]["output_sha256"], "a" * 64)
        self.assertEqual(
            self.run_cli(
                "generation",
                "show",
                "--student",
                "STU-001",
                "--generation",
                generation_id,
            )[1]["generation_id"],
            generation_id,
        )
        self.assertEqual(
            self.run_cli("generation", "list", "--student", "STU-001")[1]["count"],
            1,
        )
        status, _, error = self.run_cli(
            "generation",
            "show",
            "--student",
            "STU-002",
            "--generation",
            generation_id,
        )
        self.assertEqual(status, 1)
        self.assertIn("STU-002", error["error"])

        status, route, _ = self.run_cli(
            "agent",
            "route",
            "--student",
            "STU-001",
            "--request",
            "Prepare a grammar lesson",
            "--idempotency-key",
            "cli:agent:test:v1",
            "--register",
        )
        self.assertEqual(status, 0)
        run_id = route["run"]["run_id"]
        capability = route["run"]["primary_capability"]
        event_arguments = (
            "agent",
            "event",
            "--student",
            "STU-001",
            "--run",
            run_id,
            "--event-type",
            "started",
            "--idempotency-key",
            "cli:agent:event:test:v1",
            "--capability",
            capability,
            "--message",
            "Started through the CLI.",
        )
        self.assertEqual(self.run_cli(*event_arguments)[1]["status"], "updated")
        self.assertEqual(self.run_cli(*event_arguments)[1]["status"], "duplicate")
        shown = self.run_cli(
            "agent", "show", "--student", "STU-001", "--run", run_id
        )[1]
        self.assertEqual(shown["student_id"], "STU-001")
        dashboard = self.run_cli(
            "agent", "dashboard", "--student", "STU-001"
        )[1]
        self.assertEqual(dashboard["recent_runs"][0]["run_id"], run_id)
        status, _, error = self.run_cli(
            "agent", "show", "--student", "STU-002", "--run", run_id
        )
        self.assertEqual(status, 1)
        self.assertIn("STU-002", error["error"])


if __name__ == "__main__":
    unittest.main()
