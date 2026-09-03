from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from english_tracker.db import connect, database_path, initialize_database
from english_tracker.generation import (
    generation_detail,
    list_generations,
    mark_completed_generations_stale,
    start_generation,
    update_generation,
)
from english_tracker.workspace import create_student


class ArtifactGenerationTest(unittest.TestCase):
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

    def test_generation_is_idempotent_owned_and_stale_aware(self) -> None:
        payload = {
            "student_id": "STU-001",
            "subject_code": "english",
            "artifact_type": "courseware",
            "title": "Grammar review",
            "source_snapshot": {
                "student_id": "STU-001",
                "evidence_as_of": "2026-08-23T19:00:00+08:00",
                "weakness_codes": ["subject_verb_agreement"],
            },
            "idempotency_key": "generation:test:grammar-review:v1",
            "skill_name": "prepare-courseware-context",
        }
        first = start_generation(self.conn, payload)
        duplicate = start_generation(self.conn, payload)
        self.assertEqual(first["status"], "created")
        self.assertEqual(duplicate["status"], "duplicate")
        generation_id = first["generation"]["generation_id"]

        conflicting_owner = dict(payload)
        conflicting_owner["student_id"] = "STU-002"
        with self.assertRaisesRegex(ValueError, "another student"):
            start_generation(self.conn, conflicting_owner)
        with self.assertRaisesRegex(ValueError, "different generation request"):
            start_generation(self.conn, dict(payload, skill_name="different-skill"))

        with self.assertRaisesRegex(ValueError, "STU-002"):
            generation_detail(self.conn, generation_id, student_id="STU-002")
        with self.assertRaisesRegex(ValueError, "STU-002"):
            update_generation(
                self.conn,
                generation_id,
                {"status": "in_progress"},
                student_id="STU-002",
            )
        with self.assertRaisesRegex(ValueError, "status is required"):
            update_generation(self.conn, generation_id, {}, student_id="STU-001")

        started = update_generation(
            self.conn,
            generation_id,
            {"status": "in_progress"},
            student_id="STU-001",
        )
        self.assertEqual(started["generation"]["status"], "in_progress")
        started_replay = update_generation(
            self.conn,
            generation_id,
            {"status": "in_progress"},
            student_id="STU-001",
        )
        self.assertEqual(started_replay["status"], "duplicate")
        with self.assertRaisesRegex(ValueError, "output_path"):
            update_generation(
                self.conn,
                generation_id,
                {"status": "completed"},
                student_id="STU-001",
            )

        output_sha = hashlib.sha256(b"test artifact").hexdigest()
        completed = update_generation(
            self.conn,
            generation_id,
            {
                "status": "completed",
                "output_path": r"C:\private\grammar-review.docx",
                "output_sha256": output_sha,
                "summary": "Generated and verified.",
            },
            student_id="STU-001",
        )
        self.assertEqual(completed["generation"]["status"], "completed")
        replay = update_generation(
            self.conn,
            generation_id,
            {
                "status": "completed",
                "output_path": r"C:\private\grammar-review.docx",
                "output_sha256": output_sha,
                "summary": "Generated and verified.",
            },
            student_id="STU-001",
        )
        self.assertEqual(replay["status"], "duplicate")
        with self.assertRaisesRegex(ValueError, "terminal"):
            update_generation(
                self.conn,
                generation_id,
                {"status": "in_progress"},
                student_id="STU-001",
            )

        self.assertEqual(mark_completed_generations_stale(self.conn, student_id="STU-001"), 1)
        listed = list_generations(self.conn, student_id="STU-001")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["stale_reason"], "new_learning_evidence")
        self.assertEqual(list_generations(self.conn, student_id="STU-002")["count"], 0)


if __name__ == "__main__":
    unittest.main()
