from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.extraction import (
    commit_extraction_batch,
    create_extraction_batch,
    extraction_review,
    submit_human_decisions,
    submit_provider_results,
)
from english_tracker.ingest import import_attempts, import_session
from english_tracker.quality import run_quality_checks


class ExtractionConfirmationSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Anonymous")
        self.conn = connect(database_path(self.data_dir))
        import_session(
            self.conn,
            {
                "event_id": "EVT-SESSION-EXTRACTION",
                "idempotency_key": "session:extraction:v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session": {
                    "session_id": "SES-EXTRACTION",
                    "session_type": "homework",
                    "title": "Anonymous extraction fixture",
                    "started_at": "2026-09-02T10:00:00+08:00",
                },
            },
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _create_batch(self, suffix: str, *, second_model_required: bool = False) -> tuple[str, str]:
        batch_id = f"XBAT-{suffix}"
        item_id = f"XITEM-{suffix}"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO extraction_batches(
                  extraction_batch_id,idempotency_key,request_sha256,contract_version,
                  student_id,subject_code,session_id,title,source_thread,status,
                  expected_item_count,review_version,comparison_policy_version,
                  created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'draft',1,1,'compare-v1',?,?)
                """,
                (
                    batch_id,
                    f"batch:{suffix}:v1",
                    "a" * 64,
                    "extraction-v1",
                    "STU-001",
                    "english",
                    "SES-EXTRACTION",
                    f"Batch {suffix}",
                    "courseware",
                    "2026-09-02T02:00:00Z",
                    "2026-09-02T02:00:00Z",
                ),
            )
            self.conn.execute(
                """
                INSERT INTO extraction_assets(
                  extraction_asset_id,extraction_batch_id,source_uri,sha256,
                  media_type,byte_size,page_number,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    f"XASSET-{suffix}",
                    batch_id,
                    f"private://anonymous/{suffix}.png",
                    "b" * 64,
                    "image/png",
                    100,
                    1,
                    "2026-09-02T02:00:00Z",
                ),
            )
            self.conn.execute(
                """
                INSERT INTO extraction_items(
                  extraction_item_id,extraction_batch_id,extraction_asset_id,ordinal,
                  question_ref,question_type,risk_level,second_model_required,
                  second_model_reason,evidence_locator_json,attempt_template_json,
                  template_sha256,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item_id,
                    batch_id,
                    f"XASSET-{suffix}",
                    1,
                    f"Q-{suffix}",
                    "translation" if second_model_required else "multiple_choice",
                    "R3" if second_model_required else "R0",
                    int(second_model_required),
                    "long_answer" if second_model_required else None,
                    json.dumps({"page": 1, "region": [0, 0, 10, 10]}),
                    json.dumps({"item_id": f"ITEM-{suffix}"}),
                    "c" * 64,
                    "2026-09-02T02:00:00Z",
                ),
            )
            self.conn.execute(
                "UPDATE extraction_batches SET status='ready_to_commit' WHERE extraction_batch_id=?",
                (batch_id,),
            )
        return batch_id, item_id

    def _provider(self, batch_id: str, item_id: str, provider: str) -> str:
        provider_result_id = f"XPROV-{provider}-{item_id}"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO extraction_provider_results(
                  provider_result_id,submission_idempotency_key,idempotency_key,
                  submission_sha256,extraction_batch_id,extraction_item_id,provider,
                  model_version,prompt_version,request_sha256,result_status,
                  raw_transcription,normalized_transcription,capture_status,
                  uncertain_spans_json,candidate_alternatives_json,confidence,
                  evidence_locator_json,response_sha256,completed_at,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'succeeded',?,?,?,'[]','[]',0.95,'{}',?,?,?)
                """,
                (
                    provider_result_id,
                    f"provider-submit:{provider}:{item_id}",
                    f"provider-row:{provider}:{item_id}",
                    "d" * 64,
                    batch_id,
                    item_id,
                    provider,
                    "model-v1",
                    "prompt-v1",
                    "e" * 64,
                    "A",
                    "A",
                    "captured",
                    "f" * 64,
                    "2026-09-02T02:01:00Z",
                    "2026-09-02T02:01:00Z",
                ),
            )
        return provider_result_id

    def _decision(
        self,
        batch_id: str,
        item_id: str,
        action: str,
        *,
        selected_provider_result_id: str | None = None,
    ) -> str:
        decision_id = f"XDEC-{action}-{item_id}"
        committable = action in {"human_confirmed", "human_corrected", "confirmed_blank"}
        confirmed_text = None if action in {"confirmed_blank", "not_captured", "rejected_alignment", "pending_review", "needs_check"} else "A"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO extraction_confirmation_decisions(
                  confirmation_decision_id,submission_idempotency_key,submission_sha256,
                  extraction_batch_id,extraction_item_id,revision_no,review_version,
                  action,confirmed_text,selected_provider_result_id,evaluation_json,
                  actor,reason,decided_at,created_at
                ) VALUES (?,?,?,?,?,1,1,?,?,?,?,?,?,?,?)
                """,
                (
                    decision_id,
                    f"decision-submit:{batch_id}:v1",
                    "1" * 64,
                    batch_id,
                    item_id,
                    action,
                    confirmed_text,
                    selected_provider_result_id,
                    json.dumps({"result": "correct", "score": 1, "max_score": 1}) if committable else None,
                    "teacher",
                    "schema test",
                    "2026-09-02T02:02:00Z",
                    "2026-09-02T02:02:00Z",
                ),
            )
        return decision_id

    def _formal_attempt(self, suffix: str) -> tuple[str, str, str]:
        event_id = f"EVT-XCOMMIT-{suffix}"
        attempt_event_id = f"ATT-EVT-XCOMMIT-{suffix}"
        result = import_attempts(
            self.conn,
            {
                "event_id": event_id,
                "idempotency_key": f"extraction-commit:{suffix}:v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session_id": "SES-EXTRACTION",
                "attempts": [
                    {
                        "event_id": attempt_event_id,
                        "attempted_at": "2026-09-02T10:05:00+08:00",
                        "student_answer": "A",
                        "standard_answer": "A",
                        "answer_capture_status": "captured",
                        "validation_status": "verified",
                        "evaluation": {
                            "result": "correct",
                            "score": 1,
                            "max_score": 1,
                            "evaluated_by": "teacher",
                        },
                        "item": {
                            "domain": "grammar",
                            "item_type": "multiple_choice",
                            "prompt_snapshot": f"Anonymous prompt {suffix}",
                            "answer_snapshot": "A",
                        },
                    }
                ],
            },
        )
        self.assertEqual(result["attempts_inserted"], 1)
        attempt = self.conn.execute(
            "SELECT attempt_id FROM attempts WHERE event_id=?", (attempt_event_id,)
        ).fetchone()["attempt_id"]
        evaluation = self.conn.execute(
            "SELECT evaluation_id FROM evaluations WHERE attempt_id=? AND is_current=1",
            (attempt,),
        ).fetchone()["evaluation_id"]
        return event_id, attempt, evaluation

    def _link_and_commit(self, batch_id: str, item_id: str, decision_id: str, suffix: str) -> None:
        ingest_event_id, attempt_id, evaluation_id = self._formal_attempt(suffix)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO extraction_commit_links(
                  extraction_batch_id,extraction_item_id,confirmation_decision_id,
                  attempt_id,evaluation_id,ingest_event_id,committed_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    batch_id,
                    item_id,
                    decision_id,
                    attempt_id,
                    evaluation_id,
                    ingest_event_id,
                    "2026-09-02T02:03:00Z",
                ),
            )
            self.conn.execute(
                """
                UPDATE extraction_batches
                SET status='committed',commit_idempotency_key=?,commit_request_sha256=?,
                    committed_ingest_event_id=?,committed_at=?,updated_at=?
                WHERE extraction_batch_id=?
                """,
                (
                    f"commit:{batch_id}:v1",
                    "2" * 64,
                    ingest_event_id,
                    "2026-09-02T02:03:00Z",
                    "2026-09-02T02:03:00Z",
                    batch_id,
                ),
            )

    def _empty_commit_event(self, suffix: str) -> str:
        event_id = f"EVT-XEMPTY-{suffix}"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO ingest_events(
                  ingest_event_id,idempotency_key,event_type,source_thread,
                  payload_sha256,payload_json,status,rows_total,rows_inserted,
                  rows_skipped,imported_at
                ) VALUES (?,?, 'extraction_commit','courseware',?, '{}','applied',0,0,0,?)
                """,
                (event_id, f"empty:{suffix}:v1", "3" * 64, "2026-09-02T02:03:00Z"),
            )
        return event_id

    def test_pending_review_blocks_commit_without_learning_fact(self) -> None:
        batch_id, item_id = self._create_batch("PENDING")
        self._decision(batch_id, item_id, "pending_review")
        ingest_event_id = self._empty_commit_event("PENDING")
        before = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "pending_review or needs_check"):
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE extraction_batches
                    SET status='committed',commit_idempotency_key='commit:pending',
                        commit_request_sha256=?,committed_ingest_event_id=?,committed_at=?
                    WHERE extraction_batch_id=?
                    """,
                    ("4" * 64, ingest_event_id, "2026-09-02T02:04:00Z", batch_id),
                )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], before)
        status = self.conn.execute(
            "SELECT status FROM extraction_batches WHERE extraction_batch_id=?", (batch_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "ready_to_commit")

    def test_not_captured_commits_without_attempt_or_review_fact(self) -> None:
        batch_id, item_id = self._create_batch("NO-CAPTURE")
        self._decision(batch_id, item_id, "not_captured")
        ingest_event_id = self._empty_commit_event("NO-CAPTURE")
        with self.conn:
            self.conn.execute(
                """
                UPDATE extraction_batches
                SET status='committed',commit_idempotency_key='commit:no-capture',
                    commit_request_sha256=?,committed_ingest_event_id=?,committed_at=?
                WHERE extraction_batch_id=?
                """,
                ("5" * 64, ingest_event_id, "2026-09-02T02:04:00Z", batch_id),
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM extraction_commit_links").fetchone()[0], 0)
        self.assertEqual(run_quality_checks(self.conn)["trust_status"], "ready")

    def test_rejected_alignment_cannot_link_to_a_formal_attempt(self) -> None:
        batch_id, item_id = self._create_batch("ALIGNMENT")
        decision_id = self._decision(batch_id, item_id, "rejected_alignment")
        ingest_event_id, attempt_id, evaluation_id = self._formal_attempt("ALIGNMENT")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "confirmation or learning-fact ownership"):
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO extraction_commit_links(
                      extraction_batch_id,extraction_item_id,confirmation_decision_id,
                      attempt_id,evaluation_id,ingest_event_id,committed_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id,
                        item_id,
                        decision_id,
                        attempt_id,
                        evaluation_id,
                        ingest_event_id,
                        "2026-09-02T02:03:00Z",
                    ),
                )

    def test_second_model_and_commit_link_gate_then_atomic_fact_shape(self) -> None:
        batch_id, item_id = self._create_batch("DUAL", second_model_required=True)
        first_result = self._provider(batch_id, item_id, "codex")
        decision_id = self._decision(
            batch_id,
            item_id,
            "human_confirmed",
            selected_provider_result_id=first_result,
        )
        ingest_event_id, attempt_id, evaluation_id = self._formal_attempt("DUAL")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO extraction_commit_links(
                  extraction_batch_id,extraction_item_id,confirmation_decision_id,
                  attempt_id,evaluation_id,ingest_event_id,committed_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    batch_id,
                    item_id,
                    decision_id,
                    attempt_id,
                    evaluation_id,
                    ingest_event_id,
                    "2026-09-02T02:03:00Z",
                ),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Codex and Doubao"):
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE extraction_batches
                    SET status='committed',commit_idempotency_key='commit:dual',
                        commit_request_sha256=?,committed_ingest_event_id=?,committed_at=?
                    WHERE extraction_batch_id=?
                    """,
                    ("6" * 64, ingest_event_id, "2026-09-02T02:04:00Z", batch_id),
                )
        self._provider(batch_id, item_id, "deterministic")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Codex and Doubao"):
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE extraction_batches
                    SET status='committed',commit_idempotency_key='commit:dual',
                        commit_request_sha256=?,committed_ingest_event_id=?,committed_at=?
                    WHERE extraction_batch_id=?
                    """,
                    ("6" * 64, ingest_event_id, "2026-09-02T02:04:00Z", batch_id),
                )
        self._provider(batch_id, item_id, "doubao")
        with self.conn:
            self.conn.execute(
                """
                UPDATE extraction_batches
                SET status='committed',commit_idempotency_key='commit:dual',
                    commit_request_sha256=?,committed_ingest_event_id=?,committed_at=?
                WHERE extraction_batch_id=?
                """,
                ("6" * 64, ingest_event_id, "2026-09-02T02:04:00Z", batch_id),
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM extraction_commit_links").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 1)
        self.assertEqual(run_quality_checks(self.conn)["trust_status"], "ready")

    def test_provider_results_and_human_decisions_are_append_only(self) -> None:
        batch_id, item_id = self._create_batch("APPEND")
        provider_result_id = self._provider(batch_id, item_id, "codex")
        decision_id = self._decision(
            batch_id,
            item_id,
            "human_confirmed",
            selected_provider_result_id=provider_result_id,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.conn:
                self.conn.execute(
                    "UPDATE extraction_provider_results SET raw_transcription='B' WHERE provider_result_id=?",
                    (provider_result_id,),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.conn:
                self.conn.execute(
                    "UPDATE extraction_confirmation_decisions SET confirmed_text='B' WHERE confirmation_decision_id=?",
                    (decision_id,),
                )

    def test_service_closes_full_review_then_atomically_creates_formal_facts(self) -> None:
        created = create_extraction_batch(
            self.conn,
            {
                "idempotency_key": "service-batch:v1",
                "student_id": "STU-001",
                "subject_code": "english",
                "session_id": "SES-EXTRACTION",
                "title": "Mixed anonymous sample",
                "source_thread": "courseware",
                "source_images": [
                    {
                        "extraction_asset_id": "XASSET-SERVICE",
                        "source_uri": "private://anonymous/service.png",
                        "sha256": "7" * 64,
                        "media_type": "image/png",
                        "byte_size": 200,
                        "page_number": 1,
                    }
                ],
                "items": [
                    {
                        "extraction_item_id": "XITEM-SERVICE-R0",
                        "extraction_asset_id": "XASSET-SERVICE",
                        "ordinal": 1,
                        "question_ref": "Q-R0",
                        "question_type": "multiple_choice",
                        "risk_level": "R0",
                        "second_model_required": False,
                        "evidence_locator": {"page": 1, "region": [0, 0, 20, 20]},
                        "attempt_template": {
                            "attempted_at": "2026-09-02T10:10:00+08:00",
                            "standard_answer": "A",
                            "response_mode": "recognition",
                            "item": {
                                "domain": "grammar",
                                "item_type": "multiple_choice",
                                "prompt_snapshot": "Anonymous objective item",
                                "answer_snapshot": "A",
                            },
                            "grading_contract": {
                                "mode": "deterministic_exact",
                                "acceptable_answers": ["A"],
                                "max_score": 1,
                            },
                        },
                    },
                    {
                        "extraction_item_id": "XITEM-SERVICE-R3",
                        "extraction_asset_id": "XASSET-SERVICE",
                        "ordinal": 2,
                        "question_ref": "Q-R3",
                        "question_type": "translation",
                        "risk_level": "R3",
                        "second_model_required": True,
                        "second_model_reason": "long_answer",
                        "evidence_locator": {"page": 1, "region": [0, 20, 100, 80]},
                        "attempt_template": {
                            "attempted_at": "2026-09-02T10:11:00+08:00",
                            "standard_answer": "A source-checked reference",
                            "response_mode": "production",
                            "item": {
                                "domain": "translation",
                                "item_type": "translation",
                                "prompt_snapshot": "Anonymous long-answer item",
                                "answer_snapshot": "A source-checked reference",
                            },
                            "grading_contract": {"mode": "teacher_rubric"},
                        },
                    },
                ],
            },
        )
        batch_id = created["batch"]["extraction_batch_id"]

        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "service-provider-codex-r0:v1",
                "provider": "codex",
                "model_version": "codex-v1",
                "prompt_version": "transcription-v1",
                "completed_at": "2026-09-02T02:01:00Z",
                "results": [
                    {
                        "extraction_item_id": "XITEM-SERVICE-R0",
                        "request_sha256": "8" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "A",
                        "normalized_transcription": "A",
                        "capture_status": "captured",
                        "confidence": 0.99,
                        "evidence_locator": {"page": 1},
                        "response_sha256": "9" * 64,
                    }
                ],
            },
            student_id="STU-001",
        )
        for provider, raw, hash_char in (
            ("codex", "Long candidate one", "a"),
            ("doubao", "Long candidate two", "b"),
        ):
            submit_provider_results(
                self.conn,
                batch_id,
                {
                    "idempotency_key": f"service-provider-{provider}-r3:v1",
                    "provider": provider,
                    "model_version": f"{provider}-v1",
                    "prompt_version": "transcription-v1",
                    "completed_at": "2026-09-02T02:01:30Z",
                    "results": [
                        {
                            "extraction_item_id": "XITEM-SERVICE-R3",
                            "request_sha256": hash_char * 64,
                            "result_status": "succeeded",
                            "raw_transcription": raw,
                            "normalized_transcription": raw,
                            "capture_status": "captured",
                            "confidence": 0.9,
                            "evidence_locator": {"page": 1},
                            "response_sha256": hash_char * 64,
                        }
                    ],
                },
                student_id="STU-001",
            )

        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        self.assertEqual(review["counts"]["total"], 2)
        self.assertEqual(review["counts"]["pending"], 2)
        self.assertTrue(review["standard_answers_hidden"])
        r0 = next(item for item in review["items"] if item["extraction_item_id"] == "XITEM-SERVICE-R0")
        self.assertEqual(r0["review_group"], "ordinary")
        r0_provider_result = r0["provider_results"][0]["provider_result_id"]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

        decided = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "service-decisions:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-SERVICE-R0",
                        "action": "human_confirmed",
                        "selected_provider_result_id": r0_provider_result,
                    },
                    {
                        "extraction_item_id": "XITEM-SERVICE-R3",
                        "action": "human_corrected",
                        "confirmed_text": "Teacher-confirmed long answer",
                        "evaluation": {
                            "result": "correct",
                            "score": 4,
                            "max_score": 4,
                            "evaluated_by": "teacher",
                        },
                    },
                ],
            },
            student_id="STU-001",
        )
        self.assertTrue(decided["review"]["can_commit"])
        self.assertEqual(decided["review"]["status"], "ready_to_commit")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

        committed = commit_extraction_batch(
            self.conn,
            batch_id,
            {
                "idempotency_key": "service-commit:v1",
                "expected_review_version": decided["review"]["review_version"],
                "actor": "teacher",
            },
            student_id="STU-001",
        )
        self.assertEqual(committed["attempts_inserted"], 2)
        self.assertEqual(committed["readback"]["count"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM extraction_commit_links").fetchone()[0], 2)
        self.assertEqual(run_quality_checks(self.conn)["trust_status"], "ready")

        replay = commit_extraction_batch(
            self.conn,
            batch_id,
            {
                "idempotency_key": "service-commit:v1",
                "expected_review_version": decided["review"]["review_version"],
                "actor": "teacher",
            },
            student_id="STU-001",
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
