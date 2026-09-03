from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.base_projection import stage_projection_run
from english_tracker.db import connect, database_path, initialize_database, migration_status
from english_tracker.extraction import (
    ExtractionConflict,
    commit_extraction_batch,
    create_extraction_batch,
    extraction_review,
    submit_human_decisions,
    submit_provider_results,
)
from english_tracker.ingest import import_session
from english_tracker.quality import run_quality_checks
from english_tracker.selection_manifests import (
    cache_public_explanation,
    create_question_selection_manifest,
    lookup_public_explanation,
)


STUDENT_ID = "STU-E2E-ANON-001"
SESSION_ID = "SES-E2E-ANON-001"
DATA_AS_OF = "2026-09-02T08:30:00Z"


def _synthetic_cassian_target() -> dict:
    return {
        "schema_version": "synthetic-e2e-v1",
        "primary_target": {
            "tenant_display_name": "Cassian Learning Lab | 学习工作室",
            "account_purpose": "all-local contract regression",
            "cli_profile": "cassian-learning-hub",
            "identity": "user",
            "app_name": "Cassian Learning Ops",
            "students": {
                STUDENT_ID: {
                    "display_name": "Anonymous learner",
                    "folder": {
                        "name": "Anonymous learner folder",
                        "token": "fldcnAnonymousE2E001",
                        "url": (
                            "https://cassian-e2e.feishu.cn/drive/folder/"
                            "fldcnAnonymousE2E001"
                        ),
                    },
                    "base": {
                        "name": "Anonymous learner operations",
                        "token": "bascnAnonymousE2E001",
                        "url": (
                            "https://cassian-e2e.feishu.cn/base/"
                            "bascnAnonymousE2E001"
                        ),
                    },
                }
            },
        },
        "write_guard": {
            "require_explicit_profile": True,
            "required_profile": "cassian-learning-hub",
            "required_identity": "user",
            "fail_closed_on_mismatch": True,
            "upload_question_bank": False,
        },
    }


