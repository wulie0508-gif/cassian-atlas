from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database, pending_migration_versions
from english_tracker.orchestration import (
    agent_dashboard,
    append_run_event,
    capability_manifest,
    plan_route,
    register_run,
)
from english_tracker.workspace import create_student


class AgentOrchestrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "private"
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner")
        self.conn = connect(database_path(self.data_dir))
        create_student(
            self.conn,
            {
                "student_id": "STU-002",
                "display_name": "Second learner",
                "subject_codes": ["english"],
            },
        )

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def test_manifest_exposes_small_specialist_capabilities(self):
        manifest = capability_manifest()
        skills = {row["skill"] for row in manifest["capabilities"]}
        self.assertIn("record-learning-evidence", skills)
        self.assertIn("diagnose-learning-mistakes", skills)
        self.assertIn("select-learning-practice", skills)
        self.assertIn("manage-learning-system", skills)
        self.assertNotIn("use-learning-hub", skills)
        self.assertEqual(
            manifest["architecture"]["control_plane"],
            "The opentutor CLI is the control plane; the dashboard is a read-only projection for status and evidence.",
        )

    def test_initialized_database_has_no_pending_packaged_migrations(self):
        self.assertEqual(pending_migration_versions(self.conn), [])

    def test_router_builds_minimal_specialist_pipelines(self):
        reading = plan_route(
            "记录今天的阅读成绩，并分析两道错题的错因；学生原始答案已保存。",
            student_id="STU-001",
        )
        self.assertEqual(
            [row["capability_key"] for row in reading["steps"]],
            ["evidence-recording", "mistake-diagnosis"],
        )
        self.assertEqual(reading["execution_mode"], "specialist_pipeline")

        lesson = plan_route(
            "准备下节课课件，并按薄弱点选取覆盖完整语篇的练习。",
            student_id="STU-001",
        )
        self.assertEqual(
            [row["capability_key"] for row in lesson["steps"]],
            ["courseware-context", "practice-selection"],
        )

        dictation = plan_route(
            "读取到期单词，批改听写并保存成绩。",
            student_id="STU-001",
        )
        self.assertEqual([row["capability_key"] for row in dictation["steps"]], ["dictation-workflow"])

    def test_codex_first_platform_upgrade_routes_to_engineering_with_high_confidence(self):
        platform = plan_route(
            "把现有系统改造成 Codex-first、全 CLI 控制的部署，数据库看板只读，并支持多学生升级。",
            student_id="STU-001",
        )
        self.assertEqual(platform["primary_capability"], "platform-engineering")
        self.assertEqual(platform["confidence"], "high")
        self.assertEqual(platform["execution_mode"], "single_skill")
        self.assertEqual(
            [row["skill"] for row in platform["steps"]],
            ["$manage-learning-system"],
        )

    def test_run_ledger_is_idempotent_and_dashboard_ready(self):
        payload = {
            "idempotency_key": "router:lesson-001:v1",
            "student_id": "STU-001",
            "subject_code": "english",
            "source_thread": "orchestrator",
            "request_text": "记录课堂成绩并分析错题。",
            "title": "Lesson evidence",
        }
        created = register_run(self.conn, payload)
        self.assertEqual(created["status"], "created")
        run_id = created["run"]["run_id"]
        duplicate = register_run(self.conn, payload)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["run"]["run_id"], run_id)

        started = append_run_event(
            self.conn,
            run_id,
            {
                "student_id": "STU-001",
                "event_type": "started",
                "capability_key": "evidence-recording",
                "actor": "recording-agent",
                "message": "Validating item-level answers.",
            },
        )
        self.assertEqual(started["run"]["status"], "in_progress")
        completed = append_run_event(
            self.conn,
            run_id,
            {
                "student_id": "STU-001",
                "event_type": "completed",
                "capability_key": "evidence-recording",
                "actor": "recording-agent",
                "message": "Evidence stored.",
                "summary": "Stored two attempts.",
                "result_ref": "/api/performance/sessions",
            },
        )
        self.assertEqual(completed["run"]["status"], "completed")
        self.assertEqual(len(completed["run"]["events"]), 3)

        dashboard = agent_dashboard(self.conn, student_id="STU-001")
        self.assertEqual(dashboard["summary"]["completed"], 1)
        self.assertEqual(dashboard["recent_runs"][0]["run_id"], run_id)

        changed = dict(payload, request_text="A different request")
        with self.assertRaises(ValueError):
            register_run(self.conn, changed)

    def test_runs_require_an_active_student_subject_enrollment(self):
        with self.assertRaisesRegex(ValueError, "not enrolled"):
            register_run(
                self.conn,
                {
                    "student_id": "STU-001",
                    "subject_code": "geography",
                    "request_text": "准备地理课件",
                },
            )

    def test_run_events_are_student_scoped_idempotent_and_terminal(self):
        run = register_run(
            self.conn,
            {
                "idempotency_key": "run:student-one:v1",
                "student_id": "STU-001",
                "request_text": "记录课堂成绩",
            },
        )["run"]
        run_id = run["run_id"]

        with self.assertRaisesRegex(ValueError, "student_id is required"):
            append_run_event(self.conn, run_id, {"event_type": "started"})
        with self.assertRaisesRegex(ValueError, "belongs to student STU-001"):
            append_run_event(
                self.conn,
                run_id,
                {"student_id": "STU-002", "event_type": "started"},
            )

        started_payload = {
            "student_id": "STU-001",
            "idempotency_key": "event:start:v1",
            "event_type": "started",
            "message": "Started once.",
        }
        self.assertEqual(
            append_run_event(self.conn, run_id, started_payload)["status"],
            "updated",
        )
        duplicate = append_run_event(self.conn, run_id, started_payload)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(duplicate["run"]["events"]), 2)
        with self.assertRaisesRegex(ValueError, "different run event"):
            append_run_event(
                self.conn,
                run_id,
                dict(started_payload, message="Changed payload."),
            )

        completed_payload = {
            "student_id": "STU-001",
            "idempotency_key": "event:complete:v1",
            "event_type": "completed",
            "message": "Finished.",
        }
        completed = append_run_event(self.conn, run_id, completed_payload)
        self.assertEqual(completed["run"]["status"], "completed")
        self.assertEqual(
            append_run_event(self.conn, run_id, completed_payload)["status"],
            "duplicate",
        )
        with self.assertRaisesRegex(ValueError, "already terminal"):
            append_run_event(
                self.conn,
                run_id,
                {"student_id": "STU-001", "event_type": "progress"},
            )

    def test_agent_dashboard_does_not_mix_students(self):
        first = register_run(
            self.conn,
            {
                "idempotency_key": "run:first:v1",
                "student_id": "STU-001",
                "request_text": "准备英语课件",
            },
        )["run"]
        second = register_run(
            self.conn,
            {
                "idempotency_key": "run:second:v1",
                "student_id": "STU-002",
                "request_text": "记录英语课堂成绩",
            },
        )["run"]
        append_run_event(
            self.conn,
            first["run_id"],
            {"student_id": "STU-001", "event_type": "completed"},
        )
        one = agent_dashboard(self.conn, student_id="STU-001")
        two = agent_dashboard(self.conn, student_id="STU-002")
        self.assertEqual(one["summary"]["completed"], 1)
        self.assertEqual(one["summary"]["active"], 0)
        self.assertEqual({row["run_id"] for row in one["recent_runs"]}, {first["run_id"]})
        self.assertEqual(two["summary"]["completed"], 0)
        self.assertEqual(two["summary"]["active"], 1)
        self.assertEqual({row["run_id"] for row in two["recent_runs"]}, {second["run_id"]})


if __name__ == "__main__":
    unittest.main()
