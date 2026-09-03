from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.quality import run_quality_checks
from english_tracker.selection_manifests import (
    SelectionManifestConflict,
    cache_public_explanation,
    create_question_selection_manifest,
    invalidate_public_explanations,
    lookup_public_explanation,
    public_explanation_cache_key,
    question_selection_manifest_detail,
)
from english_tracker.workspace import create_student


class SelectionManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "private"
        self.question_bank = self.root / "question-bank.sqlite"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner One")
        self.conn = connect(database_path(self.data_dir))
        create_student(
            self.conn,
            {"student_id": "STU-002", "display_name": "Learner Two", "subject_codes": ["english"]},
        )
        self._make_question_bank()
        self.base_time = datetime.now(UTC).replace(microsecond=0)

    def tearDown(self) -> None:
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _make_question_bank(self) -> None:
        conn = sqlite3.connect(self.question_bank)
        conn.executescript(
            """
            CREATE TABLE sources(
              source_id TEXT PRIMARY KEY,title TEXT,source_mode TEXT,original_path TEXT,
              pdf_original_path TEXT,processing_status TEXT,notes TEXT
            );
            CREATE TABLE passages(
              passage_id TEXT PRIMARY KEY,title TEXT,passage_text TEXT,passage_type TEXT,
              source_id TEXT,source_page INTEGER,original_number TEXT,verification_status TEXT
            );
            CREATE TABLE questions(
              question_id TEXT PRIMARY KEY,passage_id TEXT,source_id TEXT,question_type TEXT,
              original_number TEXT,source_page INTEGER,stem TEXT,answer TEXT,
              primary_test_point TEXT,secondary_test_points TEXT,difficulty TEXT,
              verification_status TEXT,source_path TEXT,source_ordinal INTEGER
            );
            CREATE TABLE options(
              question_id TEXT,option_label TEXT,option_text TEXT,option_order INTEGER,
              PRIMARY KEY(question_id,option_label)
            );
            CREATE TABLE duplicate_map(
              canonical_question_id TEXT,duplicate_question_id TEXT,similarity_type TEXT,
              source_occurrence TEXT,notes TEXT,
              PRIMARY KEY(canonical_question_id,duplicate_question_id)
            );

            INSERT INTO sources VALUES
              ('SRC-REAL','Verified real paper','question_only','real-paper.pdf',NULL,'source_checked',''),
              ('SRC-GEN','Generated worksheet','ai_generated','generated.json',NULL,'source_checked','model generated'),
              ('SRC-DRAFT','Unreviewed source','question_only','draft-paper.pdf',NULL,'draft','');

            INSERT INTO passages VALUES
              ('PAS-READ-1','Reading A',
               'Urban trees cool streets, absorb rainwater, and give birds a place to rest. Researchers compared several neighbourhoods over five summers.',
               'reading','SRC-REAL',1,'A','source_checked'),
              ('PAS-BAD','Incomplete reading',
               'This passage has one unchecked member question and must stay out as a whole.',
               'reading','SRC-REAL',2,'B','source_checked'),
              ('PAS-READ-2','Reading A duplicate',
               'Urban trees cool city streets, absorb rainwater, and give birds places to rest. Researchers compared several neighbourhoods over five summers.',
               'reading','SRC-REAL',3,'C','source_checked');

            INSERT INTO questions VALUES
              ('Q-R1-1','PAS-READ-1','SRC-REAL','阅读理解','1',1,
               'What was compared over five summers?','A','reading_detail','','basic','source_checked','real-paper.pdf',1),
              ('Q-R1-2','PAS-READ-1','SRC-REAL','阅读理解','2',1,
               'Which benefit of trees is mentioned?','B','reading_detail','','basic','verified','real-paper.pdf',2),
              ('Q-BAD-1','PAS-BAD','SRC-REAL','阅读理解','1',2,
               'What is the passage mainly about?','A','reading_main','','basic','source_checked','real-paper.pdf',3),
              ('Q-BAD-2','PAS-BAD','SRC-REAL','阅读理解','2',2,
               'Which statement is supported?','C','reading_detail','','basic','needs_check','real-paper.pdf',4),
              ('Q-R2-1','PAS-READ-2','SRC-REAL','阅读理解','1',3,
               'What did researchers compare?','A','reading_detail','','basic','source_checked','real-paper.pdf',5),
              ('Q-R2-2','PAS-READ-2','SRC-REAL','阅读理解','2',3,
               'What benefit is described?','B','reading_detail','','basic','source_checked','real-paper.pdf',6),
              ('Q-EXACT-A',NULL,'SRC-REAL','翻译','1',4,
               'Translate: Practice makes progress.','Practice makes progress.','translation','','basic','source_checked','real-paper.pdf',7),
              ('Q-EXACT-B',NULL,'SRC-REAL','翻译','2',4,
               'Translate: Practice makes progress.','Practice makes progress.','translation','','basic','verified','real-paper.pdf',8),
              ('Q-UNVERIFIED',NULL,'SRC-REAL','翻译','3',4,
               'Translate an unchecked item.','Unchecked.','translation','','basic','needs_check','real-paper.pdf',9),
              ('Q-GENERATED',NULL,'SRC-GEN','翻译','1',1,
               'A model-created prompt.','Generated.','translation','','basic','verified','generated.json',10),
              ('Q-DRAFT-SOURCE',NULL,'SRC-DRAFT','翻译','1',1,
               'A prompt from an unreviewed source.','Draft.','translation','','basic','verified','draft-paper.pdf',11);

            INSERT INTO options VALUES
              ('Q-R1-1','A','Several neighbourhoods',1),('Q-R1-1','B','Several bird species',2),
              ('Q-R1-2','A','Producing electricity',1),('Q-R1-2','B','Absorbing rainwater',2),
              ('Q-BAD-1','A','A city study',1),('Q-BAD-1','B','A school project',2),
              ('Q-BAD-2','A','Statement A',1),('Q-BAD-2','C','Statement C',2),
              ('Q-R2-1','A','Several neighbourhoods',1),('Q-R2-1','B','Several bird species',2),
              ('Q-R2-2','A','Producing electricity',1),('Q-R2-2','B','Absorbing rainwater',2);

            INSERT INTO duplicate_map VALUES
              ('Q-R1-1','Q-R2-1','near_simhash','second source occurrence','same passage with OCR variation');
            """
        )
        conn.commit()
        conn.close()

    def _question_bank_sha256(self) -> str:
        return hashlib.sha256(self.question_bank.read_bytes()).hexdigest()

    def _payload(
        self,
        student_id: str,
        candidate_ids: list[str],
        *,
        suffix: str,
        when: datetime | None = None,
        mode: str = "transfer",
        allow_exact_retests: bool = False,
        max_questions: int = 20,
    ) -> dict:
        return {
            "student_id": student_id,
            "subject_code": "english",
            "training_mode": mode,
            "data_as_of": (when or self.base_time).isoformat(),
            "candidate_question_ids": candidate_ids,
            "candidate_context": {
                question_id: {
                    "reason_codes": ["due_retest" if mode == "correction" else "transfer_check"],
                    "knowledge_codes": ["tense"],
                    "evidence_references": [
                        {
                            "entity_type": "teacher_target",
                            "entity_id": f"TARGET-{suffix}",
                            "as_of": (when or self.base_time).isoformat(),
                        }
                    ],
                    "priority": len(candidate_ids) - index,
                }
                for index, question_id in enumerate(candidate_ids)
            },
            "target_knowledge_codes": ["tense"],
            "max_questions": max_questions,
            "max_groups": 20,
            "duplicate_window_days": 30,
            "near_duplicate_threshold": 0.92,
            "allow_exact_retests": allow_exact_retests,
            "idempotency_key": f"selection:{student_id}:{suffix}",
            "explanation_contract": {
                "rubric_version": "rubric-v1",
                "policy_version": "policy-v1",
                "schema_version": "schema-v1",
            },
        }

    def test_manifest_selects_only_verified_real_complete_groups_and_audits_exclusions(self) -> None:
        source_sha256 = self._question_bank_sha256()
        candidate_ids = [
            "Q-R1-1",
            "Q-BAD-1",
            "Q-R2-1",
            "Q-UNVERIFIED",
            "Q-GENERATED",
            "Q-DRAFT-SOURCE",
            "Q-EXACT-A",
            "Q-EXACT-B",
        ]
        result = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload("STU-001", candidate_ids, suffix="audit"),
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["manifest"]["status"], "finalized")
        self.assertEqual(result["manifest"]["candidate_question_count"], 8)
        self.assertEqual(result["manifest"]["selected_group_count"], 2)
        self.assertEqual(result["manifest"]["selected_question_count"], 3)
        self.assertEqual(result["manifest"]["coverage"]["selected_unconfirmed"], ["tense"])
        self.assertEqual(
            [item["question_id"] for item in result["items"]],
            ["Q-R1-1", "Q-R1-2", "Q-EXACT-A"],
        )
        self.assertTrue(
            all(item["verification_status"] in {"source_checked", "verified"} for item in result["items"])
        )
        self.assertTrue(all(item["is_real_question"] == 1 for item in result["items"]))

        passage_group = next(group for group in result["groups"] if group["passage_id"] == "PAS-READ-1")
        self.assertEqual(passage_group["expected_question_count"], 2)
        self.assertEqual(passage_group["selected_question_count"], 2)
        self.assertEqual(passage_group["complete_group"], 1)
        self.assertIn("source_path", passage_group["source_locator"])
        self.assertEqual(passage_group["reason_codes"], ["transfer_check"])

        reasons = {row["candidate_question_id"]: row["reason_code"] for row in result["exclusions"]}
        self.assertEqual(reasons["Q-BAD-1"], "incomplete_passage")
        self.assertEqual(reasons["Q-R2-1"], "near_duplicate")
        self.assertEqual(reasons["Q-UNVERIFIED"], "unverified_question")
        self.assertEqual(reasons["Q-GENERATED"], "not_real_question")
        self.assertEqual(reasons["Q-DRAFT-SOURCE"], "not_real_question")
        self.assertEqual(reasons["Q-EXACT-B"], "exact_duplicate")
        near = next(row for row in result["exclusions"] if row["reason_code"] == "near_duplicate")
        self.assertEqual(near["detail"]["basis"], "question_bank_duplicate_map")

        manifest_id = result["manifest"]["selection_manifest_id"]
        with self.assertRaisesRegex(ValueError, "STU-002"):
            question_selection_manifest_detail(self.conn, manifest_id, student_id="STU-002")
        duplicate = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload("STU-001", candidate_ids, suffix="audit"),
        )
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(self._question_bank_sha256(), source_sha256)

        for table in ("question_selection_groups", "question_selection_items"):
            columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            self.assertFalse({"stem", "answer", "explanation", "student_answer"} & columns)

    def test_duplicate_history_is_student_scoped_and_correction_retest_is_explicit(self) -> None:
        first = self._payload("STU-001", ["Q-EXACT-A"], suffix="first")
        created = create_question_selection_manifest(self.conn, self.question_bank, first)
        self.assertEqual(created["manifest"]["selected_question_count"], 1)

        later = self.base_time + timedelta(minutes=5)
        transfer = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload("STU-001", ["Q-EXACT-A"], suffix="repeat-transfer", when=later),
        )
        self.assertEqual(transfer["manifest"]["selected_question_count"], 0)
        self.assertEqual(transfer["exclusions"][0]["reason_code"], "exact_duplicate")

        other_student = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload("STU-002", ["Q-EXACT-A"], suffix="other", when=later),
        )
        self.assertEqual(other_student["manifest"]["selected_question_count"], 1)

        correction = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload(
                "STU-001",
                ["Q-EXACT-A"],
                suffix="correction",
                when=later,
                mode="correction",
                allow_exact_retests=True,
            ),
        )
        self.assertEqual(correction["manifest"]["selected_question_count"], 1)
        self.assertEqual(correction["groups"][0]["duplicate_check"]["result"], "exact_retest")
        self.assertIn("exact_retest", correction["items"][0]["reason_codes"])

        conflict = self._payload("STU-001", ["Q-EXACT-B"], suffix="first", when=later)
        with self.assertRaisesRegex(SelectionManifestConflict, "different selection request"):
            create_question_selection_manifest(self.conn, self.question_bank, conflict)

    def test_correction_never_allows_duplicates_within_the_same_manifest(self) -> None:
        result = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload(
                "STU-001",
                ["Q-EXACT-A", "Q-EXACT-B"],
                suffix="correction-in-run",
                mode="correction",
                allow_exact_retests=True,
            ),
        )
        self.assertEqual([item["question_id"] for item in result["items"]], ["Q-EXACT-A"])
        self.assertEqual(result["exclusions"][0]["candidate_question_id"], "Q-EXACT-B")
        self.assertEqual(result["exclusions"][0]["reason_code"], "exact_duplicate")
        self.assertEqual(result["exclusions"][0]["detail"]["scope"], "current_manifest")

    def test_selection_request_and_evidence_reference_contracts_are_closed(self) -> None:
        unknown_top_level = self._payload("STU-001", ["Q-EXACT-A"], suffix="unknown-top")
        unknown_top_level["student_answer"] = "private response"
        with self.assertRaisesRegex(ValueError, "Unknown selection request field.*student_answer"):
            create_question_selection_manifest(self.conn, self.question_bank, unknown_top_level)

        raw_evidence = self._payload("STU-001", ["Q-EXACT-A"], suffix="raw-evidence")
        raw_evidence["candidate_context"]["Q-EXACT-A"]["evidence_references"] = [
            {
                "entity_type": "attempt",
                "entity_id": "ATT-001",
                "student_answer": "private response",
            }
        ]
        with self.assertRaisesRegex(ValueError, "Unknown .*student_answer"):
            create_question_selection_manifest(self.conn, self.question_bank, raw_evidence)

        path_evidence = self._payload("STU-001", ["Q-EXACT-A"], suffix="path-evidence")
        path_evidence["candidate_context"]["Q-EXACT-A"]["evidence_references"] = [
            {"entity_type": "attempt", "entity_id": r"C:\\private\\answer.json"}
        ]
        with self.assertRaisesRegex(ValueError, "opaque identifier"):
            create_question_selection_manifest(self.conn, self.question_bank, path_evidence)

        first = self._payload("STU-001", ["Q-EXACT-A"], suffix="recent-hash")
        create_question_selection_manifest(self.conn, self.question_bank, first)
        changed_recent = self._payload("STU-001", ["Q-EXACT-A"], suffix="recent-hash")
        changed_recent["recent_question_ids"] = ["Q-EXACT-A"]
        with self.assertRaisesRegex(SelectionManifestConflict, "different selection request"):
            create_question_selection_manifest(self.conn, self.question_bank, changed_recent)

    def test_concurrent_idempotent_creates_normalize_to_created_and_duplicate(self) -> None:
        payload = self._payload("STU-001", ["Q-EXACT-A"], suffix="concurrent")

        def run_once() -> str:
            worker = connect(database_path(self.data_dir))
            try:
                return create_question_selection_manifest(
                    worker,
                    self.question_bank,
                    payload,
                )["status"]
            finally:
                worker.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(lambda _: run_once(), range(2)))
        self.assertEqual(statuses, ["created", "duplicate"])

    def test_question_limit_never_splits_a_passage_and_finalization_trigger_rejects_partial_group(self) -> None:
        result = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload(
                "STU-001",
                ["Q-R1-1"],
                suffix="limit",
                max_questions=1,
            ),
        )
        self.assertEqual(result["manifest"]["selected_group_count"], 0)
        self.assertEqual(result["manifest"]["selected_question_count"], 0)
        self.assertEqual(result["exclusions"][0]["reason_code"], "question_limit")
        self.assertTrue(result["exclusions"][0]["detail"]["complete_group_preserved"])

        now = self.base_time.isoformat()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "inserted in building status"):
            self.conn.execute(
                """
                INSERT INTO question_selection_manifests(
                  selection_manifest_id,idempotency_key,request_sha256,student_id,subject_code,
                  training_mode,status,source_namespace,source_snapshot_sha256,source_locator_json,
                  data_as_of,algorithm_version,target_knowledge_json,selection_policy_json,
                  explanation_contract_json,candidate_question_count,selected_group_count,
                  selected_question_count,exclusion_count,coverage_json,created_at,finalized_at
                ) VALUES ('SEL-DIRECT','selection:direct',?,'STU-001','english','transfer',
                          'finalized','shanghai_question_bank',?,'{}',?,'test-v1','[]','{}','{}',
                          0,0,0,0,'{}',?,?)
                """,
                ("9" * 64, "8" * 64, now, now, now),
            )
        self.conn.rollback()
        self.conn.execute(
            """
            INSERT INTO question_selection_manifests(
              selection_manifest_id,idempotency_key,request_sha256,student_id,subject_code,
              training_mode,status,source_namespace,source_snapshot_sha256,source_locator_json,
              data_as_of,algorithm_version,target_knowledge_json,selection_policy_json,
              explanation_contract_json,candidate_question_count,selected_group_count,
              selected_question_count,exclusion_count,coverage_json,created_at
            ) VALUES ('SEL-PARTIAL','selection:partial',?,'STU-001','english','transfer',
                      'building','shanghai_question_bank',?,'{}',?,'test-v1','[]','{}','{}',
                      1,1,0,0,'{}',?)
            """,
            ("a" * 64, "b" * 64, now, now),
        )
        self.conn.execute(
            """
            INSERT INTO question_selection_groups(
              selection_group_id,selection_manifest_id,group_kind,passage_id,source_id,
              source_locator_json,passage_content_sha256,group_content_sha256,
              expected_question_count,selected_question_count,complete_group,reason_codes_json,
              knowledge_codes_json,evidence_references_json,duplicate_check_json,ordinal
            ) VALUES ('SELG-PARTIAL','SEL-PARTIAL','passage','PAS-X','SRC-X','{}',?, ?,
                      2,1,1,'[]','[]','[]','{}',1)
            """,
            ("c" * 64, "d" * 64),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "incomplete passage group"):
            self.conn.execute(
                """
                UPDATE question_selection_manifests
                SET status='finalized', finalized_at=?
                WHERE selection_manifest_id='SEL-PARTIAL'
                """,
                (now,),
            )
        self.conn.rollback()

    def _explanation_payload(self, *, policy: str = "policy-v1") -> dict:
        return {
            "question_id": "Q-EXACT-A",
            "explanation_status": "source_checked",
            "explanation": {
                "standard_answer": "Practice makes progress.",
                "reasoning": ["Identify the imperative source sentence.", "Preserve the general statement."],
                "common_errors": ["Changing the tense without evidence."],
            },
            "created_by": "codex-local-structured-input",
            "confirmed_by": "teacher-001",
            "explanation_contract": {
                "rubric_version": "rubric-v1",
                "policy_version": policy,
                "schema_version": "schema-v1",
            },
        }

    def test_public_explanation_cache_is_non_student_deterministic_and_versioned(self) -> None:
        created = cache_public_explanation(
            self.conn,
            self.question_bank,
            self._explanation_payload(),
        )
        self.assertEqual(created["status"], "created")
        key_v1 = created["cache_identity"]["cache_key"]
        self.assertEqual(len(key_v1), 64)
        duplicate = cache_public_explanation(
            self.conn,
            self.question_bank,
            self._explanation_payload(),
        )
        self.assertEqual(duplicate["status"], "duplicate")
        hit = lookup_public_explanation(
            self.conn,
            self.question_bank,
            "Q-EXACT-A",
            explanation_contract=self._explanation_payload()["explanation_contract"],
        )
        self.assertEqual(hit["status"], "hit")
        self.assertEqual(hit["explanation"]["explanation"]["reasoning"][0], "Identify the imperative source sentence.")

        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(public_question_explanations)")}
        self.assertFalse({"student_id", "attempt_id", "student_answer", "diagnosis"} & columns)
        forbidden = self._explanation_payload()
        forbidden["explanation"] = {"reasoning": ["Public reasoning"], "student_answer": "private"}
        with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
            cache_public_explanation(self.conn, self.question_bank, forbidden)
        forbidden_cn = self._explanation_payload()
        forbidden_cn["explanation"] = {"reasoning": ["Public reasoning"], "学生诊断": "private"}
        with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
            cache_public_explanation(self.conn, self.question_bank, forbidden_cn)
        forbidden_value = self._explanation_payload()
        forbidden_value["explanation"] = {
            "summary": "STU-001 answered ATT-SECRET from private://answers/item.json"
        }
        with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
            cache_public_explanation(self.conn, self.question_bank, forbidden_value)
        forbidden_actor = self._explanation_payload()
        forbidden_actor["confirmed_by"] = "STU-001"
        with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
            cache_public_explanation(self.conn, self.question_bank, forbidden_actor)
        unverified = self._explanation_payload()
        unverified["question_id"] = "Q-UNVERIFIED"
        with self.assertRaisesRegex(ValueError, "verified real complete question"):
            cache_public_explanation(self.conn, self.question_bank, unverified)
        incomplete_group = self._explanation_payload()
        incomplete_group["question_id"] = "Q-BAD-1"
        with self.assertRaisesRegex(ValueError, "complete question group"):
            cache_public_explanation(self.conn, self.question_bank, incomplete_group)

        v2 = cache_public_explanation(
            self.conn,
            self.question_bank,
            self._explanation_payload(policy="policy-v2"),
        )
        self.assertEqual(v2["status"], "created")
        self.assertEqual(v2["invalidated_count"], 1)
        self.assertNotEqual(key_v1, v2["cache_identity"]["cache_key"])
        old_status = self.conn.execute(
            "SELECT explanation_status FROM public_question_explanations WHERE cache_key=?",
            (key_v1,),
        ).fetchone()[0]
        self.assertEqual(old_status, "stale")

        stable_inputs = {
            "question_id": "Q",
            "source_snapshot_sha256": "a" * 64,
            "question_content_sha256": "b" * 64,
            "standard_answer_sha256": "c" * 64,
            "knowledge_mapping_sha256": "d" * 64,
            "rubric_sha256": "e" * 64,
            "explanation_policy_version": "policy-v1",
            "explanation_schema_version": "schema-v1",
        }
        self.assertEqual(
            public_explanation_cache_key(**stable_inputs),
            public_explanation_cache_key(**stable_inputs),
        )
        self.assertNotEqual(
            public_explanation_cache_key(**stable_inputs),
            public_explanation_cache_key(**{**stable_inputs, "explanation_policy_version": "policy-v2"}),
        )

    def test_public_explanation_rejects_learner_identity_and_targeted_prose(self) -> None:
        # Inactive learner identities remain private and must not enter the public cache.
        self.conn.execute("UPDATE students SET active=0 WHERE student_id='STU-002'")
        self.conn.commit()

        named = self._explanation_payload(policy="privacy-name")
        named["explanation"] = {
            "analysis": ["The teacher reviewed this note with LEARNER TWO yesterday."]
        }
        with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
            cache_public_explanation(self.conn, self.question_bank, named)

        embedded_name = self._explanation_payload(policy="privacy-name-embedded")
        embedded_name["explanation"] = {"analysis": ["tag-xLEARNER TWOy"]}
        with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
            cache_public_explanation(self.conn, self.question_bank, embedded_name)

        named_actor = self._explanation_payload(policy="privacy-actor")
        named_actor["confirmed_by"] = "learner two"
        with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
            cache_public_explanation(self.conn, self.question_bank, named_actor)

        targeted_values = (
            "This learner chose the distractor.",
            "该学生需要重新检查时态。",
            "本次作答遗漏了谓语。",
        )
        for index, text in enumerate(targeted_values):
            with self.subTest(text=text):
                targeted = self._explanation_payload(policy=f"privacy-targeted-{index}")
                targeted["explanation"] = {"analysis": [text]}
                with self.assertRaisesRegex(ValueError, "student-specific diagnosis"):
                    cache_public_explanation(self.conn, self.question_bank, targeted)

        generic = self._explanation_payload(policy="privacy-generic")
        generic["explanation"] = {
            "analysis": ["Students often confuse tense in this construction."]
        }
        created = cache_public_explanation(self.conn, self.question_bank, generic)
        self.assertEqual(created["status"], "created")

    def test_public_explanation_quality_guard_matches_write_validation(self) -> None:
        def named_learner_check() -> dict:
            report = run_quality_checks(self.conn)
            return next(
                check
                for check in report["checks"]
                if check["check_id"] == "public_explanation_named_learner_guard"
            )

        cached = cache_public_explanation(
            self.conn,
            self.question_bank,
            {
                **self._explanation_payload(policy="quality-privacy"),
                "explanation": {
                    "analysis": ["Students often confuse tense in this construction."]
                },
            },
        )
        explanation_id = cached["explanation"]["public_explanation_id"]
        self.assertEqual(named_learner_check()["status"], "pass")
        self.conn.execute("DROP TRIGGER public_question_explanation_identity_immutable")

        # Exercise inactive-name matching, whitespace normalization, case folding,
        # arbitrary nesting, and literal substring semantics used by the write guard.
        self.conn.execute(
            "UPDATE students SET active=0, display_name='Learner   Two' WHERE student_id='STU-002'"
        )
        self.conn.execute(
            "UPDATE public_question_explanations SET explanation_json=? WHERE public_explanation_id=?",
            (
                '{"analysis":{"audit":["tag-xLEARNER TWOy"]}}',
                explanation_id,
            ),
        )
        self.conn.commit()
        self.assertEqual(named_learner_check()["failed_rows"], 1)

        targeted_values = (
            "This student answered with the distractor.",
            "这位学生需要重新检查时态。",
            "本次作答遗漏了谓语。",
        )
        for text in targeted_values:
            with self.subTest(text=text):
                self.conn.execute(
                    "UPDATE public_question_explanations SET explanation_json=? WHERE public_explanation_id=?",
                    ('{"analysis":' + repr(text).replace("'", '"') + "}", explanation_id),
                )
                self.conn.commit()
                self.assertEqual(named_learner_check()["failed_rows"], 1)

        self.conn.execute(
            """
            UPDATE public_question_explanations
            SET explanation_json='{"analysis":["Students often confuse tense."]}',
                confirmed_by='LEARNER TWO reviewer'
            WHERE public_explanation_id=?
            """,
            (explanation_id,),
        )
        self.conn.commit()
        self.assertEqual(named_learner_check()["failed_rows"], 1)

        self.conn.execute(
            """
            UPDATE public_question_explanations
            SET confirmed_by='teacher-001'
            WHERE public_explanation_id=?
            """,
            (explanation_id,),
        )
        self.conn.commit()
        self.assertEqual(named_learner_check()["status"], "pass")

    def test_source_or_answer_change_misses_and_explicitly_invalidates_cache(self) -> None:
        created = cache_public_explanation(
            self.conn,
            self.question_bank,
            self._explanation_payload(),
        )
        old_key = created["cache_identity"]["cache_key"]
        source = sqlite3.connect(self.question_bank)
        source.execute(
            "UPDATE questions SET answer='Steady practice leads to progress.' WHERE question_id='Q-EXACT-A'"
        )
        source.commit()
        source.close()

        lookup = lookup_public_explanation(
            self.conn,
            self.question_bank,
            "Q-EXACT-A",
            explanation_contract=self._explanation_payload()["explanation_contract"],
        )
        self.assertEqual(lookup["status"], "miss")
        self.assertEqual(lookup["reason"], "deterministic_cache_identity_changed")
        self.assertNotEqual(old_key, lookup["cache_identity"]["cache_key"])
        invalidated = invalidate_public_explanations(
            self.conn,
            self.question_bank,
            "Q-EXACT-A",
            explanation_contract=self._explanation_payload()["explanation_contract"],
            reason="source_answer_changed",
        )
        self.assertEqual(invalidated["status"], "invalidated")
        self.assertEqual(invalidated["invalidated_count"], 1)
        row = self.conn.execute(
            """
            SELECT explanation_status,invalidation_reason
            FROM public_question_explanations WHERE cache_key=?
            """,
            (old_key,),
        ).fetchone()
        self.assertEqual(dict(row), {"explanation_status": "stale", "invalidation_reason": "source_answer_changed"})

    def test_source_downgrade_invalidates_instead_of_reusing_public_explanation(self) -> None:
        created = cache_public_explanation(
            self.conn,
            self.question_bank,
            self._explanation_payload(),
        )
        source = sqlite3.connect(self.question_bank)
        source.execute(
            "UPDATE questions SET verification_status='needs_check' WHERE question_id='Q-EXACT-A'"
        )
        source.commit()
        source.close()

        invalidated = invalidate_public_explanations(
            self.conn,
            self.question_bank,
            "Q-EXACT-A",
            explanation_contract=self._explanation_payload()["explanation_contract"],
            reason="source_no_longer_verified",
        )
        self.assertEqual(invalidated["status"], "invalidated")
        self.assertEqual(invalidated["lookup_status"], "source_not_reusable")
        self.assertIsNone(invalidated["expected_cache_key"])
        row = self.conn.execute(
            "SELECT explanation_status,invalidation_reason FROM public_question_explanations WHERE cache_key=?",
            (created["cache_identity"]["cache_key"],),
        ).fetchone()
        self.assertEqual(dict(row), {"explanation_status": "stale", "invalidation_reason": "source_no_longer_verified"})

    def test_cached_explanation_status_is_snapshotted_only_for_selected_items(self) -> None:
        cached = cache_public_explanation(
            self.conn,
            self.question_bank,
            self._explanation_payload(),
        )
        result = create_question_selection_manifest(
            self.conn,
            self.question_bank,
            self._payload("STU-001", ["Q-EXACT-A", "Q-UNVERIFIED"], suffix="cache-link"),
        )
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["question_id"], "Q-EXACT-A")
        self.assertEqual(result["items"][0]["public_explanation_status"], "source_checked")
        self.assertEqual(
            result["items"][0]["expected_public_explanation_cache_key"],
            cached["cache_identity"]["cache_key"],
        )
        self.assertEqual(result["exclusions"][0]["candidate_question_id"], "Q-UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
