from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.ingest import (
    IngestConflict,
    import_attempts,
    import_session,
    replace_session_attempts,
)
from english_tracker.quality import run_quality_checks


class AtomicAttemptReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Demo")
        self.conn = connect(database_path(self.data_dir))
        import_session(
            self.conn,
            {
                "event_id": "EVT-SESSION-56",
                "idempotency_key": "session-56:v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session": {
                    "session_id": "SES-56",
                    "session_type": "full_exam",
                    "title": "56 item regression",
                    "started_at": "2026-08-29T09:00:00+08:00",
                },
                "assessment": {
                    "assessment_kind": "full_exam",
                    "reporting_series": "regression",
                    "delivery_mode": "offline_closed",
                    "raw_score": 102,
                    "max_score": 112,
                    "blank_count": 5,
                    "validation_status": "verified",
                },
            },
        )
        attempts = []
        for index in range(1, 57):
            blank = index <= 5
            attempts.append(
                {
                    "attempt_id": f"ATT-ORIGINAL-{index:02d}",
                    "event_id": f"ATT-EVENT-{index:02d}",
                    "attempted_at": f"2026-08-29T09:{index:02d}:00+08:00",
                    "student_answer": "" if blank else "answer",
                    "standard_answer": "answer",
                    "answer_capture_status": "captured_blank" if blank else "captured",
                    "attempt_phase": "exam",
                    "response_mode": "production",
                    "validation_status": "verified",
                    "evaluation": {
                        "result": "wrong" if blank else "correct",
                        "score": 0 if blank else 2,
                        "max_score": 2,
                    },
                    "error_types": ["needs_check"] if blank else [],
                    "item": {
                        "item_id": f"ITEM-{index:02d}",
                        "domain": "assessment",
                        "item_type": "constructed_response",
                        "prompt_snapshot": f"Prompt {index}",
                        "answer_snapshot": "answer",
                    },
                }
            )
        import_attempts(
            self.conn,
            {
                "event_id": "EVT-ATTEMPTS-56",
                "idempotency_key": "attempts-56:v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session_id": "SES-56",
                "attempts": attempts,
            },
        )

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _replacement_payload(self, event_id: str = "EVT-REPLACE-5") -> dict:
        return {
            "event_id": event_id,
            "idempotency_key": f"{event_id}:v1",
            "source_thread": "manual",
            "student_id": "STU-001",
            "session_id": "SES-56",
            "replacements": [
                {
                    "old_attempt_id": f"ATT-ORIGINAL-{index:02d}",
                    "attempt_id": f"ATT-REPLACEMENT-{index:02d}-{event_id}",
                    "event_id": f"ATT-REPLACEMENT-EVENT-{index:02d}-{event_id}",
                    "student_answer": f"verified answer {index}",
                    "answer_capture_status": "captured",
                    "validation_status": "verified",
                    "evaluation": {
                        "result": "partial",
                        "score": 1,
                        "max_score": 2,
                        "evaluated_by": "teacher",
                        "is_human_corrected": True,
                    },
                    "error_types": ["needs_check"],
                }
                for index in range(1, 6)
            ],
        }

    def test_five_replacements_keep_56_active_slots_and_rebuild_assessment_reviews(self):
        payload = self._replacement_payload()
        result = replace_session_attempts(
            self.conn,
            payload,
            actor="teacher",
            reason="verified missing answers",
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["assessment"]["active_attempt_count"], 56)
        self.assertEqual(result["assessment"]["raw_score"], 107)
        self.assertEqual(result["assessment"]["max_score"], 112)
        self.assertEqual(result["assessment"]["blank_count"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE session_id='SES-56' AND record_status='active'"
            ).fetchone()[0],
            56,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(DISTINCT item_id) FROM attempts WHERE session_id='SES-56' AND record_status='active'"
            ).fetchone()[0],
            56,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 61)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts WHERE record_status='superseded'").fetchone()[0],
            5,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM review_tasks WHERE status='open'").fetchone()[0],
            5,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM review_tasks WHERE status='voided'").fetchone()[0],
            5,
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM review_tasks rt
                JOIN attempts a ON a.attempt_id=rt.source_attempt_id
                WHERE rt.status='open' AND a.record_status='active'
                """
            ).fetchone()[0],
            5,
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM attempt_error_map aem
                JOIN attempts a ON a.attempt_id=aem.attempt_id
                WHERE a.record_status='superseded' AND aem.record_status='voided'
                """
            ).fetchone()[0],
            5,
        )
        assessment = self.conn.execute(
            "SELECT raw_score,max_score,blank_count FROM session_assessments WHERE session_id='SES-56'"
        ).fetchone()
        self.assertEqual(tuple(assessment), (107, 112, 0))
        self.assertEqual(run_quality_checks(self.conn)["trust_status"], "ready")

        duplicate = replace_session_attempts(self.conn, copy.deepcopy(payload))
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 61)

    def test_failure_after_first_mutation_rolls_back_every_change(self):
        payload = self._replacement_payload("EVT-ROLLBACK")
        payload["replacements"][1]["error_types"] = ["needs_check", "needs_check"]
        with self.assertRaises(IngestConflict):
            replace_session_attempts(self.conn, payload)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 56)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts WHERE record_status='active'").fetchone()[0],
            56,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM review_tasks WHERE status='open'").fetchone()[0],
            5,
        )
        assessment = self.conn.execute(
            "SELECT raw_score,max_score,blank_count FROM session_assessments WHERE session_id='SES-56'"
        ).fetchone()
        self.assertEqual(tuple(assessment), (102, 112, 5))
        self.assertIsNone(
            self.conn.execute(
                "SELECT ingest_event_id FROM ingest_events WHERE ingest_event_id='EVT-ROLLBACK'"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
