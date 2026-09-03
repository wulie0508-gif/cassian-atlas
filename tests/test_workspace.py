from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from english_tracker.cli import main
from english_tracker.db import connect, database_path, initialize_database
from english_tracker.ingest import IngestConflict, import_attempts, import_session
from english_tracker.workspace import (
    app_config,
    create_student,
    deactivate_student,
    enroll_student,
    require_student_enrollment,
    student_detail,
    student_summaries,
    subject_overview,
    update_student,
)


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "data_dir": str(self.data_dir),
                    "db_name": "learning.sqlite",
                    "question_bank": str(Path(self.temp.name) / "questions.sqlite"),
                    "library_root": str(Path(self.temp.name) / "library"),
                }
            ),
            encoding="utf-8",
        )
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner One")
        self.conn = connect(database_path(self.data_dir))

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def run_cli(self, *args: str) -> dict:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--config", str(self.config_path), *args])
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        return json.loads(stdout.getvalue())

    def test_student_creation_and_subject_registry(self):
        config = app_config(self.conn)
        self.assertEqual(config["product"]["name"], "Cassian Atlas")
        self.assertEqual([row["code"] for row in config["locales"]], ["zh-CN", "en"])
        self.assertIn("geography", {row["subject_code"] for row in config["subjects"]})
        legacy_default = student_detail(self.conn, "STU-001")
        self.assertEqual(legacy_default["display_name"], "Learner One")
        self.assertTrue(all(value is None for value in legacy_default["profile"].values()))
        self.assertEqual(require_student_enrollment(self.conn, "STU-001", "english"), ("STU-001", "english"))

        created = create_student(
            self.conn,
            {
                "student_id": "STU-002",
                "display_name": "Learner Two",
                "timezone": "Asia/Shanghai",
                "subject_codes": ["english", "geography"],
                "profile": {
                    "grade_level": "高三",
                    "exam_system": "上海春考外语一考",
                    "target_exam_date": "2027-01-08",
                    "target_score": 115,
                    "weekly_hours": 18.5,
                    "course_stage": "语法填空强化",
                    "teacher_notes": "Use verified evidence only.",
                },
            },
        )
        self.assertEqual(created["student_id"], "STU-002")
        self.assertEqual(created["profile"]["grade_level"], "高三")
        self.assertEqual(created["profile"]["target_score"], 115)
        students = student_summaries(self.conn)
        self.assertEqual(students["count"], 2)
        second = next(row for row in students["items"] if row["student_id"] == "STU-002")
        self.assertEqual({row["subject_code"] for row in second["subjects"]}, {"english", "geography"})

    def test_subject_evidence_is_isolated_by_student(self):
        create_student(
            self.conn,
            {"student_id": "STU-002", "display_name": "Learner Two", "subject_codes": ["english"]},
        )
        with self.assertRaisesRegex(ValueError, "not enrolled"):
            subject_overview(self.conn, "STU-002", "geography")
        import_session(
            self.conn,
            {
                "event_id": "EVT-GEO-SESSION",
                "idempotency_key": "geo-session-v1",
                "source_thread": "courseware",
                "student_id": "STU-002",
                "session": {
                    "session_id": "SES-GEO-001",
                    "session_type": "lesson",
                    "title": "Geography lesson",
                    "started_at": "2026-08-01T09:00:00+08:00",
                },
            },
        )
        import_attempts(
            self.conn,
            {
                "event_id": "EVT-GEO-ATTEMPTS",
                "idempotency_key": "geo-attempts-v1",
                "source_thread": "courseware",
                "student_id": "STU-002",
                "session_id": "SES-GEO-001",
                "attempts": [
                    {
                        "event_id": "ATT-GEO-001",
                        "attempted_at": "2026-08-01T09:05:00+08:00",
                        "student_answer": "A",
                        "standard_answer": "A",
                        "answer_capture_status": "captured",
                        "evaluation": {"result": "correct", "score": 1, "max_score": 1},
                        "item": {
                            "subject_code": "geography",
                            "domain": "knowledge",
                            "item_type": "multiple_choice",
                            "prompt_snapshot": "Anonymous geography prompt",
                            "answer_snapshot": "A",
                        },
                    }
                ],
            },
        )
        second = subject_overview(self.conn, "STU-002", "geography")
        self.assertEqual(second["summary"]["attempt_count"], 1)
        self.assertEqual(second["summary"]["accuracy"], 1.0)
        with self.assertRaisesRegex(ValueError, "not enrolled"):
            subject_overview(self.conn, "STU-001", "geography")
        enrolled = next(row for row in student_summaries(self.conn)["items"] if row["student_id"] == "STU-002")
        self.assertEqual({row["subject_code"] for row in enrolled["subjects"]}, {"english", "geography"})

        with self.assertRaises(IngestConflict):
            import_attempts(
                self.conn,
                {
                    "event_id": "EVT-BAD-SUBJECT",
                    "idempotency_key": "bad-subject-v1",
                    "source_thread": "courseware",
                    "student_id": "STU-002",
                    "session_id": "SES-GEO-001",
                    "attempts": [
                        {
                            "event_id": "ATT-BAD-SUBJECT",
                            "attempted_at": "2026-08-01T09:06:00+08:00",
                            "answer_capture_status": "not_captured",
                            "evaluation": {"result": "wrong"},
                            "item": {"subject_code": "unknown", "domain": "knowledge", "item_type": "other"},
                        }
                    ],
                },
            )

    def test_student_lifecycle_and_enrollment_validation(self):
        create_student(
            self.conn,
            {
                "student_id": "STU-003",
                "display_name": "Learner Three",
                "subject_codes": ["english"],
            },
        )
        updated = update_student(
            self.conn,
            "STU-003",
            {
                "display_name": "Learner 3",
                "profile": {
                    "grade_level": "Grade 11",
                    "target_exam_date": "2027-06-01",
                    "target_score": 120,
                    "weekly_hours": 12,
                    "course_stage": "foundation",
                },
            },
        )
        self.assertEqual(updated["display_name"], "Learner 3")
        self.assertEqual(updated["profile"]["weekly_hours"], 12)
        with self.assertRaisesRegex(ValueError, "not enrolled"):
            require_student_enrollment(self.conn, "STU-003", "geography")
        enrolled = enroll_student(self.conn, "STU-003", ["geography"])
        self.assertEqual(enrolled["enrolled_subject_codes"], ["geography"])
        self.assertEqual(require_student_enrollment(self.conn, "STU-003", "geography"), ("STU-003", "geography"))

        deactivated = deactivate_student(self.conn, "STU-003")
        self.assertFalse(deactivated["active"])
        self.assertNotIn("STU-003", {row["student_id"] for row in student_summaries(self.conn)["items"]})
        all_students = student_summaries(self.conn, include_inactive=True)
        self.assertIn("STU-003", {row["student_id"] for row in all_students["items"]})
        with self.assertRaisesRegex(ValueError, "Unknown or inactive"):
            require_student_enrollment(self.conn, "STU-003", "english")
        self.assertFalse(student_detail(self.conn, "STU-003")["active"])

    def test_student_cli_commands_emit_json_and_require_explicit_ids(self):
        listed = self.run_cli("student", "list")
        self.assertEqual(listed["count"], 1)

        added = self.run_cli(
            "student",
            "add",
            "--student",
            "STU-CLI-001",
            "--display-name",
            "CLI Learner",
            "--grade-level",
            "高二",
            "--exam-system",
            "上海高考",
            "--target-exam-date",
            "2028-01-07",
            "--target-score",
            "110",
            "--weekly-hours",
            "10",
            "--course-stage",
            "foundation",
            "--teacher-notes",
            "CLI-managed profile",
        )
        self.assertEqual(added["student_id"], "STU-CLI-001")
        self.assertEqual(added["profile"]["exam_system"], "上海高考")

        shown = self.run_cli("student", "show", "--student", "STU-CLI-001")
        self.assertEqual(shown["display_name"], "CLI Learner")
        updated = self.run_cli(
            "student",
            "update",
            "--student",
            "STU-CLI-001",
            "--course-stage",
            "reading",
            "--weekly-hours",
            "12.5",
        )
        self.assertEqual(updated["profile"]["course_stage"], "reading")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(
                [
                    "--data-dir",
                    str(self.data_dir),
                    "agent",
                    "route",
                    "--student",
                    "STU-CLI-001",
                    "--subject",
                    "geography",
                    "--request",
                    "准备地理课件",
                ]
            )
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("not enrolled", json.loads(stderr.getvalue())["error"])

        enrolled = self.run_cli(
            "student",
            "enroll",
            "--student",
            "STU-CLI-001",
            "--subject",
            "geography",
        )
        self.assertIn("geography", {row["subject_code"] for row in enrolled["subjects"]})
        routed = self.run_cli(
            "agent",
            "route",
            "--student",
            "STU-CLI-001",
            "--subject",
            "geography",
            "--request",
            "准备地理课件",
        )
        self.assertEqual(routed["student_id"], "STU-CLI-001")
        self.assertEqual(routed["subject_code"], "geography")
        deactivated = self.run_cli("student", "deactivate", "--student", "STU-CLI-001")
        self.assertEqual(deactivated["status"], "deactivated")
        self.assertFalse(deactivated["active"])
        with_inactive = self.run_cli("student", "list", "--include-inactive")
        self.assertIn("STU-CLI-001", {row["student_id"] for row in with_inactive["items"]})

    def test_identical_prompts_remain_distinct_across_subjects(self):
        import_session(
            self.conn,
            {
                "event_id": "EVT-MIXED-SESSION",
                "idempotency_key": "mixed-session-v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session": {
                    "session_id": "SES-MIXED-001",
                    "session_type": "lesson",
                    "title": "Mixed subject evidence",
                    "started_at": "2026-08-01T10:00:00+08:00",
                },
            },
        )
        import_attempts(
            self.conn,
            {
                "event_id": "EVT-MIXED-ATTEMPTS",
                "idempotency_key": "mixed-attempts-v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session_id": "SES-MIXED-001",
                "attempts": [
                    {
                        "event_id": "ATT-MIXED-EN",
                        "attempted_at": "2026-08-01T10:01:00+08:00",
                        "answer_capture_status": "not_captured",
                        "evaluation": {"result": "correct"},
                        "item": {"subject_code": "english", "domain": "knowledge", "item_type": "other", "prompt_snapshot": "Shared prompt"},
                    },
                    {
                        "event_id": "ATT-MIXED-GEO",
                        "attempted_at": "2026-08-01T10:02:00+08:00",
                        "answer_capture_status": "not_captured",
                        "evaluation": {"result": "correct"},
                        "item": {"subject_code": "geography", "domain": "knowledge", "item_type": "other", "prompt_snapshot": "Shared prompt"},
                    },
                ],
            },
        )
        rows = self.conn.execute(
            "SELECT subject_code,COUNT(*) count FROM content_items WHERE prompt_snapshot='Shared prompt' GROUP BY subject_code"
        ).fetchall()
        self.assertEqual({(row["subject_code"], row["count"]) for row in rows}, {("english", 1), ("geography", 1)})


if __name__ == "__main__":
    unittest.main()
