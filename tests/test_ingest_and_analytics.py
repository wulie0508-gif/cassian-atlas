from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.analytics import session_acceptance_report, weakness_report
from english_tracker.db import connect, database_path, initialize_database
from english_tracker.ingest import import_attempts, import_session, undo_ingest_event
from english_tracker.quality import run_quality_checks


class TrackerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Demo Learner")
        self.conn = connect(database_path(self.data_dir))

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _session(self):
        return {
            "event_id": "EVT-S-1",
            "idempotency_key": "session-1",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "session": {
                "session_id": "SES-1",
                "session_type": "class",
                "title": "Demo",
                "started_at": "2026-01-01T10:00:00+08:00",
            },
            "observations": [{"observation_text": "A session-level note", "evidence_level": "session_only"}],
        }

    def _attempts(self):
        base = {
            "event_id": "EVT-A-1",
            "idempotency_key": "attempts-1",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "session_id": "SES-1",
            "attempts": [],
        }
        for index, result in enumerate(("correct", "wrong"), start=1):
            base["attempts"].append(
                {
                    "event_id": f"ATT-EVT-{index}",
                    "attempted_at": f"2026-01-01T10:00:0{index}+08:00",
                    "student_answer": None,
                    "standard_answer": "answer",
                    "answer_capture_status": "not_captured",
                    "response_mode": "production",
                    "evaluation": {"result": result, "score": 1 if result == "correct" else 0, "max_score": 1},
                    "error_types": [] if result == "correct" else ["blank"],
                    "item": {
                        "domain": "grammar",
                        "item_type": "cloze",
                        "prompt_snapshot": f"Prompt {index}",
                        "answer_snapshot": "answer",
                        "knowledge_points": ["noun_clause" if result == "wrong" else "tense"],
                        "external_references": [{"namespace": "demo", "reference_type": "question_id", "external_id": f"Q-{index}"}],
                    },
                }
            )
        return base

    def test_import_is_idempotent_and_session_report_is_evidence_based(self):
        import_session(self.conn, self._session())
        first = import_attempts(self.conn, self._attempts())
        second = import_attempts(self.conn, self._attempts())
        self.assertEqual(first["attempts_inserted"], 2)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
        report = session_acceptance_report(self.conn, "SES-1")
        self.assertEqual(report["accuracy"], 0.5)
        self.assertEqual(report["knowledge_point_errors"][0]["confidence"], "tentative")
        self.assertEqual(report["open_review_tasks_from_session"], 1)

    def test_weakness_report_exposes_sample_size_and_confidence(self):
        import_session(self.conn, self._session())
        import_attempts(self.conn, self._attempts())
        report = weakness_report(self.conn, "STU-001", as_of="2026-01-02T00:00:00+08:00")
        noun = next(row for row in report["windows"]["all_time"] if row["knowledge_point"] == "noun_clause")
        self.assertEqual(noun["sample_size"], 1)
        self.assertEqual(noun["confidence"], "tentative")
        self.assertEqual(noun["error_count"], 1)

    def test_undo_preserves_audit_and_removes_active_facts(self):
        import_session(self.conn, self._session())
        import_attempts(self.conn, self._attempts())
        result = undo_ingest_event(self.conn, "EVT-A-1", reason="test correction")
        self.assertEqual(result["status"], "reverted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts WHERE record_status='active'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts WHERE record_status='voided'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 1)

    def test_quality_checks_pass_on_valid_data(self):
        import_session(self.conn, self._session())
        import_attempts(self.conn, self._attempts())
        report = run_quality_checks(self.conn)
        self.assertEqual(report["trust_status"], "ready")
        self.assertEqual(report["summary"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()