class CodexFirstAllLocalE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "private-ledger"
        self.question_bank = self.root / "verified-question-bank.sqlite"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()

        # These public flows are intentionally local-only. Keep the common network
        # and CLI escape hatches closed for the entire initialized workflow.
        self.network = patch("socket.create_connection")
        self.urlopen = patch("urllib.request.urlopen")
        self.cli_run = patch("subprocess.run")
        self.cli_popen = patch("subprocess.Popen")
        self.network_mock = self.network.start()
        self.urlopen_mock = self.urlopen.start()
        self.cli_run_mock = self.cli_run.start()
        self.cli_popen_mock = self.cli_popen.start()

        self.initialization = initialize_database(
            self.data_dir,
            student_id=STUDENT_ID,
            display_name="Anonymous learner",
        )
        self.conn = connect(database_path(self.data_dir))
        import_session(
            self.conn,
            {
                "event_id": "EVT-E2E-SESSION-001",
                "idempotency_key": "e2e:anonymous-session:v1",
                "source_thread": "courseware",
                "student_id": STUDENT_ID,
                "session": {
                    "session_id": SESSION_ID,
                    "session_type": "homework",
                    "title": "Anonymous mixed evidence session",
                    "started_at": "2026-09-02T16:00:00+08:00",
                },
            },
        )
        self._create_verified_question_bank()

    def tearDown(self) -> None:
        self.conn.close()
        self.cli_popen.stop()
        self.cli_run.stop()
        self.urlopen.stop()
        self.network.stop()
        self.env.stop()
        self.temp.cleanup()

    def _create_verified_question_bank(self) -> None:
        fixture = sqlite3.connect(self.question_bank)
        try:
            fixture.executescript(
                """
                CREATE TABLE sources(
                  source_id TEXT PRIMARY KEY,
                  title TEXT,
                  source_mode TEXT,
                  original_path TEXT,
                  pdf_original_path TEXT,
                  processing_status TEXT,
                  notes TEXT
                );
                CREATE TABLE passages(
                  passage_id TEXT PRIMARY KEY,
                  title TEXT,
                  passage_text TEXT,
                  passage_type TEXT,
                  source_id TEXT,
                  source_page INTEGER,
                  original_number TEXT,
                  verification_status TEXT
                );
                CREATE TABLE questions(
                  question_id TEXT PRIMARY KEY,
                  passage_id TEXT,
                  source_id TEXT,
                  question_type TEXT,
                  original_number TEXT,
                  source_page INTEGER,
                  stem TEXT,
                  answer TEXT,
                  primary_test_point TEXT,
                  secondary_test_points TEXT,
                  difficulty TEXT,
                  verification_status TEXT,
                  source_path TEXT,
                  source_ordinal INTEGER
                );
                CREATE TABLE options(
                  question_id TEXT,
                  option_label TEXT,
                  option_text TEXT,
                  option_order INTEGER,
                  PRIMARY KEY(question_id, option_label)
                );

                INSERT INTO sources VALUES (
                  'SRC-E2E', 'Verified local paper', 'question_only',
                  'verified-paper.pdf', NULL, 'source_checked', ''
                );
                INSERT INTO passages VALUES (
                  'PASS-E2E', 'Reading passage',
                  'Trees cool streets and absorb rainwater. Researchers observed both effects.',
                  'reading', 'SRC-E2E', 1, 'A', 'source_checked'
                );
                INSERT INTO questions VALUES
                  ('QB-Q1', 'PASS-E2E', 'SRC-E2E', '阅读理解', '1', 1,
                   'Which effect of trees is stated first?', 'A', 'reading_detail', '',
                   'basic', 'source_checked', 'verified-paper.pdf', 1),
                  ('QB-Q2', 'PASS-E2E', 'SRC-E2E', '阅读理解', '2', 1,
                   'What else do trees absorb?', 'B', 'reading_detail', '',
                   'basic', 'verified', 'verified-paper.pdf', 2);
                INSERT INTO options VALUES
                  ('QB-Q1', 'A', 'They cool streets.', 1),
                  ('QB-Q1', 'B', 'They produce electricity.', 2),
                  ('QB-Q2', 'A', 'Traffic noise.', 1),
                  ('QB-Q2', 'B', 'Rainwater.', 2);
                """
            )
            fixture.commit()
        finally:
            fixture.close()

    @staticmethod
    def _fact_counts(conn: sqlite3.Connection) -> dict[str, int]:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "attempts",
                "evaluations",
                "review_state",
                "review_tasks",
                "mastery_snapshots",
            )
        }

    def _create_mixed_batch(self) -> str:
        created = create_extraction_batch(
            self.conn,
            {
                "idempotency_key": "e2e:mixed-extraction:v1",
                "student_id": STUDENT_ID,
                "subject_code": "english",
                "session_id": SESSION_ID,
                "source_thread": "courseware",
                "title": "Anonymous MC, short response, and translation",
                "source_images": [
                    {
                        "extraction_asset_id": "XAST-E2E-001",
                        "source_uri": "private://anonymous/e2e-answer-page-1",
                        "sha256": "1" * 64,
                        "media_type": "image/png",
                        "byte_size": 256,
                        "page_number": 1,
                    }
                ],
                "items": [
                    {
                        "extraction_item_id": "XITEM-E2E-MC",
                        "extraction_asset_id": "XAST-E2E-001",
                        "ordinal": 1,
                        "question_ref": "Q-MC",
                        "question_type": "multiple_choice",
                        "risk_level": "R0",
                        "evidence_locator": {"page": 1, "box": [10, 10, 30, 24]},
                        "attempt_template": {
                            "attempted_at": "2026-09-02T16:05:00+08:00",
                            "standard_answer": "B",
                            "response_mode": "recognition",
                            "validation_status": "source_checked",
                            "item": {
                                "subject_code": "english",
                                "domain": "reading",
                                "item_type": "multiple_choice",
                                "prompt_snapshot": "Anonymous multiple-choice prompt",
                                "answer_snapshot": "B",
                            },
                            "grading_contract": {
                                "mode": "deterministic_exact",
                                "acceptable_answers": ["B"],
                                "case_sensitive": False,
                                "max_score": 1,
                            },
                        },
                    },
                    {
                        "extraction_item_id": "XITEM-E2E-R2",
                        "extraction_asset_id": "XAST-E2E-001",
                        "ordinal": 2,
                        "question_ref": "Q-SHORT",
                        "question_type": "short_response",
                        "risk_level": "R2",
                        "second_model_reason": "handwritten_short_response",
                        "evidence_locator": {"page": 1, "box": [10, 30, 120, 54]},
                        "attempt_template": {
                            "attempted_at": "2026-09-02T16:06:00+08:00",
                            "standard_answer": "because it was raining",
                            "response_mode": "production",
                            "validation_status": "source_checked",
                            "item": {
                                "subject_code": "english",
                                "domain": "grammar",
                                "item_type": "short_response",
                                "prompt_snapshot": "Anonymous short-response prompt",
                                "answer_snapshot": "because it was raining",
                            },
                            "grading_contract": {
                                "mode": "deterministic_exact",
                                "acceptable_answers": ["because it was raining"],
                                "case_sensitive": False,
                                "max_score": 1,
                            },
                        },
                    },
                    {
                        "extraction_item_id": "XITEM-E2E-R3",
                        "extraction_asset_id": "XAST-E2E-001",
                        "ordinal": 3,
                        "question_ref": "Q-TRANSLATION",
                        "question_type": "translation",
                        "risk_level": "R3",
                        "second_model_reason": "long_answer_calibration",
                        "evidence_locator": {"page": 1, "box": [10, 60, 180, 110]},
                        "attempt_template": {
                            "attempted_at": "2026-09-02T16:07:00+08:00",
                            "response_mode": "production",
                            "validation_status": "source_checked",
                            "item": {
                                "subject_code": "english",
                                "domain": "translation",
                                "item_type": "translation",
                                "prompt_snapshot": "Anonymous translation prompt",
                            },
                            "grading_contract": {"mode": "teacher_confirmed"},
                        },
                    },
                ],
            },
        )
        self.assertEqual(created["status"], "created")
        return created["batch"]["extraction_batch_id"]

    def _submit_provider_candidates(self, batch_id: str) -> None:
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "e2e:provider:deterministic:v1",
                "provider": "deterministic",
                "model_version": "answer-grid-v1",
                "prompt_version": "grid-v1",
                "completed_at": "2026-09-02T16:10:00+08:00",
                "results": [
                    {
                        "extraction_item_id": "XITEM-E2E-MC",
                        "request_sha256": "2" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "B",
                        "normalized_transcription": "B",
                        "capture_status": "captured",
                        "confidence": 0.99,
                    }
                ],
            },
            student_id=STUDENT_ID,
        )
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "e2e:provider:codex:v1",
                "provider": "codex",
                "model_version": "codex-vision-contract-test",
                "prompt_version": "transcription-v1",
                "completed_at": "2026-09-02T16:11:00+08:00",
                "results": [
                    {
                        "extraction_item_id": "XITEM-E2E-MC",
                        "request_sha256": "3" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "B",
                        "normalized_transcription": "B",
                        "capture_status": "captured",
                        "confidence": 0.98,
                    },
                    {
                        "extraction_item_id": "XITEM-E2E-R2",
                        "request_sha256": "4" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "because it was raining",
                        "normalized_transcription": "because it was raining",
                        "capture_status": "captured",
                        "confidence": 0.91,
                    },
                    {
                        "extraction_item_id": "XITEM-E2E-R3",
                        "request_sha256": "5" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "The rain stopped before we left.",
                        "normalized_transcription": "The rain stopped before we left.",
                        "capture_status": "captured",
                        "confidence": 0.83,
                    },
                ],
            },
            student_id=STUDENT_ID,
        )
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "e2e:provider:doubao:v1",
                "provider": "doubao",
                "model_version": "doubao-contract-test",
                "prompt_version": "transcription-v1",
                "completed_at": "2026-09-02T16:11:30+08:00",
                "results": [
                    {
                        "extraction_item_id": "XITEM-E2E-R2",
                        "request_sha256": "6" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "because it was raining",
                        "normalized_transcription": "because it was raining",
                        "capture_status": "captured",
                        "confidence": 0.89,
                    },
                    {
                        "extraction_item_id": "XITEM-E2E-R3",
                        "request_sha256": "7" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "The rain had stopped before we left.",
                        "normalized_transcription": "The rain had stopped before we left.",
                        "capture_status": "captured",
                        "confidence": 0.81,
                    },
                ],
            },
            student_id=STUDENT_ID,
        )

    def test_confirmation_to_projection_selection_and_public_explanation(self) -> None:
        self.assertEqual(migration_status(self.conn)["status"], "ready")
        applied = {
            str(row[0])
            for row in self.conn.execute("SELECT version FROM schema_migrations")
        }
        self.assertTrue({"012", "013", "014"}.issubset(applied))

        baseline = self._fact_counts(self.conn)
        self.assertEqual(baseline["attempts"], 0)
        batch_id = self._create_mixed_batch()
        self._submit_provider_candidates(batch_id)

        review = extraction_review(self.conn, batch_id, student_id=STUDENT_ID)
        self.assertEqual(review["counts"]["total"], 3)
        self.assertTrue(review["standard_answers_hidden"])
        by_id = {item["extraction_item_id"]: item for item in review["items"]}
        self.assertEqual(
            {row["provider"] for row in by_id["XITEM-E2E-MC"]["provider_results"]},
            {"deterministic", "codex"},
        )
        for item_id in ("XITEM-E2E-R2", "XITEM-E2E-R3"):
            self.assertTrue(by_id[item_id]["second_model_required"])
            self.assertEqual(
                {row["provider"] for row in by_id[item_id]["provider_results"]},
                {"codex", "doubao"},
            )
        self.assertEqual(self._fact_counts(self.conn), baseline)

        deterministic_id = next(
            row["provider_result_id"]
            for row in by_id["XITEM-E2E-MC"]["provider_results"]
            if row["provider"] == "deterministic"
        )
        partial = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "e2e:teacher-decision:mc:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher-reviewer",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-E2E-MC",
                        "action": "human_confirmed",
                        "selected_provider_result_id": deterministic_id,
                    }
                ],
            },
            student_id=STUDENT_ID,
        )
        self.assertFalse(partial["review"]["can_commit"])
        with self.assertRaises(ExtractionConflict):
            commit_extraction_batch(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "e2e:commit:v1",
                    "expected_review_version": partial["review"]["review_version"],
                    "actor": "teacher-reviewer",
                },
                student_id=STUDENT_ID,
            )
        self.assertEqual(self._fact_counts(self.conn), baseline)

        partial_by_id = {
            item["extraction_item_id"]: item for item in partial["review"]["items"]
        }
        codex_translation_id = next(
            row["provider_result_id"]
            for row in partial_by_id["XITEM-E2E-R3"]["provider_results"]
            if row["provider"] == "codex"
        )
        completed = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "e2e:teacher-decision:remaining:v1",
                "expected_review_version": partial["review"]["review_version"],
                "actor": "teacher-reviewer",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-E2E-R2",
                        "action": "rejected_alignment",
                        "reason": "Teacher found that the crop belongs to an adjacent response.",
                    },
                    {
                        "extraction_item_id": "XITEM-E2E-R3",
                        "action": "human_corrected",
                        "selected_provider_result_id": codex_translation_id,
                        "confirmed_text": "The rain had stopped before we left.",
                        "evaluation": {
                            "result": "partial",
                            "score": 1,
                            "max_score": 2,
                            "evaluated_by": "teacher",
                        },
                        "reason": "Teacher reconciled the handwriting against the crop.",
                    },
                ],
            },
            student_id=STUDENT_ID,
        )
        self.assertTrue(completed["review"]["can_commit"])
        self.assertEqual(completed["review"]["status"], "ready_to_commit")
        self.assertEqual(self._fact_counts(self.conn), baseline)

        committed = commit_extraction_batch(
            self.conn,
            batch_id,
            {
                "idempotency_key": "e2e:commit:v1",
                "expected_review_version": completed["review"]["review_version"],
                "actor": "teacher-reviewer",
            },
            student_id=STUDENT_ID,
        )
        self.assertEqual(committed["status"], "applied")
        self.assertEqual(committed["attempts_inserted"], 2)
        self.assertEqual(committed["excluded_items"], 1)
        self.assertEqual(committed["readback"]["count"], 2)
        self.assertEqual(
            {row["extraction_item_id"] for row in committed["readback"]["items"]},
            {"XITEM-E2E-MC", "XITEM-E2E-R3"},
        )
        after = self._fact_counts(self.conn)
        self.assertEqual(after["attempts"], 2)
        self.assertEqual(after["evaluations"], 2)
        self.assertEqual(
            int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM extraction_commit_links WHERE extraction_batch_id=?",
                    (batch_id,),
                ).fetchone()[0]
            ),
            2,
        )
        self.assertEqual(
            int(
                self.conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM extraction_commit_links l
                    JOIN extraction_confirmation_decisions d
                      ON d.confirmation_decision_id=l.confirmation_decision_id
                    WHERE l.extraction_batch_id=?
                      AND d.action NOT IN ('human_confirmed','human_corrected','confirmed_blank')
                    """,
                    (batch_id,),
                ).fetchone()[0]
            ),
            0,
        )
        self.assertEqual(
            int(
                self.conn.execute(
                    """
                    SELECT COUNT(DISTINCT ingest_event_id)
                    FROM extraction_commit_links WHERE extraction_batch_id=?
                    """,
                    (batch_id,),
                ).fetchone()[0]
            ),
            1,
        )
        self.assertFalse(
            self.conn.execute(
                """
                SELECT 1 FROM extraction_commit_links
                WHERE extraction_batch_id=? AND extraction_item_id='XITEM-E2E-R2'
                """,
                (batch_id,),
            ).fetchone()
        )

        projection = stage_projection_run(
            self.conn,
            {
                "idempotency_key": "e2e:base-projection:v1",
                "projection_name": "student_overview",
                "student_id": STUDENT_ID,
                "subject_code": "english",
                "data_as_of": DATA_AS_OF,
                "publisher": "local_e2e_test",
                "records": [
                    {
                        "metric_version": "metrics-e2e-v1",
                        "freshness_status": "FRESH",
                        "sample_size": 2,
                        "is_active": True,
                        "session_count": 1,
                        "attempt_count": 2,
                        "scored_attempt_count": 2,
                        "accuracy": 0.5,
                        "review_due_count": 1,
                        "last_activity_at": "2026-09-02T08:07:00Z",
                    }
                ],
            },
            target_config=_synthetic_cassian_target(),
        )
        self.assertEqual(projection["status"], "created")
        self.assertEqual(projection["run"]["status"], "staged")
        self.assertEqual(projection["run"]["status_counts"], {"pending": 1})
        self.assertEqual(
            int(self.conn.execute("SELECT COUNT(*) FROM base_projection_delivery_attempts").fetchone()[0]),
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM base_projection_outbox").fetchone()[0],
            "pending",
        )
        outbox_json = self.conn.execute(
            "SELECT payload_json FROM base_projection_outbox"
        ).fetchone()[0]
        self.assertNotIn("student_answer", outbox_json)
        self.assertNotIn("private://", outbox_json)
        self.assertNotIn("bascnAnonymousE2E001", outbox_json)

        source_sha256 = hashlib.sha256(self.question_bank.read_bytes()).hexdigest()
        explanation_contract = {
            "rubric_version": "rubric-e2e-v1",
            "policy_version": "policy-e2e-v1",
            "schema_version": "schema-e2e-v1",
        }
        selection = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            {
                "student_id": STUDENT_ID,
                "subject_code": "english",
                "training_mode": "transfer",
                "data_as_of": DATA_AS_OF,
                "candidate_question_ids": ["QB-Q1"],
                "candidate_context": {
                    "QB-Q1": {
                        "reason_codes": ["transfer_check"],
                        "knowledge_codes": ["tense"],
                        "evidence_references": [
                            {
                                "entity_type": "session",
                                "entity_id": SESSION_ID,
                                "as_of": DATA_AS_OF,
                            }
                        ],
                        "priority": 1,
                    }
                },
                "target_knowledge_codes": ["tense"],
                "max_questions": 2,
                "max_groups": 1,
                "duplicate_window_days": 30,
                "near_duplicate_threshold": 0.92,
                "allow_exact_retests": False,
                "idempotency_key": "e2e:verified-selection:v1",
                "explanation_contract": explanation_contract,
            },
        )
        self.assertEqual(selection["status"], "created")
        self.assertEqual(selection["manifest"]["status"], "finalized")
        self.assertEqual(selection["manifest"]["selected_group_count"], 1)
        self.assertEqual(selection["manifest"]["selected_question_count"], 2)
        self.assertEqual(len(selection["groups"]), 1)
        self.assertEqual(selection["groups"][0]["expected_question_count"], 2)
        self.assertEqual(selection["groups"][0]["selected_question_count"], 2)
        self.assertEqual(selection["groups"][0]["complete_group"], 1)
        self.assertTrue(
            all(
                item["is_real_question"] == 1
                and item["verification_status"] in {"source_checked", "verified"}
                for item in selection["items"]
            )
        )
        self.assertEqual(hashlib.sha256(self.question_bank.read_bytes()).hexdigest(), source_sha256)

        cached = cache_public_explanation(
            self.conn,
            self.question_bank,
            {
                "question_id": "QB-Q1",
                "explanation_status": "teacher_confirmed",
                "explanation": {
                    "standard_answer": "A",
                    "reasoning": [
                        "The first sentence states that trees cool streets before describing rainwater."
                    ],
                    "common_errors": ["Selecting an effect that the source never states."],
                },
                "created_by": "codex-structured-source",
                "confirmed_by": "teacher-reviewer",
                "explanation_contract": explanation_contract,
            },
        )
        self.assertEqual(cached["status"], "created")
        self.assertEqual(cached["explanation"]["explanation_status"], "teacher_confirmed")
        hit = lookup_public_explanation(
            self.conn,
            self.question_bank,
            "QB-Q1",
            explanation_contract=explanation_contract,
        )
        self.assertEqual(hit["status"], "hit")
        public_payload = json.dumps(hit["explanation"], ensure_ascii=False)
        self.assertNotIn(STUDENT_ID, public_payload)
        self.assertNotIn(SESSION_ID, public_payload)
        self.assertNotIn("private://", public_payload)

        quality = run_quality_checks(self.conn)
        self.assertEqual(
            quality["trust_status"],
            "ready",
            msg={
                row["check_id"]: row["failed_rows"]
                for row in quality["checks"]
                if row["status"] == "fail"
            },
        )
        self.assertEqual(quality["summary"]["failed"], 0)

        self.network_mock.assert_not_called()
        self.urlopen_mock.assert_not_called()
        self.cli_run_mock.assert_not_called()
        self.cli_popen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
