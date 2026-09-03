from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from english_tracker.base_projection import (
    CONTRACT_VERSION,
    ProjectionConflict,
    ProjectionError,
    ProjectionPrivacyError,
    ProjectionStateError,
    build_projection_payload,
    claim_next_projection_record,
    projection_contract,
    projection_run_detail,
    record_projection_delivery_result,
    stage_projection_run,
    validate_projection_target_config,
)
from english_tracker.db import connect, database_path, initialize_database
from english_tracker.util import canonical_json


DATA_AS_OF = "2026-09-02T08:00:00Z"


def target_config() -> dict:
    return {
        "schema_version": "synthetic-v1",
        "primary_target": {
            "tenant_display_name": "Cassian Learning Lab | 学习工作室",
            "account_purpose": "synthetic test",
            "cli_profile": "cassian-learning-hub",
            "identity": "user",
            "app_name": "Cassian Learning Ops",
            "students": {
                "STU-001": {
                    "display_name": "Synthetic learner",
                    "folder": {
                        "name": "Synthetic learner folder",
                        "token": "fldcnSyntheticTarget001",
                        "url": "https://cassian-synthetic.feishu.cn/drive/folder/fldcnSyntheticTarget001",
                    },
                    "base": {
                        "name": "Synthetic learner operations",
                        "token": "bascnSyntheticTarget001",
                        "url": "https://cassian-synthetic.feishu.cn/base/bascnSyntheticTarget001",
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


def projection_fixtures() -> dict[str, dict]:
    common = {
        "metric_version": "metrics-v1",
        "freshness_status": "FRESH",
    }
    return {
        "student_overview": {
            **common,
            "sample_size": 4,
            "is_active": True,
            "session_count": 2,
            "attempt_count": 4,
            "scored_attempt_count": 4,
            "accuracy": 0.75,
            "review_due_count": 2,
            "last_activity_at": "2026-09-02T07:30:00Z",
        },
        "period_metrics": {
            **common,
            "sample_size": 4,
            "period_start": "2026-08-25",
            "period_end": "2026-09-01",
            "assessment_kind": "weekly_quiz",
            "reporting_series": "grammar_v1",
            "score_scale_max": 100,
            "attempt_count": 4,
            "scored_attempt_count": 4,
            "accuracy": 0.75,
            "average_score_rate": 0.8,
            "calibration_count": 1,
        },
        "knowledge_performance": {
            **common,
            "sample_size": 4,
            "knowledge_code": "KP-GRA-CLAUSE",
            "attempt_count": 4,
            "distinct_item_count": 2,
            "weighted_accuracy": 0.75,
            "mastery_status": "developing",
            "last_evidence_at": "2026-09-02T07:30:00Z",
        },
        "retest_summary": {
            **common,
            "sample_size": 2,
            "window_start": "2026-08-25",
            "window_end": "2026-09-01",
            "due_count": 3,
            "completed_count": 2,
            "recovered_count": 1,
            "still_incorrect_count": 1,
            "overdue_count": 1,
            "next_due_at": "2026-09-03T08:00:00Z",
        },
        "data_quality": {
            **common,
            "sample_size": 10,
            "check_scope": "student_subject",
            "total_check_count": 10,
            "failed_check_count": 0,
            "critical_failure_count": 0,
            "trust_status": "ready",
            "checked_at": "2026-09-02T07:45:00Z",
        },
        "generation_runs": {
            **common,
            "sample_size": 1,
            "generation_id": "GEN-SYNTHETIC-001",
            "artifact_type": "courseware",
            "run_status": "completed",
            "is_stale": False,
            "created_at": "2026-09-02T06:00:00Z",
            "started_at": "2026-09-02T06:01:00Z",
            "completed_at": "2026-09-02T06:10:00Z",
        },
        "teacher_policy_correction_inbox": {
            **common,
            "sample_size": 1,
            "inbox_item_id": "INBOX-SYNTHETIC-001",
            "inbox_kind": "correction",
            "review_status": "open",
            "priority": "high",
            "reason_code": "provider_disagreement",
            "source_entity_type": "extraction_batch",
            "source_entity_id": "XBAT-SYNTHETIC-001",
            "opened_at": "2026-09-02T07:00:00Z",
            "due_at": "2026-09-03T07:00:00Z",
            "resolved_at": None,
        },
    }


class BaseProjectionOutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Synthetic One")
        initialize_database(self.data_dir, student_id="STU-002", display_name="Synthetic Two")
        self.conn = connect(database_path(self.data_dir))
        self.preflight = validate_projection_target_config(target_config(), student_id="STU-001")
        self.target_fingerprint = self.preflight["target_fingerprint_sha256"]

    def tearDown(self) -> None:
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def stage(self, projection_name: str, records: list[dict], *, key: str) -> dict:
        return stage_projection_run(
            self.conn,
            {
                "idempotency_key": key,
                "projection_name": projection_name,
                "student_id": "STU-001",
                "subject_code": "english",
                "data_as_of": DATA_AS_OF,
                "publisher": "local_test_publisher",
                "records": records,
            },
            target_config=target_config(),
        )

    def test_all_projection_kinds_build_deterministically_and_fit_sql_whitelists(self) -> None:
        contract = projection_contract()
        fixtures = projection_fixtures()
        self.assertEqual(contract["contract_version"], CONTRACT_VERSION)
        self.assertEqual(set(contract["projection_names"]), set(fixtures))

        for projection_name, record in fixtures.items():
            with self.subTest(projection_name=projection_name):
                built = build_projection_payload(
                    projection_name,
                    student_id="stu-001",
                    subject_code="ENGLISH",
                    data_as_of=DATA_AS_OF,
                    record=record,
                )
                reordered = build_projection_payload(
                    projection_name,
                    student_id="STU-001",
                    subject_code="english",
                    data_as_of="2026-09-02T16:00:00+08:00",
                    record=dict(reversed(list(record.items()))),
                )
                self.assertEqual(built, reordered)
                self.assertEqual(canonical_json(built), canonical_json(reordered))
                expected_fields = set(contract["common_outbound_fields"]) | set(
                    contract["projections"][projection_name]["fields"]
                )
                self.assertEqual(set(built), expected_fields)
                self.assertRegex(built["projection_upsert_key"], r"^FBKEY-[0-9A-F]{32}$")
                staged = self.stage(
                    projection_name,
                    [record],
                    key=f"base-projection:{projection_name}:v1",
                )
                self.assertEqual(staged["status"], "created")
                self.assertEqual(staged["run"]["status"], "staged")
                self.assertEqual(staged["run"]["status_counts"], {"pending": 1})

        stored = "\n".join(
            row[0]
            for row in self.conn.execute(
                "SELECT payload_json FROM base_projection_outbox ORDER BY outbox_id"
            )
        )
        self.assertNotIn("bascnSyntheticTarget001", stored)
        self.assertNotIn("/base/", stored)

    def test_every_projection_kind_rejects_sensitive_and_unknown_fields(self) -> None:
        forbidden_fields = (
            "question_text",
            "prompt_snapshot",
            "student_answer",
            "explanation",
            "raw_response",
            "ocr_text",
            "private_path",
        )
        for projection_name, record in projection_fixtures().items():
            for forbidden in forbidden_fields:
                with self.subTest(projection_name=projection_name, forbidden=forbidden):
                    bad = dict(record)
                    bad[forbidden] = "synthetic forbidden content"
                    with self.assertRaises(ProjectionPrivacyError):
                        build_projection_payload(
                            projection_name,
                            student_id="STU-001",
                            subject_code="english",
                            data_as_of=DATA_AS_OF,
                            record=bad,
                        )
            with self.subTest(projection_name=projection_name, unknown=True):
                bad = dict(record, extra_metric=1)
                with self.assertRaisesRegex(ProjectionError, "Unknown projection fields"):
                    build_projection_payload(
                        projection_name,
                        student_id="STU-001",
                        subject_code="english",
                        data_as_of=DATA_AS_OF,
                        record=bad,
                    )

        local_path = dict(projection_fixtures()["knowledge_performance"])
        local_path["knowledge_code"] = r"C:\private\ocr-output.txt"
        with self.assertRaises(ProjectionPrivacyError):
            build_projection_payload(
                "knowledge_performance",
                student_id="STU-001",
                subject_code="english",
                data_as_of=DATA_AS_OF,
                record=local_path,
            )

    def test_freshness_status_uses_the_exact_handoff_enum(self) -> None:
        base = projection_fixtures()["student_overview"]
        for freshness in ("FRESH", "DELAYED", "STALE", "FAILED"):
            with self.subTest(freshness=freshness):
                built = build_projection_payload(
                    "student_overview",
                    student_id="STU-001",
                    subject_code="english",
                    data_as_of=DATA_AS_OF,
                    record=dict(base, freshness_status=freshness),
                )
                self.assertEqual(built["freshness_status"], freshness)
        for invalid in ("fresh", "stale", "unknown", "delayed"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ProjectionError, "freshness_status"):
                    build_projection_payload(
                        "student_overview",
                        student_id="STU-001",
                        subject_code="english",
                        data_as_of=DATA_AS_OF,
                        record=dict(base, freshness_status=invalid),
                    )

    def test_target_config_preflight_is_fail_closed_and_never_returns_token_or_url(self) -> None:
        result = validate_projection_target_config(target_config(), student_id="STU-001")
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["target_identity"]["app_name"], "Cassian Learning Ops")
        self.assertEqual(result["target_identity"]["tenant_host"], "cassian-synthetic.feishu.cn")
        self.assertEqual(result["target_identity"]["tenant_domain"], "feishu.cn")
        self.assertTrue(result["student_folder_target_present"])
        self.assertRegex(result["target_fingerprint_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("bascnSyntheticTarget001", serialized)
        self.assertNotIn("fldcnSyntheticTarget001", serialized)
        self.assertNotIn("/drive/folder/", serialized)
        self.assertNotIn("/base/", serialized)
        self.assertNotIn("Synthetic learner operations", serialized)
        self.assertNotIn("Synthetic learner folder", serialized)

        mutations = {
            "app": lambda cfg: cfg["primary_target"].update(app_name="Wrong App"),
            "profile": lambda cfg: cfg["primary_target"].update(cli_profile="wrong-profile"),
            "identity": lambda cfg: cfg["primary_target"].update(identity="bot"),
            "guard_profile": lambda cfg: cfg["write_guard"].update(required_profile="wrong-profile"),
            "guard_identity": lambda cfg: cfg["write_guard"].update(required_identity="bot"),
            "not_fail_closed": lambda cfg: cfg["write_guard"].update(fail_closed_on_mismatch=False),
            "question_upload": lambda cfg: cfg["write_guard"].update(upload_question_bank=True),
            "host_mismatch": lambda cfg: cfg["primary_target"]["students"]["STU-001"]["base"].update(
                url="https://different-tenant.feishu.cn/base/bascnSyntheticTarget001"
            ),
            "folder_token_mismatch": lambda cfg: cfg["primary_target"]["students"]["STU-001"]["folder"].update(
                token="fldcnDifferentTarget001"
            ),
            "missing_folder": lambda cfg: cfg["primary_target"]["students"]["STU-001"].pop("folder"),
            "missing_folder_url": lambda cfg: cfg["primary_target"]["students"]["STU-001"]["folder"].pop("url"),
            "missing_token": lambda cfg: cfg["primary_target"]["students"]["STU-001"]["base"].pop("token"),
            "missing_base_url": lambda cfg: cfg["primary_target"]["students"]["STU-001"]["base"].pop("url"),
            "missing_base": lambda cfg: cfg["primary_target"]["students"]["STU-001"].pop("base"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                config = copy.deepcopy(target_config())
                mutate(config)
                with self.assertRaises(ProjectionError):
                    validate_projection_target_config(config, student_id="STU-001")
        with self.assertRaisesRegex(ProjectionError, "lacks exact target for STU-002"):
            validate_projection_target_config(target_config(), student_id="STU-002")

    def test_staging_is_order_independent_idempotent_and_conflict_safe(self) -> None:
        first = projection_fixtures()["period_metrics"]
        second = dict(first)
        second.update(
            period_start="2026-08-18",
            period_end="2026-08-24",
            sample_size=3,
            attempt_count=3,
            scored_attempt_count=3,
            calibration_count=1,
            accuracy=2 / 3,
            average_score_rate=0.7,
        )
        created = self.stage(
            "period_metrics",
            [first, second],
            key="base-projection:period:idempotent:v1",
        )
        duplicate = self.stage(
            "period_metrics",
            [dict(reversed(list(second.items()))), dict(reversed(list(first.items())))],
            key="base-projection:period:idempotent:v1",
        )
        self.assertEqual(created["status"], "created")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(created["run"]["projection_run_id"], duplicate["run"]["projection_run_id"])
        self.assertEqual(len(created["run"]["records"]), 2)

        changed = dict(first, accuracy=0.5)
        with self.assertRaises(ProjectionConflict):
            self.stage(
                "period_metrics",
                [changed, second],
                key="base-projection:period:idempotent:v1",
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM base_projection_runs WHERE idempotency_key=?",
                ("base-projection:period:idempotent:v1",),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM base_projection_outbox WHERE projection_run_id=?",
                (created["run"]["projection_run_id"],),
            ).fetchone()[0],
            2,
        )
        with self.assertRaisesRegex(ProjectionStateError, "active delivery run"):
            self.stage(
                "period_metrics",
                [first, second],
                key="base-projection:period:parallel:v1",
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM base_projection_runs WHERE idempotency_key=?",
                ("base-projection:period:parallel:v1",),
            ).fetchone()[0],
            0,
        )

    def test_concurrent_same_idempotency_key_is_serialized_to_domain_results(self) -> None:
        def concurrent_stage(
            key: str,
            records: list[dict],
            barrier: threading.Barrier,
        ) -> tuple[str, object]:
            conn = connect(database_path(self.data_dir))
            try:
                barrier.wait(timeout=5)
                try:
                    result = stage_projection_run(
                        conn,
                        {
                            "idempotency_key": key,
                            "projection_name": "period_metrics",
                            "student_id": "STU-001",
                            "subject_code": "english",
                            "data_as_of": DATA_AS_OF,
                            "publisher": "concurrent_test_publisher",
                            "records": records,
                        },
                        target_config=target_config(),
                    )
                    return "result", result
                except Exception as exc:  # return typed domain failures to the test thread
                    return "error", exc
            finally:
                conn.close()

        identical = dict(
            projection_fixtures()["period_metrics"],
            period_start="2026-07-01",
            period_end="2026-07-07",
        )
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            identical_results = [
                future.result(timeout=15)
                for future in (
                    pool.submit(
                        concurrent_stage,
                        "base-projection:concurrent-identical:v1",
                        [identical],
                        barrier,
                    ),
                    pool.submit(
                        concurrent_stage,
                        "base-projection:concurrent-identical:v1",
                        [copy.deepcopy(identical)],
                        barrier,
                    ),
                )
            ]
        self.assertEqual(
            sorted(value["status"] for kind, value in identical_results if kind == "result"),
            ["created", "duplicate"],
        )
        self.assertFalse([value for kind, value in identical_results if kind == "error"])

        first = dict(
            projection_fixtures()["period_metrics"],
            period_start="2026-07-08",
            period_end="2026-07-14",
            accuracy=0.5,
        )
        changed = dict(first, accuracy=0.75)
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            changed_results = [
                future.result(timeout=15)
                for future in (
                    pool.submit(
                        concurrent_stage,
                        "base-projection:concurrent-conflict:v1",
                        [first],
                        barrier,
                    ),
                    pool.submit(
                        concurrent_stage,
                        "base-projection:concurrent-conflict:v1",
                        [changed],
                        barrier,
                    ),
                )
            ]
        successes = [value for kind, value in changed_results if kind == "result"]
        failures = [value for kind, value in changed_results if kind == "error"]
        self.assertEqual([value["status"] for value in successes], ["created"])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ProjectionConflict)
        self.assertNotIsInstance(failures[0], sqlite3.IntegrityError)
        self.assertNotIsInstance(failures[0], sqlite3.OperationalError)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM base_projection_runs WHERE idempotency_key=?",
                ("base-projection:concurrent-conflict:v1",),
            ).fetchone()[0],
            1,
        )

    def test_concurrent_delivery_receipts_are_serialized_and_rollback_conflicts(self) -> None:
        def claimed_generation(generation_id: str, key: str) -> dict:
            record = dict(
                projection_fixtures()["generation_runs"],
                generation_id=generation_id,
            )
            staged = self.stage("generation_runs", [record], key=key)
            return claim_next_projection_record(
                self.conn,
                staged["run"]["projection_run_id"],
                student_id="STU-001",
                now="2026-09-02T11:00:00Z",
            )

        def concurrent_receipt(
            payload: dict,
            barrier: threading.Barrier,
        ) -> tuple[str, object]:
            conn = connect(database_path(self.data_dir))
            try:
                barrier.wait(timeout=5)
                try:
                    result = record_projection_delivery_result(
                        conn,
                        payload,
                        student_id="STU-001",
                        now="2026-09-02T11:00:01Z",
                    )
                    return "result", result
                except Exception as exc:  # return typed domain failures to the test thread
                    return "error", exc
            finally:
                conn.close()

        identical_claim = claimed_generation(
            "GEN-CONCURRENT-DELIVERY-IDENTICAL",
            "base-projection:delivery-identical:v1",
        )
        identical_payload = {
            "idempotency_key": "base-delivery:concurrent-identical:v1",
            "outbox_id": identical_claim["outbox_id"],
            "attempt_no": identical_claim["attempt_no"],
            "outcome": "succeeded",
            "remote_record_id": "recConcurrentIdentical",
            "readback_payload_sha256": identical_claim["payload_sha256"],
        }
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            identical_results = [
                future.result(timeout=15)
                for future in (
                    pool.submit(
                        concurrent_receipt,
                        identical_payload,
                        barrier,
                    ),
                    pool.submit(
                        concurrent_receipt,
                        copy.deepcopy(identical_payload),
                        barrier,
                    ),
                )
            ]
        self.assertEqual(
            sorted(value["status"] for kind, value in identical_results if kind == "result"),
            ["duplicate", "recorded"],
        )
        self.assertFalse([value for kind, value in identical_results if kind == "error"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM base_projection_delivery_attempts WHERE outbox_id=?",
                (identical_claim["outbox_id"],),
            ).fetchone()[0],
            1,
        )

        conflict_claim = claimed_generation(
            "GEN-CONCURRENT-DELIVERY-CONFLICT",
            "base-projection:delivery-conflict:v1",
        )
        conflict_payload = {
            "idempotency_key": "base-delivery:concurrent-conflict:v1",
            "outbox_id": conflict_claim["outbox_id"],
            "attempt_no": conflict_claim["attempt_no"],
            "outcome": "succeeded",
            "remote_record_id": "recConcurrentWinnerA",
            "readback_payload_sha256": conflict_claim["payload_sha256"],
        }
        changed_payload = dict(
            conflict_payload,
            remote_record_id="recConcurrentWinnerB",
        )
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            conflict_results = [
                future.result(timeout=15)
                for future in (
                    pool.submit(
                        concurrent_receipt,
                        conflict_payload,
                        barrier,
                    ),
                    pool.submit(
                        concurrent_receipt,
                        changed_payload,
                        barrier,
                    ),
                )
            ]
        successes = [value for kind, value in conflict_results if kind == "result"]
        failures = [value for kind, value in conflict_results if kind == "error"]
        self.assertEqual([value["status"] for value in successes], ["recorded"])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ProjectionConflict)
        self.assertNotIsInstance(failures[0], sqlite3.IntegrityError)
        self.assertNotIsInstance(failures[0], sqlite3.OperationalError)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM base_projection_delivery_attempts WHERE outbox_id=?",
                (conflict_claim["outbox_id"],),
            ).fetchone()[0],
            1,
        )
        conflict_row = self.conn.execute(
            """
            SELECT o.status,s.remote_record_id
            FROM base_projection_outbox o
            JOIN base_projection_state s ON s.last_outbox_id=o.outbox_id
            WHERE o.outbox_id=?
            """,
            (conflict_claim["outbox_id"],),
        ).fetchone()
        self.assertEqual(conflict_row["status"], "succeeded")
        self.assertIn(
            conflict_row["remote_record_id"],
            {"recConcurrentWinnerA", "recConcurrentWinnerB"},
        )

    def test_retryable_failure_success_state_and_unchanged_skip_are_auditable(self) -> None:
        record = projection_fixtures()["student_overview"]
        staged = self.stage(
            "student_overview",
            [record],
            key="base-projection:retry:v1",
        )
        run_id = staged["run"]["projection_run_id"]
        with self.assertRaisesRegex(ProjectionStateError, "for student STU-002"):
            projection_run_detail(self.conn, run_id, student_id="STU-002")
        with self.assertRaisesRegex(ProjectionStateError, "for student STU-002"):
            claim_next_projection_record(
                self.conn,
                run_id,
                student_id="STU-002",
                now="2026-09-02T08:59:59Z",
            )
        claimed = claim_next_projection_record(
            self.conn,
            run_id,
            student_id="STU-001",
            now="2026-09-02T09:00:00Z",
        )
        self.assertEqual(claimed["operation"], "lookup_or_create")
        self.assertEqual(claimed["lookup_field"], "projection_upsert_key")
        self.assertEqual(claimed["attempt_no"], 1)
        self.assertIsNone(
            claim_next_projection_record(
                self.conn,
                run_id,
                student_id="STU-001",
                now="2026-09-02T09:00:01Z",
            )
        )

        retry_payload = {
            "idempotency_key": "base-delivery:retry:attempt-1",
            "outbox_id": claimed["outbox_id"],
            "attempt_no": 1,
            "outcome": "retryable_failed",
            "failure_category": "rate_limited",
            "failure_code": "1254291",
            "retry_after_seconds": 60,
        }
        retried = record_projection_delivery_result(
            self.conn,
            retry_payload,
            student_id="STU-001",
            now="2026-09-02T09:01:00Z",
        )
        self.assertEqual(retried["run"]["status"], "retryable_failed")
        duplicate_retry = record_projection_delivery_result(
            self.conn,
            retry_payload,
            student_id="STU-001",
            now="2026-09-02T10:00:00Z",
        )
        self.assertEqual(duplicate_retry["status"], "duplicate")
        with self.assertRaisesRegex(ProjectionStateError, "for student STU-002"):
            record_projection_delivery_result(
                self.conn,
                retry_payload,
                student_id="STU-002",
                now="2026-09-02T10:00:00Z",
            )
        with self.assertRaises(ProjectionConflict):
            record_projection_delivery_result(
                self.conn,
                dict(retry_payload, failure_code="DIFFERENT"),
                student_id="STU-001",
                now="2026-09-02T10:00:00Z",
            )

        self.assertIsNone(
            claim_next_projection_record(
                self.conn,
                run_id,
                student_id="STU-001",
                now="2026-09-02T09:01:59Z",
            )
        )
        claimed_again = claim_next_projection_record(
            self.conn,
            run_id,
            student_id="STU-001",
            now="2026-09-02T09:02:00Z",
        )
        self.assertEqual(claimed_again["attempt_no"], 2)
        success_payload = {
            "idempotency_key": "base-delivery:retry:attempt-2",
            "outbox_id": claimed_again["outbox_id"],
            "attempt_no": 2,
            "outcome": "succeeded",
            "remote_record_id": "recSynthetic001",
            "readback_payload_sha256": claimed_again["payload_sha256"],
        }
        with self.assertRaisesRegex(ProjectionError, "readback_payload_sha256"):
            record_projection_delivery_result(
                self.conn,
                {key: value for key, value in success_payload.items() if key != "readback_payload_sha256"},
                student_id="STU-001",
                now="2026-09-02T09:02:04Z",
            )
        with self.assertRaisesRegex(ProjectionStateError, "readback payload hash"):
            record_projection_delivery_result(
                self.conn,
                dict(success_payload, readback_payload_sha256="0" * 64),
                student_id="STU-001",
                now="2026-09-02T09:02:04Z",
            )
        succeeded = record_projection_delivery_result(
            self.conn,
            success_payload,
            student_id="STU-001",
            now="2026-09-02T09:02:05Z",
        )
        self.assertEqual(succeeded["run"]["status"], "completed")
        self.assertEqual(len(succeeded["run"]["records"][0]["delivery_attempts"]), 2)
        self.assertEqual(
            succeeded["delivery_attempt"]["readback_payload_sha256"],
            claimed_again["payload_sha256"],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT remote_record_id FROM base_projection_state"
            ).fetchone()[0],
            "recSynthetic001",
        )

        unchanged = self.stage(
            "student_overview",
            [record],
            key="base-projection:unchanged:v1",
        )
        self.assertEqual(unchanged["run"]["status"], "completed")
        self.assertEqual(unchanged["run"]["status_counts"], {"skipped_unchanged": 1})
        self.assertIsNone(
            claim_next_projection_record(
                self.conn,
                unchanged["run"]["projection_run_id"],
                student_id="STU-001",
            )
        )

        changed = dict(record, review_due_count=3)
        next_run = self.stage(
            "student_overview",
            [changed],
            key="base-projection:update:v1",
        )
        update_claim = claim_next_projection_record(
            self.conn,
            next_run["run"]["projection_run_id"],
            student_id="STU-001",
        )
        self.assertEqual(update_claim["operation"], "update")
        self.assertEqual(update_claim["remote_record_id"], "recSynthetic001")
        self.assertEqual(
            update_claim["projection_upsert_key"],
            claimed["projection_upsert_key"],
        )

        with self.assertRaisesRegex(ProjectionStateError, "remote record ID drift"):
            record_projection_delivery_result(
                self.conn,
                {
                    "idempotency_key": "base-delivery:drift",
                    "outbox_id": update_claim["outbox_id"],
                    "attempt_no": 1,
                    "outcome": "succeeded",
                    "remote_record_id": "recDifferentRemote",
                    "readback_payload_sha256": update_claim["payload_sha256"],
                },
                student_id="STU-001",
            )

        with self.assertRaises(ProjectionPrivacyError):
            record_projection_delivery_result(
                self.conn,
                {
                    "idempotency_key": "base-delivery:forbidden",
                    "outbox_id": update_claim["outbox_id"],
                    "attempt_no": 1,
                    "outcome": "retryable_failed",
                    "failure_category": "transport",
                    "failure_code": "NETWORK",
                    "raw_response": "synthetic response body",
                },
                student_id="STU-001",
            )

        updated = record_projection_delivery_result(
            self.conn,
            {
                "idempotency_key": "base-delivery:update:attempt-1",
                "outbox_id": update_claim["outbox_id"],
                "attempt_no": 1,
                "outcome": "succeeded",
                "remote_record_id": "recSynthetic001",
                "readback_payload_sha256": update_claim["payload_sha256"],
            },
            student_id="STU-001",
        )
        self.assertEqual(updated["run"]["status"], "completed")

        delivery_attempt_id = succeeded["delivery_attempt"]["delivery_attempt_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE base_projection_delivery_attempts SET failure_code='MUTATED' WHERE delivery_attempt_id=?",
                (delivery_attempt_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM base_projection_delivery_attempts WHERE delivery_attempt_id=?",
                (delivery_attempt_id,),
            )

    def test_sql_gates_block_payload_mutation_and_premature_completion(self) -> None:
        staged = self.stage(
            "data_quality",
            [projection_fixtures()["data_quality"]],
            key="base-projection:sql-gates:v1",
        )
        run_id = staged["run"]["projection_run_id"]
        outbox_id = staged["run"]["records"][0]["outbox_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE base_projection_outbox
                SET payload_json=json_set(payload_json,'$.question_text','forbidden')
                WHERE outbox_id=?
                """,
                (outbox_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE base_projection_runs
                SET status='completed',started_at=?,completed_at=?,updated_at=?
                WHERE projection_run_id=?
                """,
                (DATA_AS_OF, DATA_AS_OF, DATA_AS_OF, run_id),
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM base_projection_runs WHERE projection_run_id=?",
                (run_id,),
            ).fetchone()[0],
            "staged",
        )


if __name__ == "__main__":
    unittest.main()
