from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.grammar_catalog import coverage_matrix, question_knowledge, sync_grammar_catalog
from english_tracker.ingest import import_attempts, import_session
from english_tracker.metrics import trend_report, weekly_report
from english_tracker.quality import run_quality_checks
from english_tracker.selection import weighted_set_cover


class GrammarCoverageAndMetricsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001")
        self.conn = connect(database_path(self.data_dir))
        self.question_bank = Path(self.temp.name) / "question-bank.sqlite"
        self._make_question_bank()

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _make_question_bank(self):
        source = sqlite3.connect(self.question_bank)
        source.executescript(
            """
            CREATE TABLE passages(
              passage_id TEXT PRIMARY KEY, title TEXT, source_id TEXT,
              verification_status TEXT
            );
            CREATE TABLE questions(
              question_id TEXT PRIMARY KEY, passage_id TEXT, source_id TEXT,
              original_number TEXT, year INTEGER, exam_type TEXT,
              district_or_school TEXT, difficulty TEXT, answer TEXT,
              explanation_raw TEXT, primary_test_point TEXT,
              secondary_test_points TEXT, source_ordinal INTEGER,
              question_type TEXT, verification_status TEXT
            );
            INSERT INTO passages VALUES
              ('PAS-DEMO-1','Passage One','SRC-DEMO','source_checked'),
              ('PAS-DEMO-2','Passage Two','SRC-DEMO','source_checked');
            INSERT INTO questions VALUES
              ('Q-DEMO-1','PAS-DEMO-1','SRC-DEMO','1',2026,'mock','school','basic','has worked','考查动词时态。','动词时态','句子结构与完整主干',1,'语法填空','source_checked'),
              ('Q-DEMO-2','PAS-DEMO-1','SRC-DEMO','2',2026,'mock','school','basic','and','考查并列连词，连接两个并列分句。','并列与连接','',2,'语法填空','source_checked'),
              ('Q-DEMO-3','PAS-DEMO-2','SRC-DEMO','1',2026,'mock','school','basic','what','考查名词性从句，从句中缺少宾语。','名词性从句','代词',1,'语法填空','source_checked');
            """
        )
        source.commit()
        source.close()

    def test_catalog_matrix_selection_and_model_guard(self):
        synced = sync_grammar_catalog(self.conn, self.question_bank)
        self.assertEqual(synced["question_count"], 3)
        self.assertEqual(synced["complete_passage_count"], 2)
        replay = sync_grammar_catalog(self.conn, self.question_bank)
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM grammar_question_catalog").fetchone()[0], 3)
        question = question_knowledge(self.conn, "Q-DEMO-1")
        self.assertTrue(any(row["code"] == "tense" and row["verification_status"] == "source_checked" for row in question["mappings"]))
        matrix = coverage_matrix(
            self.conn,
            ["PAS-DEMO-1", "PAS-DEMO-2"],
            required_codes=["tense", "noun_clause"],
            minimum_confirmed_questions=1,
        )
        self.assertEqual(matrix["uncovered"], [])
        selection = weighted_set_cover(self.conn, ["tense", "noun_clause"], max_passages=2)
        self.assertEqual(len(selection["selected_passages"]), 2)
        self.assertEqual(selection["uncovered"], [])

        snapshot = synced["source_snapshot_id"]
        noun_id = self.conn.execute("SELECT knowledge_point_id FROM knowledge_points WHERE code='noun_clause'").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO question_knowledge_map(
                  source_snapshot_id,question_id,knowledge_point_id,role,
                  mapping_source,confidence,verification_status,rationale,
                  created_at,updated_at
                ) VALUES (?, 'Q-DEMO-1', ?, 'trap', 'model_suggested', 0.9,
                          'verified', 'invalid auto-promotion', 'now', 'now')
                """,
                (snapshot, noun_id),
            )
        self.assertEqual(run_quality_checks(self.conn)["trust_status"], "ready")

    def _session(self, suffix: str, kind: str, series: str, raw: float, maximum: float):
        return {
            "event_id": f"EVT-S-{suffix}",
            "idempotency_key": f"session:{suffix}",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "session": {
                "session_id": f"SES-{suffix}",
                "session_type": "test",
                "title": suffix,
                "started_at": f"2026-12-0{suffix}T09:00:00+00:00",
            },
            "assessment": {
                "assessment_kind": kind,
                "reporting_series": series,
                "delivery_mode": "offline_closed",
                "raw_score": raw,
                "max_score": maximum,
                "duration_seconds": 600,
                "validation_status": "verified",
            },
        }

    def test_weekly_metrics_and_raw_score_series_are_partitioned(self):
        import_session(self.conn, self._session("1", "topic_quiz", "grammar-fill", 1, 2))
        import_session(self.conn, self._session("2", "full_exam", "formal-paper", 80, 100))
        import_session(self.conn, self._session("3", "full_exam", "formal-paper", 120, 150))
        attempts = {
            "event_id": "EVT-A-1",
            "idempotency_key": "attempts:1",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "session_id": "SES-1",
            "attempts": [],
        }
        for index, (result, capture, answer) in enumerate((("correct", "captured", "ok"), ("wrong", "captured_blank", "")), 1):
            attempts["attempts"].append(
                {
                    "event_id": f"ATT-EVT-{index}",
                    "attempted_at": f"2026-12-01T09:00:0{index}+00:00",
                    "student_answer": answer,
                    "standard_answer": "ok",
                    "answer_capture_status": capture,
                    "evaluation": {"result": result, "score": int(result == "correct"), "max_score": 1},
                    "error_types": [],
                    "item": {
                        "domain": "grammar",
                        "item_type": "cloze",
                        "prompt_snapshot": f"Prompt {index}",
                        "answer_snapshot": "ok",
                        "knowledge_points": ["tense"],
                    },
                }
            )
        import_attempts(self.conn, attempts)
        weekly = weekly_report(self.conn, "STU-001", week_start="2026-11-30")
        self.assertEqual(weekly["topic_accuracy"][0]["accuracy"], 0.5)
        self.assertEqual(weekly["blank_rate"]["rate"], 0.5)
        trend = trend_report(self.conn, "STU-001", start="2026-12-01", end="2026-12-07")
        self.assertEqual(len(trend["raw_score_series"]), 3)
        keys = {row["series_key"] for row in trend["raw_score_series"]}
        self.assertIn("full_exam|formal-paper|max=100", keys)
        self.assertIn("full_exam|formal-paper|max=150", keys)


if __name__ == "__main__":
    unittest.main()
