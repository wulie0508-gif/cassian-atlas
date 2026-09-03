from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import (
    MigrationChecksumMismatch,
    apply_migrations,
    connect,
    database_path,
    initialize_database,
    migration_status,
)
from english_tracker.generation import (
    mark_completed_generations_stale,
    start_generation,
    update_generation,
)
from english_tracker.ingest import (
    import_attempt_diagnostics,
    import_attempts,
    import_progress,
    import_session,
)
from english_tracker.quality import run_quality_checks
from english_tracker.workspace import create_student


class MultiStudentIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner One")
        self.conn = connect(database_path(self.data_dir))
        create_student(
            self.conn,
            {"student_id": "STU-002", "display_name": "Learner Two", "subject_codes": ["english"]},
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def _session_payload(student_id: str, suffix: str) -> dict:
        return {
            "event_id": f"EVT-SESSION-{suffix}",
            "idempotency_key": f"session:{suffix}",
            "source_thread": "courseware",
            "student_id": student_id,
            "session": {
                "session_id": f"SES-{suffix}",
                "session_type": "class",
                "title": "Ownership lesson",
                "started_at": "2026-08-23T19:00:00+08:00",
            },
            "artifact": {
                "artifact_type": "courseware",
                "title": "Shared-looking title",
                "private_path": rf"C:\private\{suffix}.docx",
            },
        }

    @staticmethod
    def _attempt_payload(student_id: str, suffix: str, *, include_item: bool) -> dict:
        attempt: dict = {
            "attempt_id": f"ATT-{suffix}",
            "event_id": f"ATTEMPT-EVENT-{suffix}",
            "attempted_at": "2026-08-23T20:00:00+08:00",
            "student_answer": "student response",
            "standard_answer": "standard response",
            "answer_capture_status": "captured",
            "response_mode": "production",
            "evaluation": {"result": "wrong", "score": 0, "max_score": 1},
            "item_id": "ITEM-SHARED",
        }
        if include_item:
            attempt["item"] = {
                "item_id": "ITEM-SHARED",
                "subject_code": "english",
                "domain": "grammar",
                "item_type": "cloze",
                "prompt_snapshot": "A shared grammar item",
                "answer_snapshot": "standard response",
                "knowledge_points": ["tense"],
            }
        return {
            "event_id": f"EVT-ATTEMPTS-{suffix}",
            "idempotency_key": f"attempts:{suffix}",
            "source_thread": "courseware",
            "student_id": student_id,
            "session_id": f"SES-{suffix}",
            "attempts": [attempt],
        }

    def _prepare_two_students(self) -> tuple[str, str]:
        import_session(self.conn, self._session_payload("STU-001", "ONE"))
        import_session(self.conn, self._session_payload("STU-002", "TWO"))
        import_attempts(
            self.conn,
            self._attempt_payload("STU-001", "ONE", include_item=True),
        )
        import_attempts(
            self.conn,
            self._attempt_payload("STU-002", "TWO", include_item=False),
        )
        rows = self.conn.execute(
            "SELECT artifact_id,student_id FROM artifacts ORDER BY student_id"
        ).fetchall()
        return rows[0]["artifact_id"], rows[1]["artifact_id"]

    def _assert_sql_rejected(self, sql: str, params: tuple, message: str) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, message):
            self.conn.execute(sql, params)
        self.conn.rollback()

    def test_ingested_artifacts_are_student_owned_and_ids_include_student(self) -> None:
        import_session(self.conn, self._session_payload("STU-001", "ONE"))
        import_session(self.conn, self._session_payload("STU-002", "TWO"))
        rows = self.conn.execute(
            "SELECT artifact_id,student_id FROM artifacts ORDER BY student_id"
        ).fetchall()
        self.assertEqual([row["student_id"] for row in rows], ["STU-001", "STU-002"])
        self.assertNotEqual(rows[0]["artifact_id"], rows[1]["artifact_id"])

    def test_direct_sql_rejects_every_cross_student_relationship(self) -> None:
        artifact_one, artifact_two = self._prepare_two_students()
        self._assert_sql_rejected(
            """
            INSERT INTO artifacts(
              artifact_id,artifact_type,title,verification_status,created_at,updated_at
            ) VALUES ('ART-NO-OWNER','courseware','No owner','unverified','now','now')
            """,
            (),
            "artifacts.student_id is required",
        )
        self._assert_sql_rejected(
            "UPDATE learning_sessions SET artifact_id=? WHERE session_id='SES-ONE'",
            (artifact_two,),
            "session artifact belongs to another student",
        )
        self._assert_sql_rejected(
            "UPDATE attempts SET session_id='SES-TWO' WHERE attempt_id='ATT-ONE'",
            (),
            "attempt session belongs to another student",
        )
        self._assert_sql_rejected(
            "UPDATE attempts SET artifact_id=? WHERE attempt_id='ATT-ONE'",
            (artifact_two,),
            "attempt artifact belongs to another student",
        )
        self._assert_sql_rejected(
            """
            UPDATE review_state SET last_attempt_id='ATT-TWO'
            WHERE student_id='STU-001' AND item_id='ITEM-SHARED'
            """,
            (),
            "review state attempt belongs to another student",
        )
        self._assert_sql_rejected(
            """
            UPDATE review_tasks SET source_attempt_id='ATT-TWO'
            WHERE student_id='STU-001' AND item_id='ITEM-SHARED'
            """,
            (),
            "review task source attempt belongs to another student",
        )
        generation = start_generation(
            self.conn,
            {
                "student_id": "STU-001",
                "subject_code": "english",
                "artifact_type": "courseware",
                "title": "Owned generation",
                "source_snapshot": {"student_id": "STU-001"},
                "idempotency_key": "generation:ownership",
            },
        )["generation"]
        self._assert_sql_rejected(
            "UPDATE artifact_generation_runs SET output_artifact_id=? WHERE generation_id=?",
            (artifact_two, generation["generation_id"]),
            "generation output artifact belongs to another student",
        )
        self._assert_sql_rejected(
            "UPDATE artifacts SET student_id='STU-002' WHERE artifact_id=?",
            (artifact_one,),
            "artifact student conflicts with linked learning records",
        )

    def test_quality_report_contains_student_ownership_gates(self) -> None:
        self._prepare_two_students()
        report = run_quality_checks(self.conn)
        checks = {row["check_id"]: row for row in report["checks"]}
        expected = {
            "artifact_student_ownership",
            "session_artifact_student_consistency",
            "attempt_session_student_consistency",
            "attempt_artifact_student_consistency",
            "review_state_attempt_consistency",
            "review_task_attempt_consistency",
            "generation_artifact_student_consistency",
        }
        self.assertTrue(expected.issubset(checks))
        self.assertTrue(all(checks[check_id]["status"] == "pass" for check_id in expected))

    def test_all_evidence_imports_invalidate_once_and_duplicates_do_not(self) -> None:
        session = self._session_payload("STU-001", "ONE")
        progress = {
            "event_id": "EVT-PROGRESS-ONE",
            "idempotency_key": "progress:one",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "session_id": "SES-ONE",
            "progress": [{"content_label": "Grammar", "progress_status": "completed"}],
        }
        attempts = self._attempt_payload("STU-001", "ONE", include_item=True)
        diagnostics = {
            "event_id": "EVT-DIAGNOSTIC-ONE",
            "idempotency_key": "diagnostic:one",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "diagnostics": [
                {
                    "attempt_id": "ATT-ONE",
                    "error_types": [
                        {
                            "code": "needs_check",
                            "error_source": "model_suggested",
                            "verification_status": "suggested",
                            "rationale": "The captured answer differs from the checked answer.",
                        }
                    ],
                }
            ],
        }
        with patch("english_tracker.ingest.mark_completed_generations_stale") as stale:
            for importer, payload in (
                (import_session, session),
                (import_progress, progress),
                (import_attempts, attempts),
                (import_attempt_diagnostics, diagnostics),
            ):
                self.assertEqual(importer(self.conn, payload)["status"], "applied")
                self.assertEqual(importer(self.conn, payload)["status"], "duplicate")
            self.assertEqual(stale.call_count, 4)
            self.assertEqual(
                [call.kwargs["student_id"] for call in stale.call_args_list],
                ["STU-001"] * 4,
            )

    def test_stale_mark_and_ingest_rows_share_one_transaction(self) -> None:
        generation = start_generation(
            self.conn,
            {
                "student_id": "STU-001",
                "subject_code": "english",
                "artifact_type": "courseware",
                "title": "Transaction test",
                "source_snapshot": {"student_id": "STU-001", "revision": 1},
                "idempotency_key": "generation:transaction",
            },
        )["generation"]
        update_generation(
            self.conn,
            generation["generation_id"],
            {
                "status": "completed",
                "output_path": r"C:\private\transaction.docx",
                "output_sha256": "a" * 64,
            },
            student_id="STU-001",
        )

        def mark_then_fail(conn, *, student_id: str) -> int:
            changed = mark_completed_generations_stale(conn, student_id=student_id)
            raise RuntimeError(f"forced rollback after {changed} stale mark")

        with patch(
            "english_tracker.ingest.mark_completed_generations_stale",
            side_effect=mark_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced rollback"):
                import_session(self.conn, self._session_payload("STU-001", "ROLLBACK"))
        self.assertIsNone(
            self.conn.execute(
                "SELECT stale_reason FROM artifact_generation_runs WHERE generation_id=?",
                (generation["generation_id"],),
            ).fetchone()["stale_reason"]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM learning_sessions WHERE session_id='SES-ROLLBACK'"
            ).fetchone()[0],
            0,
        )


class MigrationChecksumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001")
        self.conn = connect(database_path(self.data_dir))

    def tearDown(self) -> None:
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def test_initialized_database_records_and_verifies_sql_checksums(self) -> None:
        status = migration_status(self.conn)
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["pending_versions"], [])
        self.assertEqual(status["checksum_mismatches"], [])
        checksums = [
            row[0]
            for row in self.conn.execute(
                "SELECT checksum_sha256 FROM schema_migrations ORDER BY version"
            )
        ]
        self.assertTrue(checksums)
        self.assertTrue(all(len(value) == 64 for value in checksums))

    def test_checksum_mismatch_is_explicit_and_blocks_migration(self) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE schema_migrations SET checksum_sha256=? WHERE version='001'",
                ("0" * 64,),
            )
        status = migration_status(self.conn)
        self.assertEqual(status["status"], "mismatch")
        self.assertEqual(status["checksum_mismatches"][0]["version"], "001")
        with self.assertRaisesRegex(MigrationChecksumMismatch, "001"):
            apply_migrations(self.conn)

    def test_legacy_migration_table_is_upgraded_and_backfilled(self) -> None:
        with self.conn:
            self.conn.execute("DROP TRIGGER schema_migrations_checksum_required_insert")
            self.conn.execute("DROP TRIGGER schema_migrations_checksum_required_update")
            self.conn.execute("ALTER TABLE schema_migrations DROP COLUMN checksum_sha256")
        before = migration_status(self.conn)
        self.assertEqual(before["status"], "checksum_uninitialized")
        self.assertTrue(before["missing_checksums"])
        self.assertEqual(apply_migrations(self.conn), [])
        self.assertEqual(migration_status(self.conn)["status"], "ready")


if __name__ == "__main__":
    unittest.main()
