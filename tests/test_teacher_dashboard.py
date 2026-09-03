from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.dashboard import teacher_dashboard
from english_tracker.db import connect, database_path, initialize_database
from english_tracker.ingest import import_attempts, import_session
from english_tracker.workspace import create_student, enroll_student


class TeacherDashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(
            self.data_dir, student_id="STU-001", display_name="Primary learner"
        )
        self.conn = connect(database_path(self.data_dir))
        create_student(
            self.conn,
            {
                "student_id": "STU-002",
                "display_name": "Empty learner",
                "subject_codes": ["english"],
            },
        )
        enroll_student(self.conn, "STU-001", ["geography"])

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _record_session(
        self,
        suffix: str,
        *,
        student_id: str = "STU-001",
        subject_code: str = "english",
        day: int,
        series: str,
        items: list[dict],
        kind: str = "lesson",
        delivery_mode: str = "offline_open",
    ) -> None:
        session_id = f"SES-{suffix}"
        import_session(
            self.conn,
            {
                "event_id": f"EVT-SESSION-{suffix}",
                "idempotency_key": f"teacher-dashboard:session:{suffix}:v1",
                "source_thread": "manual",
                "student_id": student_id,
                "session": {
                    "session_id": session_id,
                    "session_type": "lesson",
                    "title": suffix,
                    "started_at": f"2026-08-{day:02d}T09:00:00+08:00",
                },
                "assessment": {
                    "assessment_kind": kind,
                    "reporting_series": series,
                    "delivery_mode": delivery_mode,
                    "validation_status": "verified",
                },
            },
        )
        payload = {
            "event_id": f"EVT-ATTEMPTS-{suffix}",
            "idempotency_key": f"teacher-dashboard:attempts:{suffix}:v1",
            "source_thread": "manual",
            "student_id": student_id,
            "session_id": session_id,
            "attempts": [],
        }
        for index, item in enumerate(items, 1):
            result = item["result"]
            item_id = item.get("item_id") or f"ITEM-{suffix}-{index}"
            knowledge_code = item.get("knowledge_point", "tense")
            answer = "ok" if result == "correct" else "student-answer"
            payload["attempts"].append(
                {
                    "event_id": f"ATT-{suffix}-{index}",
                    "attempted_at": f"2026-08-{day:02d}T09:{index:02d}:00+08:00",
                    "attempt_phase": item.get("attempt_phase", "first"),
                    "student_answer": answer,
                    "standard_answer": "ok",
                    "answer_capture_status": "captured",
                    "validation_status": "verified",
                    "evaluation": {
                        "result": result,
                        "score": 1 if result == "correct" else 0,
                        "max_score": 1,
                    },
                    "error_types": item.get("error_types", []),
                    "item": {
                        "item_id": item_id,
                        "subject_code": subject_code,
                        "domain": "grammar",
                        "item_type": "cloze",
                        "prompt_snapshot": f"Prompt {item_id}",
                        "answer_snapshot": "ok",
                        "source_validation_status": "source_checked",
                        "knowledge_points": [
                            {
                                "code": knowledge_code,
                                "mapping_source": "manual",
                                "verification_status": "source_checked",
                            }
                        ],
                    },
                }
            )
        import_attempts(self.conn, payload)

    def _seed_decision_data(self) -> None:
        self._record_session(
            "A1",
            day=1,
            series="grammar-series",
            items=[
                {"result": "wrong", "item_id": "ITEM-REVIEW"},
                {"result": "wrong"},
                {"result": "correct"},
                {"result": "correct"},
            ],
        )
        self._record_session(
            "A2",
            day=2,
            series="grammar-series",
            items=[
                {"result": "wrong"},
                {"result": "correct"},
                {"result": "correct"},
                {"result": "correct"},
            ],
        )
        self._record_session(
            "B1",
            day=3,
            series="other-series",
            items=[
                {"result": "wrong", "knowledge_point": "noun_clause"},
                {"result": "correct", "knowledge_point": "noun_clause"},
                {"result": "wrong", "knowledge_point": "inversion"},
            ],
        )
        self._record_session(
            "A3",
            day=4,
            series="grammar-series",
            items=[
                {
                    "result": "correct",
                    "item_id": "ITEM-REVIEW",
                    "attempt_phase": "review",
                },
                {"result": "correct"},
                {"result": "correct"},
                {"result": "correct"},
            ],
        )

    def test_comparable_series_and_stable_signals_are_kept_separate(self):
        self._seed_decision_data()
        result = teacher_dashboard(
            self.conn,
            "STU-001",
            subject_code="english",
            as_of="2026-08-10",
        )

        comparable = result["comparable_performance"]
        self.assertEqual(comparable["series_key"], "lesson|grammar-series")
        self.assertEqual([point["session_id"] for point in comparable["points"]], ["SES-A1", "SES-A2", "SES-A3"])
        self.assertEqual(comparable["latest"]["accuracy"], 1.0)
        self.assertEqual(comparable["previous"]["accuracy"], 0.75)
        self.assertEqual(comparable["change"], 0.25)
        self.assertEqual(comparable["sample"]["scored_attempt_count"], 12)

        stable_codes = {row["knowledge_point"] for row in result["teaching_priorities"]}
        confirmation_codes = {
            row["knowledge_point"] for row in result["confirmation_signals"]
        }
        self.assertIn("tense", stable_codes)
        self.assertNotIn("noun_clause", stable_codes)
        self.assertTrue({"noun_clause", "inversion"} & confirmation_codes)
        self.assertEqual(result["calibration"]["status"], "missing")
        self.assertIsNone(result["calibration"]["latest_anchor"])
        self.assertEqual(
            result["review_health"]["latest_retest_recovery"]["rate"], 1.0
        )
        self.assertGreater(result["review_health"]["open_due_total"], 0)
        self.assertEqual(result["next_action"]["action"], "teach_stable_weakness")
        self.assertIn("请为 STU-001", result["next_action"]["codex_prompt"])

    def test_student_and_subject_data_are_isolated(self):
        self._seed_decision_data()
        self._record_session(
            "GEO",
            day=5,
            subject_code="geography",
            series="geography-series",
            items=[{"result": "wrong"}],
        )

        english = teacher_dashboard(
            self.conn,
            "STU-001",
            subject_code="english",
            as_of="2026-08-10",
        )
        geography = teacher_dashboard(
            self.conn,
            "STU-001",
            subject_code="geography",
            as_of="2026-08-10",
        )
        empty_other_student = teacher_dashboard(
            self.conn,
            "STU-002",
            subject_code="english",
            as_of="2026-08-10",
        )

        self.assertEqual(english["data_coverage"]["scored_attempts"]["numerator"], 15)
        self.assertEqual(geography["data_coverage"]["scored_attempts"]["numerator"], 1)
        self.assertEqual(empty_other_student["data_coverage"]["scored_attempts"]["numerator"], 0)
        self.assertEqual(empty_other_student["teaching_priorities"], [])
        self.assertIsNone(empty_other_student["comparable_performance"]["latest"])

    def test_empty_dashboard_returns_explicit_nulls_without_invented_trends(self):
        result = teacher_dashboard(
            self.conn,
            "STU-002",
            subject_code="english",
            as_of="2026-08-10",
        )
        self.assertEqual(result["freshness"]["status"], "no_data")
        self.assertIsNone(result["freshness"]["last_scored_attempt"])
        self.assertEqual(result["comparable_performance"]["points"], [])
        self.assertIsNone(result["comparable_performance"]["change"])
        self.assertEqual(result["teaching_priorities"], [])
        self.assertEqual(result["confirmation_signals"], [])
        self.assertEqual(result["review_health"]["open_due_total"], 0)
        self.assertIsNone(
            result["review_health"]["latest_retest_recovery"]["period"]
        )
        self.assertEqual(result["calibration"]["status"], "missing")
        self.assertIsNone(result["calibration"]["offline_accuracy"])
        self.assertIsNone(result["data_coverage"]["scored_attempts"]["rate"])
        self.assertEqual(result["next_action"]["action"], "collect_baseline")

    def test_unenrolled_subject_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not enrolled"):
            teacher_dashboard(
                self.conn,
                "STU-002",
                subject_code="geography",
                as_of="2026-08-10",
            )


if __name__ == "__main__":
    unittest.main()
