from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.extraction import (
    ExtractionConflict,
    commit_extraction_batch,
    create_extraction_batch,
    extraction_review,
    submit_human_decisions,
    submit_provider_results,
)
from english_tracker.ingest import import_session
from english_tracker.orchestration import plan_route


class ConfirmationSkillForwardTest(unittest.TestCase):
    """Exercise the skill boundary with no production data or provider transport."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.db_name = patch.dict(
            os.environ,
            {"ENGLISH_TRACKER_DB_NAME": "skill-forward.sqlite"},
        )
        self.db_name.start()
        initialize_database(
            self.data_dir,
            student_id="STU-001",
            display_name="Anonymous learner",
        )
        self.conn = connect(database_path(self.data_dir))
        import_session(
            self.conn,
            {
                "event_id": "EVT-SKILL-FORWARD-SESSION",
                "idempotency_key": "skill-forward:session:v1",
                "source_thread": "courseware",
                "student_id": "STU-001",
                "session": {
                    "session_id": "SES-SKILL-FORWARD",
                    "session_type": "homework",
                    "title": "Anonymous image evidence",
                    "started_at": "2026-09-02T09:00:00+08:00",
                },
            },
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.db_name.stop()
        self.temp.cleanup()

    def _fact_counts(self) -> tuple[int, int]:
        return (
            int(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]),
            int(self.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]),
        )

    def _batch_payload(self, suffix: str) -> tuple[dict, dict[str, str]]:
        ids = {
            "mc": f"XITEM-SKILL-{suffix}-MC",
            "cloze": f"XITEM-SKILL-{suffix}-CLOZE",
            "translation": f"XITEM-SKILL-{suffix}-TRANSLATION",
        }
        asset_id = f"XAST-SKILL-{suffix}"
        payload = {
            "idempotency_key": f"skill-forward:{suffix}:batch:v1",
            "student_id": "STU-001",
            "subject_code": "english",
            "session_id": "SES-SKILL-FORWARD",
            "source_thread": "courseware",
            "title": "Anonymous MC, handwritten cloze, and translation image",
            "source_images": [
                {
                    "extraction_asset_id": asset_id,
                    "source_uri": f"private://anonymous-fixture/{suffix}/page-1",
                    "sha256": hashlib.sha256(
                        f"synthetic-image-{suffix}".encode("utf-8")
                    ).hexdigest(),
                    "media_type": "image/png",
                    "byte_size": 128,
                    "page_number": 1,
                }
            ],
            "items": [
                {
                    "extraction_item_id": ids["mc"],
                    "extraction_asset_id": asset_id,
                    "ordinal": 1,
                    "question_ref": "Q-MC",
                    "question_type": "multiple_choice",
                    "risk_level": "R0",
                    "evidence_locator": {"page": 1, "region": [0, 0, 20, 20]},
                    "attempt_template": {
                        "attempted_at": "2026-09-02T09:05:00+08:00",
                        "standard_answer": "C",
                        "response_mode": "recognition",
                        "grading_contract": {
                            "mode": "deterministic_exact",
                            "acceptable_answers": ["C"],
                            "max_score": 1,
                        },
                        "item": {
                            "item_id": f"ITEM-SKILL-{suffix}-MC",
                            "domain": "grammar",
                            "item_type": "multiple_choice",
                            "prompt_snapshot": "Anonymous clear multiple-choice prompt",
                            "answer_snapshot": "C",
                        },
                    },
                },
                {
                    "extraction_item_id": ids["cloze"],
                    "extraction_asset_id": asset_id,
                    "ordinal": 2,
                    "question_ref": "Q-CLOZE",
                    "question_type": "cloze",
                    "risk_level": "R2",
                    "second_model_reason": "handwritten_short_free_text",
                    "evidence_locator": {"page": 1, "region": [0, 20, 50, 45]},
                    "attempt_template": {
                        "attempted_at": "2026-09-02T09:06:00+08:00",
                        "standard_answer": "went",
                        "response_mode": "production",
                        "grading_contract": {
                            "mode": "deterministic_exact",
                            "acceptable_answers": ["went"],
                            "max_score": 1,
                        },
                        "item": {
                            "item_id": f"ITEM-SKILL-{suffix}-CLOZE",
                            "domain": "grammar",
                            "item_type": "cloze",
                            "prompt_snapshot": "Anonymous handwritten cloze prompt",
                            "answer_snapshot": "went",
                        },
                    },
                },
                {
                    "extraction_item_id": ids["translation"],
                    "extraction_asset_id": asset_id,
                    "ordinal": 3,
                    "question_ref": "Q-TRANSLATION",
                    "question_type": "translation",
                    "risk_level": "R3",
                    "second_model_reason": "translation_free_text",
                    "evidence_locator": {"page": 1, "region": [0, 45, 100, 100]},
                    "attempt_template": {
                        "attempted_at": "2026-09-02T09:07:00+08:00",
                        "response_mode": "production",
                        "grading_contract": {"mode": "teacher_rubric"},
                        "item": {
                            "item_id": f"ITEM-SKILL-{suffix}-TRANSLATION",
                            "domain": "translation",
                            "item_type": "translation",
                            "prompt_snapshot": "Anonymous translation prompt",
                        },
                    },
                },
            ],
        }
        return payload, ids

    def _create_batch(self, suffix: str) -> tuple[str, dict[str, str]]:
        payload, ids = self._batch_payload(suffix)
        created = create_extraction_batch(self.conn, payload)
        return created["batch"]["extraction_batch_id"], ids

    def _submit(
        self,
        batch_id: str,
        ids: dict[str, str],
        *,
        provider: str,
        submission: str,
        answers: dict[str, str],
    ) -> None:
        submit_provider_results(
            self.conn,
            batch_id,
            {
                "idempotency_key": f"skill-forward:{submission}:v1",
                "provider": provider,
                "model_version": f"synthetic-{provider}-v1",
                "prompt_version": "transcription-v1",
                "completed_at": "2026-09-02T01:10:00Z",
                "results": [
                    {
                        "extraction_item_id": ids[key],
                        "request_sha256": hashlib.sha256(
                            f"{submission}:{key}".encode("utf-8")
                        ).hexdigest(),
                        "result_status": "succeeded",
                        "raw_transcription": value,
                        "normalized_transcription": value,
                        "capture_status": "captured",
                        "uncertain_spans": [],
                        "candidate_alternatives": [],
                        "confidence": 0.9,
                    }
                    for key, value in answers.items()
                ],
            },
            student_id="STU-001",
        )

    @staticmethod
    def _review_item(review: dict, item_id: str) -> dict:
        return next(
            item for item in review["items"] if item["extraction_item_id"] == item_id
        )

    def test_router_keeps_mixed_image_evidence_on_confirmation_path(self) -> None:
        route = plan_route(
            "请从匿名答题图片提取一题清晰选择题、一题手写填空和一题翻译，人工确认后保存。",
            student_id="STU-001",
        )
        self.assertEqual(
            [step["capability_key"] for step in route["steps"]],
            ["evidence-confirmation"],
        )

    def test_silence_default_and_partial_review_cannot_commit(self) -> None:
        baseline = self._fact_counts()
        batch_id, ids = self._create_batch("FULL")
        self._submit(
            batch_id,
            ids,
            provider="deterministic",
            submission="full-deterministic",
            answers={"mc": "C"},
        )
        self._submit(
            batch_id,
            ids,
            provider="codex",
            submission="full-codex",
            answers={
                "cloze": "went",
                "translation": "Candidate translation one.",
            },
        )
        self._submit(
            batch_id,
            ids,
            provider="doubao",
            submission="full-doubao",
            answers={
                "cloze": "want",
                "translation": "Candidate translation two.",
            },
        )
        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        self.assertEqual(review["counts"]["total"], 3)
        self.assertEqual(
            self._review_item(review, ids["mc"])["review_group"], "ordinary"
        )
        for key in ("cloze", "translation"):
            item = self._review_item(review, ids[key])
            self.assertEqual(item["review_group"], "attention")
            self.assertEqual(item["comparison"]["classification"], "content_conflict")
            self.assertTrue(item["comparison"]["second_model_ready"])
        self.assertEqual(self._fact_counts(), baseline)

        with self.assertRaisesRegex(ExtractionConflict, "terminal human decision"):
            commit_extraction_batch(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "skill-forward:full:silent-commit:v1",
                    "expected_review_version": review["review_version"],
                    "actor": "teacher",
                },
                student_id="STU-001",
            )
        with self.assertRaisesRegex(ExtractionConflict, "explicit decision"):
            submit_human_decisions(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "skill-forward:full:silent-default:v1",
                    "expected_review_version": review["review_version"],
                    "actor": "teacher",
                    "default_action": "accept_prefill",
                },
                student_id="STU-001",
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM extraction_confirmation_decisions "
                "WHERE extraction_batch_id=?",
                (batch_id,),
            ).fetchone()[0],
            0,
        )

        mc = self._review_item(review, ids["mc"])
        partial = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "skill-forward:full:partial:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher",
                "decisions": [
                    {
                        "extraction_item_id": ids["mc"],
                        "action": "human_confirmed",
                        "selected_provider_result_id": mc["comparison"][
                            "prefill_provider_result_id"
                        ],
                    }
                ],
            },
            student_id="STU-001",
        )
        self.assertFalse(partial["review"]["can_commit"])
        self.assertEqual(partial["review"]["status"], "pending_review")
        with self.assertRaisesRegex(ExtractionConflict, "terminal human decision"):
            commit_extraction_batch(
                self.conn,
                batch_id,
                {
                    "idempotency_key": "skill-forward:full:partial-commit:v1",
                    "expected_review_version": partial["review"]["review_version"],
                    "actor": "teacher",
                },
                student_id="STU-001",
            )
        self.assertEqual(self._fact_counts(), baseline)

        current = partial["review"]
        cloze = self._review_item(current, ids["cloze"])
        translation = self._review_item(current, ids["translation"])
        completed = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "skill-forward:full:complete:v1",
                "expected_review_version": current["review_version"],
                "actor": "teacher",
                "decisions": [
                    {
                        "extraction_item_id": ids["cloze"],
                        "action": "human_corrected",
                        "selected_provider_result_id": cloze["provider_results"][0][
                            "provider_result_id"
                        ],
                        "confirmed_text": "went",
                    },
                    {
                        "extraction_item_id": ids["translation"],
                        "action": "human_corrected",
                        "selected_provider_result_id": translation["provider_results"][0][
                            "provider_result_id"
                        ],
                        "confirmed_text": "Teacher-confirmed translation.",
                        "evaluation": {
                            "result": "partial",
                            "score": 1,
                            "max_score": 2,
                            "evaluated_by": "teacher",
                        },
                    },
                ],
            },
            student_id="STU-001",
        )
        self.assertTrue(completed["review"]["can_commit"])
        committed = commit_extraction_batch(
            self.conn,
            batch_id,
            {
                "idempotency_key": "skill-forward:full:commit:v1",
                "expected_review_version": completed["review"]["review_version"],
                "actor": "teacher",
            },
            student_id="STU-001",
        )
        self.assertEqual(committed["attempts_inserted"], 3)
        self.assertEqual(committed["readback"]["count"], 3)
        self.assertEqual(
            self._fact_counts(),
            (baseline[0] + 3, baseline[1] + 3),
        )

    def test_r2_r3_need_successful_codex_and_doubao_results(self) -> None:
        baseline = self._fact_counts()
        batch_id, ids = self._create_batch("DISTINCT")
        self._submit(
            batch_id,
            ids,
            provider="deterministic",
            submission="distinct-deterministic",
            answers={"mc": "C"},
        )
        for round_no in (1, 2):
            self._submit(
                batch_id,
                ids,
                provider="codex",
                submission=f"distinct-codex-{round_no}",
                answers={
                    "cloze": "went",
                    "translation": f"Codex candidate round {round_no}.",
                },
            )
        review = extraction_review(self.conn, batch_id, student_id="STU-001")
        for key in ("cloze", "translation"):
            comparison = self._review_item(review, ids[key])["comparison"]
            self.assertEqual(comparison["successful_provider_count"], 1)
            self.assertFalse(comparison["second_model_ready"])
            self.assertEqual(comparison["classification"], "blocked_second_model")

        mc = self._review_item(review, ids["mc"])
        decided = submit_human_decisions(
            self.conn,
            batch_id,
            {
                "idempotency_key": "skill-forward:distinct:decisions:v1",
                "expected_review_version": review["review_version"],
                "actor": "teacher",
                "decisions": [
                    {
                        "extraction_item_id": ids["mc"],
                        "action": "human_confirmed",
                        "selected_provider_result_id": mc["comparison"][
                            "prefill_provider_result_id"
                        ],
                    },
                    {
                        "extraction_item_id": ids["cloze"],
                        "action": "human_corrected",
                        "confirmed_text": "went",
                    },
                    {
                        "extraction_item_id": ids["translation"],
                        "action": "human_corrected",
                        "confirmed_text": "Teacher-confirmed translation.",
                        "evaluation": {
                            "result": "correct",
                            "score": 2,
                            "max_score": 2,
                        },
                    },
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
                    "idempotency_key": "skill-forward:distinct:commit:v1",
                    "expected_review_version": decided["review"]["review_version"],
                    "actor": "teacher",
                },
                student_id="STU-001",
            )
        self.assertEqual(self._fact_counts(), baseline)


if __name__ == "__main__":
    unittest.main()
