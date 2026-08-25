from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.ingest import import_attempts, import_session
from english_tracker.workflows import record_assessment, record_dictation


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner")
        self.conn = connect(database_path(self.data_dir))

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def test_assessment_record_is_deterministic_and_idempotent(self):
        payload = {
            "student_id": "STU-001",
            "title": "Monthly check",
            "date": "2026-08-23",
            "assessment_kind": "topic_quiz",
            "raw_score": 72,
            "max_score": 100,
        }
        first = record_assessment(self.conn, payload)
        second = record_assessment(self.conn, payload)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "duplicate")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM learning_sessions WHERE student_id='STU-001'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_dictation_record_grades_and_replays_safely(self):
        import_session(
            self.conn,
            {
                "event_id": "EVT-SEED-SESSION",
                "idempotency_key": "seed-session-v1",
                "source_thread": "migration",
                "student_id": "STU-001",
                "session": {
                    "session_id": "SES-SEED",
                    "session_type": "migration",
                    "title": "Seed vocabulary",
                    "started_at": "2026-08-22T08:00:00+08:00",
                },
            },
        )
        import_attempts(
            self.conn,
            {
                "event_id": "EVT-SEED-ATTEMPTS",
                "idempotency_key": "seed-attempts-v1",
                "source_thread": "migration",
                "student_id": "STU-001",
                "session_id": "SES-SEED",
                "attempts": [
                    {
                        "event_id": "ATT-SEED-WORD",
                        "attempted_at": "2026-08-22T08:01:00+08:00",
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
                    }
                ],
            },
        )
        payload = {
            "student_id": "STU-001",
            "title": "Morning dictation",
            "date": "2026-08-23",
            "items": [{"item_id": "WORD-DELIVER", "student_answer": "deliver"}],
        }
        first = record_dictation(self.conn, payload)
        second = record_dictation(self.conn, payload)
        self.assertEqual(first["correct"], 1)
        self.assertEqual(first["total"], 1)
        self.assertEqual(second["session_result"]["status"], "duplicate")
        self.assertEqual(second["attempts_result"]["status"], "duplicate")


if __name__ == "__main__":
    unittest.main()
