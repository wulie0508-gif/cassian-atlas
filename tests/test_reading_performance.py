from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.contracts import ContractError
from english_tracker.db import connect, database_path, initialize_database
from english_tracker.dashboard import learning_summary, low_friction_summary
from english_tracker.ingest import IngestConflict, import_attempt_diagnostics, import_attempts, import_session
from english_tracker.performance import reading_passage_performance, session_performance


class ReadingPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001")
        self.conn = connect(database_path(self.data_dir))
        self.question_bank = Path(self.temp.name) / "question-bank.sqlite"
        self._make_question_bank()
        import_session(
            self.conn,
            {
                "event_id": "EVT-READ-SESSION",
                "idempotency_key": "read-session-v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session": {
                    "session_id": "SES-READ",
                    "session_type": "class",
                    "title": "Reading lesson",
                    "started_at": "2026-08-01T09:00:00+08:00",
                },
            },
        )
        attempts = []
        for index, result in enumerate(("correct", "wrong"), start=1):
            attempts.append(
                {
                    "event_id": f"ATT-READ-{index}",
                    "attempted_at": f"2026-08-01T09:0{index}:00+08:00",
                    "student_answer": "A" if result == "correct" else "B",
                    "standard_answer": "A",
                    "answer_capture_status": "captured",
                    "evaluation": {"result": result, "score": int(result == "correct"), "max_score": 1},
                    "error_types": [],
                    "item": {
                        "domain": "reading",
                        "item_type": "multiple_choice",
                        "prompt_snapshot": f"Question {index}",
                        "answer_snapshot": "A",
                        "knowledge_points": ["reading_detail" if index == 1 else "reading_inference"],
                        "external_references": [
                            {
                                "namespace": "shanghai_question_bank",
                                "reference_type": "question_id",
                                "external_id": f"Q-READ-{index}",
                                "external_parent_id": "PAS-READ-1",
                                "source_validation_status": "source_checked",
                            }
                        ],
                    },
                }
            )
        import_attempts(
            self.conn,
            {
                "event_id": "EVT-READ-ATTEMPTS",
                "idempotency_key": "read-attempts-v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session_id": "SES-READ",
                "attempts": attempts,
            },
        )

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _make_question_bank(self):
        conn = sqlite3.connect(self.question_bank)
        conn.executescript(
            """
            CREATE TABLE passages(
              passage_id TEXT PRIMARY KEY,title TEXT,passage_text TEXT,passage_type TEXT,
              source_id TEXT,source_page INTEGER,original_number TEXT,word_count INTEGER,
              context_tags TEXT,verification_status TEXT
            );
            CREATE TABLE questions(
              question_id TEXT PRIMARY KEY,passage_id TEXT,question_type TEXT,original_number TEXT,
              stem TEXT,answer TEXT,explanation_raw TEXT,primary_test_point TEXT,
              secondary_test_points TEXT,difficulty TEXT,verification_status TEXT,
              source_path TEXT,source_page INTEGER,source_ordinal INTEGER,year INTEGER,
              district_or_school TEXT
            );
            INSERT INTO passages VALUES
              ('PAS-READ-1','Passage 1','Text','reading','SRC',1,'A',300,'','source_checked'),
              ('PAS-READ-2','Passage 2','Text 2','reading','SRC',2,'B',280,'','source_checked');
            INSERT INTO questions VALUES
              ('Q-READ-1','PAS-READ-1','阅读理解','1','Which detail?','A','Locate evidence.','细节理解','', '基础','source_checked','paper.pdf',1,1,2026,'Demo'),
              ('Q-READ-2','PAS-READ-1','阅读理解','2','What can be inferred?','A','Infer from evidence.','推理判断','', '中等','source_checked','paper.pdf',1,2,2026,'Demo'),
              ('Q-READ-3','PAS-READ-2','阅读理解','1','Another inference?','C','Infer from evidence.','推理判断','', '中等','source_checked','paper.pdf',2,1,2025,'Demo');
            """
        )
        conn.commit()
        conn.close()

    def test_classroom_attempts_are_real_scores_without_assessment_row(self):
        report = session_performance(self.conn, "STU-001")
        self.assertEqual(report["count"], 1)
        session = report["items"][0]
        self.assertTrue(session["is_real_performance_evidence"])
        self.assertFalse(session["is_calibration_anchor"])
        self.assertEqual(session["assessment_kind"], "lesson")
        self.assertEqual(session["derived_score"], 1.0)
        self.assertEqual(session["derived_max_score"], 2)
        self.assertEqual(session["accuracy"], 0.5)

    def test_overview_counts_only_active_performance_facts(self):
        self.assertEqual(learning_summary(self.conn, "STU-001")["counts"]["attempts"], 2)
        attempt_id = self.conn.execute("SELECT attempt_id FROM attempts ORDER BY attempt_id LIMIT 1").fetchone()[0]
        self.conn.execute("UPDATE attempts SET record_status='voided' WHERE attempt_id=?", (attempt_id,))
        self.conn.commit()
        self.assertEqual(learning_summary(self.conn, "STU-001")["counts"]["attempts"], 1)

    def test_low_friction_summary_keeps_action_surface_small(self):
        summary = low_friction_summary(self.conn, "STU-001")
        self.assertEqual(summary["mode"], "low_friction_v1")
        self.assertEqual(summary["current"]["attempt_count"], 2)
        self.assertEqual(summary["current"]["reading_attempt_count"], 2)
        self.assertEqual(summary["current"]["calibration_anchor_count"], 0)
        self.assertEqual(summary["current"]["dictation_plan_size"], 0)
        self.assertEqual(summary["current"]["vocabulary_due_total"], 0)
        self.assertEqual(len(summary["automation"]), 3)
        self.assertTrue(any("线下测" in item["owner"] for item in summary["next_actions"]))
        self.assertIn("performance", summary["detail_endpoints"])

    def test_reading_passage_keeps_test_points_separate_from_error_causes(self):
        report = reading_passage_performance(self.conn, self.question_bank, "STU-001", "PAS-READ-1")
        self.assertEqual(report["summary"]["question_count"], 2)
        self.assertEqual(report["summary"]["wrong_count"], 1)
        self.assertEqual(report["summary"]["pending_diagnosis_count"], 1)
        self.assertEqual(report["questions"][1]["primary_test_point"], "推理判断")
        self.assertEqual(report["questions"][1]["attempts"][0]["error_causes"], [])
        self.assertEqual(report["similar_questions"][0]["question_id"], "Q-READ-3")

        result = import_attempt_diagnostics(
            self.conn,
            {
                "event_id": "EVT-DIAG-1",
                "idempotency_key": "reading-diagnostic-v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "diagnostics": [
                    {
                        "attempt_id": "ATT-" + self.conn.execute(
                            "SELECT attempt_id FROM attempts WHERE event_id='ATT-READ-2'"
                        ).fetchone()[0].removeprefix("ATT-"),
                        "error_types": [
                            {
                                "code": "reading_inference_overreach",
                                "error_source": "model_suggested",
                                "verification_status": "suggested",
                                "confidence": 0.82,
                                "rationale": "Student selected B; B adds a conclusion not supported by the passage evidence.",
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(result["diagnostics_inserted"], 1)
        refreshed = reading_passage_performance(self.conn, self.question_bank, "STU-001", "PAS-READ-1")
        cause = refreshed["questions"][1]["attempts"][0]["error_causes"][0]
        self.assertEqual(cause["code"], "reading_inference_overreach")
        self.assertEqual(cause["verification_status"], "suggested")

    def test_model_cannot_auto_verify_and_missing_answer_blocks_diagnosis(self):
        attempt_id = self.conn.execute("SELECT attempt_id FROM attempts WHERE event_id='ATT-READ-2'").fetchone()[0]
        with self.assertRaises(ContractError):
            import_attempt_diagnostics(
                self.conn,
                {
                    "event_id": "EVT-DIAG-BAD",
                    "idempotency_key": "reading-diagnostic-bad",
                    "source_thread": "courseware",
                    "student_id": "STU-001",
                    "diagnostics": [{"attempt_id": attempt_id, "error_types": [{"code": "reading_inference_overreach", "error_source": "model_suggested", "verification_status": "verified", "rationale": "Evidence"}]}],
                },
            )
        self.conn.execute("UPDATE attempts SET answer_capture_status='not_captured',student_answer=NULL WHERE attempt_id=?", (attempt_id,))
        self.conn.commit()
        with self.assertRaises(IngestConflict):
            import_attempt_diagnostics(
                self.conn,
                {
                    "event_id": "EVT-DIAG-NOANSWER",
                    "idempotency_key": "reading-diagnostic-noanswer",
                    "source_thread": "manual",
                    "student_id": "STU-001",
                    "diagnostics": [{"attempt_id": attempt_id, "error_types": [{"code": "reading_inference_overreach", "error_source": "teacher_observation", "verification_status": "verified", "rationale": "Would otherwise be specific."}]}],
                },
            )


if __name__ == "__main__":
    unittest.main()
