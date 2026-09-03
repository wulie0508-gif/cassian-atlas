from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from english_tracker.dashboard import learning_summary, low_friction_summary, workflow_summary
from english_tracker.db import connect, database_path, initialize_database
from english_tracker.ingest import import_attempts
from english_tracker.webapp import LearningHubHandler, LearningHubServer
from english_tracker.workspace import create_student


class WebAgentApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "private"
        self.library_root = self.root / "library"
        self.library_root.mkdir()
        self.question_bank = self.root / "question-bank.sqlite"
        sqlite3.connect(self.question_bank).close()
        self.env = patch.dict(os.environ, {"ENGLISH_TRACKER_DB_NAME": "learning.sqlite"})
        self.env.start()
        initialize_database(self.data_dir, student_id="STU-001", display_name="Learner")
        conn = connect(database_path(self.data_dir))
        try:
            create_student(
                conn,
                {
                    "student_id": "STU-002",
                    "display_name": "Second learner",
                    "subject_codes": ["english"],
                },
            )
        finally:
            conn.close()
        self.server = LearningHubServer(
            ("127.0.0.1", 0),
            LearningHubHandler,
            data_dir=self.data_dir,
            question_bank=self.question_bank,
            library_root=self.library_root,
            student_id="STU-001",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.env.stop()
        self.temp.cleanup()

    def _post(
        self,
        path: str,
        body: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_route_and_session_boundaries_are_available_over_http(self):
        route_status, route = self._post(
            "/api/agent/route",
            {
                "request_text": "记录阅读成绩并分析错题",
                "student_id": "STU-001",
                "subject_code": "english",
                "register": False,
            },
        )
        self.assertEqual(route_status, 200)
        self.assertEqual(
            [step["capability_key"] for step in route["steps"]],
            ["evidence-recording", "mistake-diagnosis"],
        )

        session_payload = {
            "event_id": "EVT-WEB-SESSION-001",
            "idempotency_key": "web-test:session-001:v1",
            "source_thread": "courseware",
            "student_id": "STU-001",
            "session": {
                "session_id": "SES-WEB-001",
                "session_type": "lesson",
                "title": "HTTP contract test",
                "started_at": "2026-08-03T10:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
        }
        session_status, session = self._post("/api/sessions", session_payload)
        self.assertEqual(session_status, 201)
        self.assertEqual(session["result"]["status"], "applied")
        conn = connect(database_path(self.data_dir), readonly=True)
        try:
            stored = conn.execute(
                "SELECT title FROM learning_sessions WHERE session_id='SES-WEB-001'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(stored["title"], "HTTP contract test")

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/sessions", timeout=5)
        self.assertEqual(error.exception.code, 404)

    def test_post_requires_explicit_non_conflicting_student(self):
        payload = {
            "event_id": "EVT-MISSING-STUDENT",
            "idempotency_key": "web-test:missing-student:v1",
            "source_thread": "courseware",
            "session": {
                "session_id": "SES-MISSING-STUDENT",
                "session_type": "lesson",
                "title": "Must not use server fallback",
                "started_at": "2026-08-03T10:00:00+08:00",
            },
        }
        with self.assertRaises(HTTPError) as missing:
            self._post("/api/sessions", payload)
        self.assertEqual(missing.exception.code, 400)
        self.assertIn("student_id is required", missing.exception.read().decode("utf-8"))

        with self.assertRaises(HTTPError) as conflict:
            self._post(
                "/api/sessions?student_id=STU-001",
                dict(payload, student_id="STU-001"),
                headers={"X-Student-ID": "STU-002"},
            )
        self.assertEqual(conflict.exception.code, 400)
        self.assertIn("Conflicting student_id", conflict.exception.read().decode("utf-8"))

    def test_agent_event_cannot_cross_student_boundary(self):
        _, created = self._post(
            "/api/agent/runs",
            {
                "student_id": "STU-001",
                "idempotency_key": "web-agent-run:v1",
                "request_text": "记录课堂成绩",
            },
        )
        run_id = created["run"]["run_id"]
        with self.assertRaises(HTTPError) as crossed:
            self._post(
                f"/api/agent/runs/{run_id}/events",
                {"student_id": "STU-002", "event_type": "started"},
            )
        self.assertEqual(crossed.exception.code, 400)
        self.assertIn("belongs to student STU-001", crossed.exception.read().decode("utf-8"))

    def test_generation_api_is_idempotent_student_scoped_and_visible(self):
        payload = {
            "student_id": "STU-001",
            "subject_code": "english",
            "title": "Grammar review",
            "artifact_type": "courseware",
            "source_snapshot": {"attempt_ids": ["ATT-1"], "as_of": "2026-08-23"},
            "idempotency_key": "web-generation:grammar-review:v1",
        }
        created_status, created = self._post("/api/generations", payload)
        duplicate_status, duplicate = self._post("/api/generations", payload)
        self.assertEqual(created_status, 201)
        self.assertEqual(duplicate_status, 200)
        self.assertEqual(created["result"]["status"], "created")
        self.assertEqual(duplicate["result"]["status"], "duplicate")
        generation_id = created["result"]["generation"]["generation_id"]
        self.assertEqual(
            duplicate["result"]["generation"]["generation_id"], generation_id
        )

        with urlopen(
            self.base_url + "/api/generations?student_id=STU-001", timeout=5
        ) as response:
            listed = json.loads(response.read().decode("utf-8"))
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["student_id"], "STU-001")

        with urlopen(
            self.base_url + "/api/agent/dashboard?student_id=STU-001", timeout=5
        ) as response:
            dashboard = json.loads(response.read().decode("utf-8"))
        self.assertEqual(dashboard["summary"]["generation_active"], 1)
        self.assertEqual(
            dashboard["recent_generations"][0]["generation_id"], generation_id
        )

        completed_payload = {
            "student_id": "STU-001",
            "status": "completed",
            "output_path": "private/grammar-review.docx",
            "output_sha256": "a" * 64,
            "summary": "Generated and verified",
        }
        _, completed = self._post(
            f"/api/generations/{generation_id}", completed_payload
        )
        _, replayed = self._post(
            f"/api/generations/{generation_id}", completed_payload
        )
        self.assertEqual(completed["result"]["status"], "updated")
        self.assertEqual(replayed["result"]["status"], "duplicate")

        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base_url + "/api/generations", timeout=5)
        self.assertEqual(missing.exception.code, 400)
        self.assertIn("student_id is required", missing.exception.read().decode("utf-8"))

        with self.assertRaises(HTTPError) as crossed_get:
            urlopen(
                self.base_url
                + f"/api/generations/{generation_id}?student_id=STU-002",
                timeout=5,
            )
        self.assertEqual(crossed_get.exception.code, 400)

        with self.assertRaises(HTTPError) as crossed_update:
            self._post(
                f"/api/generations/{generation_id}",
                {"student_id": "STU-002", "status": "cancelled"},
            )
        self.assertEqual(crossed_update.exception.code, 400)

        with self.assertRaises(HTTPError) as crossed_key:
            self._post(
                "/api/generations",
                dict(payload, student_id="STU-002"),
            )
        self.assertEqual(crossed_key.exception.code, 400)
        self.assertIn(
            "another student", crossed_key.exception.read().decode("utf-8")
        )

    def test_high_level_learning_posts_use_deterministic_workflows(self):
        assessment = {
            "student_id": "STU-001",
            "title": "Weekly check",
            "date": "2026-08-23",
            "assessment_kind": "topic_quiz",
            "raw_score": 8,
            "max_score": 10,
        }
        first_status, first = self._post("/api/assessments", assessment)
        replay_status, replay = self._post("/api/assessments", assessment)
        self.assertEqual(first_status, 201)
        self.assertEqual(replay_status, 200)
        self.assertEqual(first["result"]["status"], "applied")
        self.assertEqual(replay["result"]["status"], "duplicate")
        self.assertEqual(first["result"]["session_id"], replay["result"]["session_id"])

        conn = connect(database_path(self.data_dir))
        try:
            from english_tracker.ingest import import_session

            import_session(
                conn,
                {
                    "event_id": "EVT-WEB-WORKFLOW-SEED",
                    "idempotency_key": "web-workflow-seed:v1",
                    "source_thread": "courseware",
                    "student_id": "STU-001",
                    "session": {
                        "session_id": "SES-WEB-WORKFLOW-SEED",
                        "session_type": "lesson",
                        "title": "Seed workflow items",
                        "started_at": "2026-08-22T09:00:00+08:00",
                    },
                },
            )
            import_attempts(
                conn,
                {
                    "event_id": "EVT-WEB-WORKFLOW-ATTEMPTS",
                    "idempotency_key": "web-workflow-attempts:v1",
                    "source_thread": "courseware",
                    "student_id": "STU-001",
                    "session_id": "SES-WEB-WORKFLOW-SEED",
                    "attempts": [
                        {
                            "event_id": "ATT-WEB-WORD",
                            "attempted_at": "2026-08-22T09:01:00+08:00",
                            "student_answer": "deliver",
                            "standard_answer": "deliver",
                            "answer_capture_status": "captured",
                            "evaluation": {"result": "correct", "score": 1, "max_score": 1},
                            "item": {
                                "item_id": "WORD-WEB-DELIVER",
                                "domain": "vocabulary",
                                "item_type": "word",
                                "prompt_snapshot": "递送",
                                "answer_snapshot": "deliver",
                            },
                        },
                        {
                            "event_id": "ATT-WEB-READING",
                            "attempted_at": "2026-08-22T09:02:00+08:00",
                            "student_answer": "B",
                            "standard_answer": "A",
                            "answer_capture_status": "captured",
                            "evaluation": {"result": "wrong", "score": 0, "max_score": 1},
                            "item": {
                                "item_id": "READ-WEB-QUESTION",
                                "domain": "reading",
                                "item_type": "multiple_choice",
                                "prompt_snapshot": "What can be inferred?",
                                "answer_snapshot": "A",
                            },
                        },
                    ],
                },
            )
            reading_attempt_id = conn.execute(
                "SELECT attempt_id FROM attempts WHERE event_id='ATT-WEB-READING'"
            ).fetchone()[0]
        finally:
            conn.close()

        dictation = {
            "student_id": "STU-001",
            "title": "Morning dictation",
            "date": "2026-08-23",
            "items": [
                {"item_id": "WORD-WEB-DELIVER", "student_answer": "deliver"}
            ],
        }
        dictation_status, dictation_first = self._post(
            "/api/dictation/results", dictation
        )
        dictation_replay_status, dictation_replay = self._post(
            "/api/dictation/results", dictation
        )
        self.assertEqual(dictation_status, 201)
        self.assertEqual(dictation_replay_status, 200)
        self.assertEqual(dictation_first["result"]["correct"], 1)
        self.assertEqual(
            dictation_replay["result"]["session_result"]["status"], "duplicate"
        )

        diagnostics = {
            "student_id": "STU-001",
            "diagnostics": [
                {
                    "attempt_id": reading_attempt_id,
                    "error_types": [
                        {
                            "code": "reading_inference_overreach",
                            "error_source": "model_suggested",
                            "verification_status": "suggested",
                            "confidence": 0.8,
                            "rationale": "The selected answer adds an unsupported conclusion.",
                        }
                    ],
                }
            ],
        }
        diagnostic_status, diagnostic_first = self._post(
            "/api/reading/diagnostics", diagnostics
        )
        diagnostic_replay_status, diagnostic_replay = self._post(
            "/api/reading/diagnostics", diagnostics
        )
        self.assertEqual(diagnostic_status, 201)
        self.assertEqual(diagnostic_replay_status, 200)
        self.assertEqual(diagnostic_first["result"]["status"], "applied")
        self.assertEqual(diagnostic_replay["result"]["status"], "duplicate")

        with self.assertRaises(HTTPError) as crossed:
            self._post(
                "/api/reading/diagnostics",
                dict(diagnostics, student_id="STU-002"),
            )
        self.assertEqual(crossed.exception.code, 400)

    def test_health_describes_schema_process_and_read_write_boundaries(self):
        with urlopen(self.base_url + "/api/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertRegex(health["app_version"], r"^\d+\.\d+\.\d+")
        self.assertGreater(health["process_id"], 0)
        self.assertEqual(health["frontend_mode"], "read_only")
        self.assertEqual(health["agent_api_mode"], "write_enabled")
        self.assertEqual(health["interfaces"]["frontend"], "read_only")
        self.assertEqual(health["interfaces"]["agent_api"], "write_enabled")
        self.assertEqual(health["database_probe"], "ok")
        self.assertEqual(health["database_probe_mode"], "SELECT 1")
        self.assertEqual(health["integrity_check"], "deferred")
        self.assertEqual(health["product"], "Cassian Atlas")
        self.assertEqual(health["integrity_check_mode"], "cassian data check")
        self.assertEqual(health["schema"]["pending_versions"], [])
        self.assertFalse(health["schema"]["migration_required"])
        self.assertEqual(
            health["schema"]["current_version"],
            health["schema"]["latest_packaged_version"],
        )

    def test_teacher_dashboard_requires_explicit_student_and_valid_enrollment(self):
        with self.assertRaises(HTTPError) as missing:
            urlopen(
                self.base_url + "/api/teacher/dashboard?subject_code=english",
                timeout=5,
            )
        self.assertEqual(missing.exception.code, 400)
        self.assertIn(
            "student_id is required", missing.exception.read().decode("utf-8")
        )

        with urlopen(
            self.base_url
            + "/api/teacher/dashboard?student_id=STU-002&subject_code=english&as_of=2026-08-23",
            timeout=5,
        ) as response:
            dashboard = json.loads(response.read().decode("utf-8"))
        self.assertEqual(dashboard["student_id"], "STU-002")
        self.assertEqual(dashboard["subject_code"], "english")
        self.assertEqual(dashboard["freshness"]["status"], "no_data")
        self.assertEqual(dashboard["teaching_priorities"], [])

        with self.assertRaises(HTTPError) as unenrolled:
            urlopen(
                self.base_url
                + "/api/teacher/dashboard?student_id=STU-002&subject_code=geography",
                timeout=5,
            )
        self.assertEqual(unenrolled.exception.code, 400)
        self.assertIn("not enrolled", unenrolled.exception.read().decode("utf-8"))

    def test_get_handlers_open_the_learning_database_readonly(self):
        with patch("english_tracker.webapp.connect", wraps=connect) as tracked:
            with urlopen(self.base_url + "/api/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
        self.assertTrue(tracked.call_args_list)
        self.assertTrue(all(call.kwargs.get("readonly") is True for call in tracked.call_args_list))

    def test_dashboard_learning_counts_and_critical_issues_are_student_scoped(self):
        for number, student_id in ((1, "STU-001"), (2, "STU-002")):
            self._post(
                "/api/sessions",
                {
                    "event_id": f"EVT-DASH-SESSION-{number}",
                    "idempotency_key": f"dash-session:{number}:v1",
                    "source_thread": "courseware",
                    "student_id": student_id,
                    "session": {
                        "session_id": f"SES-DASH-{number}",
                        "session_type": "lesson",
                        "title": f"Learner {number}",
                        "started_at": f"2026-08-0{number}T10:00:00+08:00",
                    },
                },
            )

        conn = connect(database_path(self.data_dir))
        try:
            import_attempts(
                conn,
                {
                    "event_id": "EVT-DASH-UNEVALUATED",
                    "idempotency_key": "dash-unevaluated:v1",
                    "source_thread": "courseware",
                    "student_id": "STU-002",
                    "session_id": "SES-DASH-2",
                    "attempts": [
                        {
                            "event_id": "ATT-DASH-UNEVALUATED",
                            "attempted_at": "2026-08-02T10:01:00+08:00",
                            "answer_capture_status": "not_captured",
                            "evaluation": {"result": "needs_check"},
                            "item": {
                                "domain": "grammar",
                                "item_type": "cloze",
                                "prompt_snapshot": "Unevaluated second learner item",
                            },
                        }
                    ],
                },
            )
            conn.execute(
                """
                UPDATE evaluations SET is_current=0
                WHERE attempt_id=(SELECT attempt_id FROM attempts WHERE event_id=?)
                """,
                ("ATT-DASH-UNEVALUATED",),
            )
            conn.commit()
            first_counts = learning_summary(conn, "STU-001")["counts"]
            second_counts = learning_summary(conn, "STU-002")["counts"]
            first_home = low_friction_summary(conn, "STU-001")
            second_home = low_friction_summary(conn, "STU-002")
            first_workflow = workflow_summary(conn, student_id="STU-001")
            second_workflow = workflow_summary(conn, student_id="STU-002")
        finally:
            conn.close()
        self.assertEqual(first_counts["learning_sessions"], 1)
        self.assertEqual(first_counts["attempts"], 0)
        self.assertEqual(second_counts["learning_sessions"], 1)
        self.assertEqual(second_counts["attempts"], 1)
        self.assertEqual(first_home["data_health"]["critical_issue_count"], 0)
        self.assertEqual(second_home["data_health"]["critical_issue_count"], 1)
        self.assertEqual(first_workflow["system_notice"]["existing_attempt_count"], 0)
        self.assertEqual(second_workflow["system_notice"]["existing_attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
