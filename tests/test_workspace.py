from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.ingest import IngestConflict, import_attempts, import_session
from english_tracker.workspace import app_config, create_student, student_summaries, subject_overview


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner One")
        self.conn = connect(database_path(self.data_dir))

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def test_student_creation_and_subject_registry(self):
        config = app_config(self.conn)
        self.assertEqual(config["product"]["name"], "OpenTutor Ledger")
        self.assertEqual([row["code"] for row in config["locales"]], ["zh-CN", "en"])
        self.assertIn("geography", {row["subject_code"] for row in config["subjects"]})

        created = create_student(
            self.conn,
            {
                "student_id": "STU-002",
                "display_name": "Learner Two",
                "timezone": "Asia/Shanghai",
                "subject_codes": ["english", "geography"],
            },
        )
        self.assertEqual(created["student_id"], "STU-002")
        students = student_summaries(self.conn)
        self.assertEqual(students["count"], 2)
        second = next(row for row in students["items"] if row["student_id"] == "STU-002")
        self.assertEqual({row["subject_code"] for row in second["subjects"]}, {"english", "geography"})

    def test_subject_evidence_is_isolated_by_student(self):
        create_student(
            self.conn,
            {"student_id": "STU-002", "display_name": "Learner Two", "subject_codes": ["english"]},
        )
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
        first = subject_overview(self.conn, "STU-001", "geography")
        self.assertEqual(second["summary"]["attempt_count"], 1)
        self.assertEqual(second["summary"]["accuracy"], 1.0)
        self.assertEqual(first["summary"]["attempt_count"], 0)
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
