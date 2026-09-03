from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.extraction import (
    ExtractionConflict,
    commit_extraction_batch,
    create_extraction_batch,
    extraction_batch_detail,
    extraction_review,
    submit_human_decisions,
    submit_provider_results,
)
from english_tracker.ingest import import_attempts, import_session


class ExtractionConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.db_name = patch.dict(
            "os.environ", {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"}
        )
        self.db_name.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="One")
        initialize_database(self.data_dir, student_id="STU-002", display_name="Two")
        self.conn = connect(database_path(self.data_dir))
        for number, student_id in ((1, "STU-001"), (2, "STU-002")):
            import_session(
                self.conn,
                {
                    "event_id": f"EVT-XTR-SESSION-{number}",
                    "idempotency_key": f"xtr-session:{number}:v1",
                    "source_thread": "courseware",
                    "student_id": student_id,
                    "session": {
                        "session_id": f"SES-XTR-{number}",
                        "session_type": "homework",
                        "title": f"Anonymous extraction {number}",
                        "started_at": "2026-09-02T09:00:00+08:00",
                    },
                },
            )

    def tearDown(self) -> None:
        self.conn.close()
        self.db_name.stop()
        self.temp.cleanup()

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "attempts",
                "evaluations",
                "review_state",
                "review_tasks",
                "mastery_snapshots",
            )
        }

    def batch_payload(self, *, key: str = "xtr:e2e:v1", student: str = "STU-001") -> dict:
        session = "SES-XTR-1" if student == "STU-001" else "SES-XTR-2"
        return {
            "idempotency_key": key,
            "student_id": student,
            "subject_code": "english",
            "session_id": session,
            "source_thread": "courseware",
            "title": "Anonymous mixed answer sheet",
            "source_images": [
                {
                    "extraction_asset_id": "IMG-E2E-1" if student == "STU-001" else "IMG-E2E-2",
                    "source_uri": "private://anonymous/answer-page-1",
                    "sha256": "1" * 64,
                    "media_type": "image/png",
                    "byte_size": 128,
                    "page_number": 1,
                }
            ],
            "items": [
                {
                    "extraction_item_id": "XITEM-MC-1" if student == "STU-001" else "XITEM-MC-2",
                    "extraction_asset_id": "IMG-E2E-1" if student == "STU-001" else "IMG-E2E-2",
                    "ordinal": 1,
                    "question_ref": "Q1",
                    "question_type": "multiple_choice",
                    "risk_level": "R0",
                    "evidence_locator": {"page": 1, "box": [10, 10, 40, 30]},
                    "attempt_template": {
                        "attempted_at": "2026-09-02T09:05:00+08:00",
                        "standard_answer": "A",
                        "response_mode": "recognition",
                        "validation_status": "source_checked",
                        "item": {
                            "subject_code": "english",
                            "domain": "reading",
                            "item_type": "multiple_choice",
                            "prompt_snapshot": "Anonymous question one",
                            "answer_snapshot": "A",
                        },
                        "transcription_comparison": {
                            "trim": True,
                            "case_sensitive": False,
                        },
                        "grading_contract": {
                            "mode": "deterministic_exact",
                            "acceptable_answers": ["A"],
                            "case_sensitive": False,
                            "max_score": 1,
                        },
                    },
                },
                {
                    "extraction_item_id": "XITEM-WRITE-1" if student == "STU-001" else "XITEM-WRITE-2",
                    "extraction_asset_id": "IMG-E2E-1" if student == "STU-001" else "IMG-E2E-2",
                    "ordinal": 2,
                    "question_ref": "Q2",
                    "question_type": "translation",
                    "risk_level": "R3",
                    "second_model_reason": "long_answer_calibration",
                    "evidence_locator": {"page": 1, "box": [10, 40, 180, 90]},
                    "attempt_template": {
                        "attempted_at": "2026-09-02T09:06:00+08:00",
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
                {
                    "extraction_item_id": "XITEM-MISS-1" if student == "STU-001" else "XITEM-MISS-2",
                    "extraction_asset_id": "IMG-E2E-1" if student == "STU-001" else "IMG-E2E-2",
                    "ordinal": 3,
                    "question_ref": "Q3",
                    "question_type": "cloze",
                    "risk_level": "R1",
                    "evidence_locator": {"page": 1, "box": [10, 100, 60, 120]},
                    "attempt_template": {
                        "attempted_at": "2026-09-02T09:07:00+08:00",
                        "standard_answer": "went",
                        "response_mode": "production",
                        "item": {
                            "subject_code": "english",
                            "domain": "grammar",
                            "item_type": "cloze",
                            "prompt_snapshot": "Anonymous cloze prompt",
                            "answer_snapshot": "went",
                        },
                        "grading_contract": {
                            "mode": "deterministic_exact",
                            "acceptable_answers": ["went"],
                        },
                    },
                },
            ],
        }

    def create_batch(self) -> str:
        created = create_extraction_batch(self.conn, self.batch_payload())
        return created["batch"]["extraction_batch_id"]

    def test_cold_start_r1_and_all_r2_r3_items_cannot_disable_the_second_model_gate(self) -> None:
        for index, risk_level in ((2, "R1"), (1, "R2"), (1, "R3")):
            payload = self.batch_payload(key=f"xtr:cannot-disable:{risk_level}:v1")
            payload["items"][index]["risk_level"] = risk_level
            payload["items"][index]["second_model_required"] = False
            with self.subTest(risk_level=risk_level):
                with self.assertRaisesRegex(ValueError, "cannot disable"):
                    create_extraction_batch(self.conn, payload)

    def test_provider_identity_is_closed_to_the_audited_provider_set(self) -> None:
        batch_id = self.create_batch()
        with self.assertRaisesRegex(ValueError, "provider must be one of"):
            submit_provider_results(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "provider:alias:v1",
                    "provider": "unverified-alias",
                    "model_version": "unknown",
                    "prompt_version": "transcription-v1",
                    "completed_at": "2026-09-02T09:10:00+08:00",
                    "results": [],
                },
                student_id="STU-001",
            )

    def submit_candidates(self, batch_id: str) -> None:
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "provider:deterministic:q1:v1",
                "provider": "deterministic",
                "model_version": "answer-grid-v1",
                "prompt_version": "grid-v1",
                "completed_at": "2026-09-02T09:10:00+08:00",
                "results": [
                    {
                        "extraction_item_id": "XITEM-MC-1",
                        "request_sha256": "2" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "A",
                        "normalized_transcription": "A",
                        "capture_status": "captured",
                        "confidence": 0.99,
                    }
                ],
            },
            student_id="STU-001",
        )
        for provider, text, digest in (
            ("codex", "I went home because it rained.", "3" * 64),
            ("doubao", "I went home since it rained.", "4" * 64),
        ):
            submit_provider_results(
                self.conn,
                batch_id,
                {
                    "idempotency_key": f"provider:{provider}:q2:v1",
                    "provider": provider,
                    "model_version": f"{provider}-vision-test",
                    "prompt_version": "transcription-v1",
                    "completed_at": "2026-09-02T09:11:00+08:00",
                    "results": [
                        {
                            "extraction_item_id": "XITEM-WRITE-1",
                            "request_sha256": digest,
                            "result_status": "succeeded",
                            "raw_transcription": text,
                            "normalized_transcription": text,
                            "capture_status": "captured",
                            "confidence": 0.82,
                        }
                    ],
                },
                student_id="STU-001",
            )

    def test_full_batch_gate_and_atomic_commit(self) -> None:
        baseline = self.counts()
        batch_id = self.create_batch()
        self.submit_candidates(batch_id)
        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        self.assertEqual(review["counts"]["total"], 3)
        self.assertEqual(
            {item["question_ref"] for item in review["items"]}, {"Q1", "Q2", "Q3"}
        )
        writing = next(item for item in review["items"] if item["question_ref"] == "Q2")
        self.assertEqual(writing["comparison"]["classification"], "content_conflict")
        self.assertTrue(writing["comparison"]["diff_spans"])
        self.assertTrue(review["standard_answers_hidden"])
        self.assertEqual(self.counts(), baseline)

        partial = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:q1:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher-test",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-MC-1",
                        "action": "human_confirmed",
                        "selected_provider_result_id": review["items"][0]["comparison"][
                            "prefill_provider_result_id"
                        ],
                    }
                ],
            },
            student_id="STU-001",
        )
        self.assertFalse(partial["review"]["can_commit"])
        with self.assertRaises(ExtractionConflict):
            commit_extraction_batch(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "commit:e2e:v1",
                    "expected_review_version": partial["review"]["review_version"],
                    "actor": "teacher-test",
                },
                student_id="STU-001",
            )
        self.assertEqual(self.counts(), baseline)

        completed = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:q2-q3:v1",
                "expected_review_version": partial["review"]["review_version"],
                "actor": "teacher-test",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-WRITE-1",
                        "action": "human_corrected",
                        "selected_provider_result_id": writing["provider_results"][0][
                            "provider_result_id"
                        ],
                        "confirmed_text": "I went home because it was raining.",
                        "evaluation": {
                            "result": "wrong",
                            "score": 1,
                            "max_score": 2,
                            "evaluated_by": "teacher",
                        },
                        "reason": "Teacher checked the handwriting against the crop.",
                    },
                    {
                        "extraction_item_id": "XITEM-MISS-1",
                        "action": "not_captured",
                        "reason": "The answer region is cut off.",
                    },
                ],
            },
            student_id="STU-001",
        )
        self.assertTrue(completed["review"]["can_commit"])
        self.assertEqual(completed["review"]["status"], "ready_to_commit")
        provider_before = [
            tuple(row)
            for row in self.conn.execute(
                """
                SELECT provider_result_id,raw_transcription,normalized_transcription
                FROM extraction_provider_results ORDER BY provider_result_id
                """
            )
        ]
        committed = commit_extraction_batch(
            self.conn,
            batch_id,
            {
                "idempotency_key": "commit:e2e:v1",
                "expected_review_version": completed["review"]["review_version"],
                "actor": "teacher-test",
            },
            student_id="STU-001",
        )
        self.assertEqual(committed["attempts_inserted"], 2)
        self.assertEqual(committed["excluded_items"], 1)
        self.assertEqual(committed["readback"]["count"], 2)
        after = self.counts()
        self.assertEqual(after["attempts"], baseline["attempts"] + 2)
        self.assertEqual(after["evaluations"], baseline["evaluations"] + 2)
        self.assertEqual(after["review_tasks"], baseline["review_tasks"] + 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM extraction_commit_links WHERE extraction_item_id='XITEM-MISS-1'"
            ).fetchone()[0],
            0,
        )
        provider_after = [
            tuple(row)
            for row in self.conn.execute(
                """
                SELECT provider_result_id,raw_transcription,normalized_transcription
                FROM extraction_provider_results ORDER BY provider_result_id
                """
            )
        ]
        self.assertEqual(provider_after, provider_before)

        replay = commit_extraction_batch(
            self.conn,
            batch_id,
            {
                "idempotency_key": "commit:e2e:v1",
                "expected_review_version": completed["review"]["review_version"],
                "actor": "teacher-test",
            },
            student_id="STU-001",
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(self.counts(), after)
        with self.assertRaises(ExtractionConflict):
            commit_extraction_batch(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "commit:e2e:v1",
                    "expected_review_version": completed["review"]["review_version"],
                    "actor": "different-actor",
                },
                student_id="STU-001",
            )

    def test_high_risk_one_provider_cannot_be_committed(self) -> None:
        batch_id = self.create_batch()
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "provider:codex:only:v1",
                "provider": "codex",
                "model_version": "codex-test",
                "prompt_version": "transcription-v1",
                "results": [
                    {
                        "extraction_item_id": "XITEM-WRITE-1",
                        "request_sha256": "5" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "One candidate only.",
                        "normalized_transcription": "One candidate only.",
                        "capture_status": "captured",
                    }
                ],
            },
            student_id="STU-001",
        )
        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        result = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:one-provider:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher-test",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-WRITE-1",
                        "action": "human_corrected",
                        "confirmed_text": "Teacher transcription.",
                        "evaluation": {"result": "correct", "score": 2, "max_score": 2},
                    }
                ],
            },
            student_id="STU-001",
        )
        self.assertFalse(result["review"]["can_commit"])
        self.assertEqual(result["review"]["status"], "pending_review")

    def test_latest_required_provider_result_controls_readiness_and_commit(self) -> None:
        payload = self.batch_payload(key="xtr:latest-provider-result:v1")
        payload["items"] = [payload["items"][1]]
        batch_id = create_extraction_batch(self.conn, payload)["batch"][
            "extraction_batch_id"
        ]

        def submit(
            provider: str,
            key: str,
            status: str,
            now: str,
            *,
            text: str | None = None,
            provider_result_id: str | None = None,
        ) -> dict:
            result = {
                "extraction_item_id": "XITEM-WRITE-1",
                "request_sha256": ("3" if provider == "codex" else "4") * 64,
                "result_status": status,
            }
            if provider_result_id is not None:
                result["provider_result_id"] = provider_result_id
            if status == "succeeded":
                result.update(
                    {
                        "raw_transcription": text,
                        "normalized_transcription": text,
                        "capture_status": "captured",
                    }
                )
            else:
                result["error_summary"] = "Provider failed after an earlier success."
            with patch("english_tracker.extraction.utc_now", return_value=now):
                return submit_provider_results(
                    self.conn,
                    batch_id,
                    {
                        "idempotency_key": key,
                        "provider": provider,
                        "model_version": f"{provider}-test",
                        "prompt_version": "transcription-v1",
                        "completed_at": now,
                        "results": [result],
                    },
                    student_id="STU-001",
                )

        submit(
            "codex",
            "provider:latest:codex:success-1",
            "succeeded",
            "2026-09-02T01:00:00Z",
            text="Initial candidate.",
            provider_result_id="XPR-ZZZ-OLDER-CODEX-SUCCESS",
        )
        submit(
            "doubao",
            "provider:latest:doubao:success-1",
            "succeeded",
            "2026-09-02T01:00:00Z",
            text="Initial candidate.",
        )
        ready_review = extraction_review(self.conn, batch_id, student_id="STU-001")
        self.assertTrue(ready_review["items"][0]["comparison"]["second_model_ready"])

        submit(
            "codex",
            "provider:latest:codex:failure-2",
            "failed",
            "2026-09-02T01:00:00Z",
            provider_result_id="XPR-AAA-NEWER-CODEX-FAILURE",
        )
        failed_review = extraction_review(self.conn, batch_id, student_id="STU-001")
        comparison = failed_review["items"][0]["comparison"]
        self.assertFalse(comparison["second_model_ready"])
        self.assertEqual(comparison["successful_provider_count"], 1)
        self.assertEqual(comparison["classification"], "blocked_second_model")

        decided = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:latest:blocked:v1",
                "expected_review_version": failed_review["review_version"],
                "actor": "teacher-test",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-WRITE-1",
                        "action": "human_corrected",
                        "confirmed_text": "Teacher-confirmed answer.",
                        "evaluation": {
                            "result": "correct",
                            "score": 2,
                            "max_score": 2,
                            "evaluated_by": "teacher",
                        },
                    }
                ],
            },
            student_id="STU-001",
        )
        self.assertFalse(decided["review"]["can_commit"])
        self.assertEqual(decided["review"]["status"], "pending_review")
        with self.assertRaisesRegex(ExtractionConflict, "Codex and Doubao"):
            commit_extraction_batch(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "commit:latest:v1",
                    "expected_review_version": decided["review"]["review_version"],
                    "actor": "teacher-test",
                },
                student_id="STU-001",
            )

        reopened = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:latest:reopen:v1",
                "expected_review_version": decided["review"]["review_version"],
                "actor": "teacher-test",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-WRITE-1",
                        "action": "needs_check",
                        "reason": "Wait for the current Codex retry.",
                    }
                ],
            },
            student_id="STU-001",
        )
        submit(
            "codex",
            "provider:latest:codex:success-3",
            "succeeded",
            "2026-09-02T01:00:00Z",
            text="Retry candidate.",
            provider_result_id="XPR-000-NEWEST-CODEX-SUCCESS",
        )
        restored_review = extraction_review(
            self.conn, batch_id, student_id="STU-001"
        )
        self.assertGreater(restored_review["review_version"], reopened["review"]["review_version"])
        self.assertTrue(
            restored_review["items"][0]["comparison"]["second_model_ready"]
        )
        restored = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:latest:confirmed:v1",
                "expected_review_version": restored_review["review_version"],
                "actor": "teacher-test",
                "decisions": [
                    {
                        "extraction_item_id": "XITEM-WRITE-1",
                        "action": "human_corrected",
                        "confirmed_text": "Teacher-confirmed answer.",
                        "evaluation": {
                            "result": "correct",
                            "score": 2,
                            "max_score": 2,
                            "evaluated_by": "teacher",
                        },
                    }
                ],
            },
            student_id="STU-001",
        )
        self.assertTrue(restored["review"]["can_commit"])
        committed = commit_extraction_batch(
            self.conn,
            batch_id,
            {
                "idempotency_key": "commit:latest:v1",
                "expected_review_version": restored["review"]["review_version"],
                "actor": "teacher-test",
            },
            student_id="STU-001",
        )
        self.assertEqual(committed["attempts_inserted"], 1)

    def test_same_key_concurrent_create_provider_and_decision_are_idempotent(self) -> None:
        db_path = database_path(self.data_dir)

        def concurrently(call):
            barrier = Barrier(2)

            def worker():
                conn = connect(db_path)
                try:
                    barrier.wait(timeout=5)
                    return call(conn)
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                return [future.result() for future in (pool.submit(worker), pool.submit(worker))]

        create_payload = self.batch_payload(key="xtr:concurrent:v1")
        created = concurrently(
            lambda conn: create_extraction_batch(conn, create_payload)
        )
        self.assertEqual(
            sorted(result["status"] for result in created), ["created", "duplicate"]
        )
        batch_id = created[0]["batch"]["extraction_batch_id"]
        provider_payload = {
            "idempotency_key": "provider:concurrent:v1",
            "provider": "deterministic",
            "model_version": "grid-v1",
            "prompt_version": "grid-v1",
            "results": [
                {
                    "extraction_item_id": "XITEM-MC-1",
                    "request_sha256": "8" * 64,
                    "result_status": "succeeded",
                    "raw_transcription": "A",
                    "normalized_transcription": "A",
                    "capture_status": "captured",
                }
            ],
        }
        submitted = concurrently(
            lambda conn: submit_provider_results(
                conn,
                batch_id,
                provider_payload,
                student_id="STU-001",
            )
        )
        self.assertEqual(
            sorted(result["status"] for result in submitted), ["created", "duplicate"]
        )
        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        selected_id = next(
            item for item in review["items"] if item["extraction_item_id"] == "XITEM-MC-1"
        )["comparison"]["prefill_provider_result_id"]
        decision_payload = {
            "idempotency_key": "decision:concurrent:v1",
            "expected_review_version": review["review_version"],
            "actor": "teacher-test",
            "decisions": [
                {
                    "extraction_item_id": "XITEM-MC-1",
                    "action": "human_confirmed",
                    "selected_provider_result_id": selected_id,
                }
            ],
        }
        decisions = concurrently(
            lambda conn: submit_human_decisions(
                conn,
                batch_id,
                decision_payload,
                student_id="STU-001",
            )
        )
        self.assertEqual(
            sorted(result["status"] for result in decisions), ["applied", "duplicate"]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM extraction_batches WHERE idempotency_key=?",
                ("xtr:concurrent:v1",),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM extraction_provider_results WHERE submission_idempotency_key=?",
                ("provider:concurrent:v1",),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM extraction_confirmation_decisions WHERE submission_idempotency_key=?",
                ("decision:concurrent:v1",),
            ).fetchone()[0],
            1,
        )

    def test_nested_busy_snapshot_is_normalized_without_closing_caller_transaction(self) -> None:
        db_path = database_path(self.data_dir)
        with closing(connect(db_path)) as stale, closing(connect(db_path)) as writer:
            stale.execute("BEGIN")
            stale.execute("SELECT COUNT(*) FROM extraction_batches").fetchone()
            create_extraction_batch(
                writer,
                self.batch_payload(key="xtr:nested-busy:writer:v1", student="STU-002"),
            )

            with self.assertRaisesRegex(
                ExtractionConflict, "Concurrent extraction write"
            ):
                create_extraction_batch(
                    stale,
                    self.batch_payload(key="xtr:nested-busy:stale:v1"),
                )
            self.assertTrue(stale.in_transaction)
            stale.rollback()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM extraction_batches WHERE idempotency_key=?",
                ("xtr:nested-busy:stale:v1",),
            ).fetchone()[0],
            0,
        )

    def test_nested_commit_preserves_caller_transaction_ownership_on_success_and_failure(self) -> None:
        payload = self.batch_payload(key="xtr:nested-commit:v1")
        payload["items"] = [payload["items"][0]]
        batch_id = create_extraction_batch(self.conn, payload)["batch"][
            "extraction_batch_id"
        ]
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "provider:nested-commit:v1",
                "provider": "deterministic",
                "model_version": "grid-v1",
                "prompt_version": "grid-v1",
                "results": [
                    {
                        "extraction_item_id": "XITEM-MC-1",
                        "request_sha256": "9" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "A",
                        "normalized_transcription": "A",
                        "capture_status": "captured",
                    }
                ],
            },
            student_id="STU-001",
        )
        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        decided = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:nested-commit:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher-test",
                "default_action": "accept_prefill",
            },
            student_id="STU-001",
        )
        baseline = self.counts()
        commit_payload = {
            "idempotency_key": "commit:nested-commit:v1",
            "expected_review_version": decided["review"]["review_version"],
            "actor": "teacher-test",
        }
        db_path = database_path(self.data_dir)

        with closing(connect(db_path)) as nested:
            nested.execute("BEGIN IMMEDIATE")
            nested.execute(
                "UPDATE students SET display_name='Outer success pending' WHERE student_id='STU-002'"
            )
            committed = commit_extraction_batch(
                nested,
                batch_id,
                commit_payload,
                student_id="STU-001",
            )
            self.assertEqual(committed["attempts_inserted"], 1)
            self.assertTrue(nested.in_transaction)
            nested.rollback()

        self.assertEqual(self.counts(), baseline)
        self.assertEqual(
            self.conn.execute(
                "SELECT display_name FROM students WHERE student_id='STU-002'"
            ).fetchone()[0],
            "Two",
        )
        self.assertEqual(
            extraction_batch_detail(self.conn, batch_id, student_id="STU-001")[
                "status"
            ],
            "ready_to_commit",
        )

        real_import = import_attempts

        def fail_after_ingest(conn, ingest_payload, **kwargs):
            real_import(conn, ingest_payload, **kwargs)
            raise RuntimeError("injected nested failure after fact insertion")

        with closing(connect(db_path)) as nested:
            nested.execute("BEGIN IMMEDIATE")
            nested.execute(
                "UPDATE students SET display_name='Outer failure survives' WHERE student_id='STU-002'"
            )
            with patch(
                "english_tracker.extraction.import_attempts",
                side_effect=fail_after_ingest,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected nested failure"):
                    commit_extraction_batch(
                        nested,
                        batch_id,
                        commit_payload,
                        student_id="STU-001",
                    )
            self.assertTrue(nested.in_transaction)
            self.assertEqual(
                nested.execute(
                    "SELECT display_name FROM students WHERE student_id='STU-002'"
                ).fetchone()[0],
                "Outer failure survives",
            )
            self.assertEqual(
                nested.execute(
                    "SELECT COUNT(*) FROM extraction_commit_links WHERE extraction_batch_id=?",
                    (batch_id,),
                ).fetchone()[0],
                0,
            )
            nested.commit()

        self.assertEqual(self.counts(), baseline)
        self.assertEqual(
            self.conn.execute(
                "SELECT display_name FROM students WHERE student_id='STU-002'"
            ).fetchone()[0],
            "Outer failure survives",
        )
        self.assertEqual(
            extraction_batch_detail(self.conn, batch_id, student_id="STU-001")[
                "status"
            ],
            "ready_to_commit",
        )

    def test_cross_student_and_provider_idempotency_conflicts_are_closed(self) -> None:
        batch_id = self.create_batch()
        with self.assertRaises(ValueError):
            extraction_review(self.conn, batch_id, student_id="STU-002")
        with self.assertRaises(ValueError):
            submit_provider_results(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "provider:wrong-owner:v1",
                    "provider": "codex",
                    "model_version": "test",
                    "prompt_version": "v1",
                    "results": [],
                },
                student_id="STU-002",
            )
        with self.assertRaises(ExtractionConflict):
            create_extraction_batch(
                self.conn,
                self.batch_payload(key="xtr:e2e:v1", student="STU-002"),
            )

        payload = {
            "idempotency_key": "provider:replay:v1",
            "provider": "deterministic",
            "model_version": "grid-v1",
            "prompt_version": "grid-v1",
            "results": [
                {
                    "extraction_item_id": "XITEM-MC-1",
                    "request_sha256": "6" * 64,
                    "result_status": "succeeded",
                    "raw_transcription": "A",
                    "normalized_transcription": "A",
                    "capture_status": "captured",
                }
            ],
        }
        first = submit_provider_results(
            self.conn, batch_id, payload, student_id="STU-001"
        )
        replay = submit_provider_results(
            self.conn, batch_id, payload, student_id="STU-001"
        )
        self.assertEqual(first["provider_results_inserted"], 1)
        self.assertEqual(replay["status"], "duplicate")
        changed = dict(payload)
        changed["results"] = [dict(payload["results"][0], raw_transcription="B")]
        with self.assertRaises(ExtractionConflict):
            submit_provider_results(
                self.conn, batch_id, changed, student_id="STU-001"
            )

    def test_commit_failure_rolls_back_ingest_and_batch_state(self) -> None:
        payload = self.batch_payload(key="xtr:rollback:v1")
        payload["items"] = [payload["items"][0]]
        batch_id = create_extraction_batch(self.conn, payload)["batch"][
            "extraction_batch_id"
        ]
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": "provider:rollback:v1",
                "provider": "deterministic",
                "model_version": "grid-v1",
                "prompt_version": "grid-v1",
                "results": [
                    {
                        "extraction_item_id": "XITEM-MC-1",
                        "request_sha256": "7" * 64,
                        "result_status": "succeeded",
                        "raw_transcription": "A",
                        "normalized_transcription": "A",
                        "capture_status": "captured",
                    }
                ],
            },
            student_id="STU-001",
        )
        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        decided = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "decision:rollback:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher-test",
                "default_action": "accept_prefill",
            },
            student_id="STU-001",
        )
        baseline = self.counts()
        real_import = import_attempts

        def fail_after_ingest(conn, ingest_payload, **kwargs):
            real_import(conn, ingest_payload, **kwargs)
            raise RuntimeError("injected failure after staged fact insertion")

        with patch("english_tracker.extraction.import_attempts", side_effect=fail_after_ingest):
            with self.assertRaises(RuntimeError):
                commit_extraction_batch(
                    self.conn,
                    batch_id,
                    {
                        "idempotency_key": "commit:rollback:v1",
                        "expected_review_version": decided["review"]["review_version"],
                        "actor": "teacher-test",
                    },
                    student_id="STU-001",
                )
        self.assertEqual(self.counts(), baseline)
        detail = extraction_batch_detail(self.conn, batch_id, student_id="STU-001")
        self.assertEqual(detail["status"], "ready_to_commit")
        self.assertEqual(detail["commit_links"], [])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ingest_events WHERE idempotency_key=?",
                (f"opentutor:extraction:{batch_id}:facts:v1",),
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
