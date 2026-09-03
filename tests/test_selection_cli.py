from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from english_tracker import cli
from english_tracker.runtime import CONFIG_ENV, ENV_BY_KEY


class SelectionCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "private"
        self.question_bank = self.root / "questions.sqlite"
        self.config = self.root / "config.json"
        self.saved_environment = {
            key: os.environ.get(key) for key in (CONFIG_ENV, *ENV_BY_KEY.values())
        }
        self._make_question_bank()
        self.config.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "data_dir": str(self.data_dir),
                    "db_name": "learning.sqlite",
                    "question_bank": str(self.question_bank),
                    "library_root": str(self.root / "library"),
                    "project_root": str(self.root),
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(
            self.run_cli(
                "student",
                "add",
                "--student",
                "STU-001",
                "--display-name",
                "Learner One",
            )[0],
            0,
        )

    def tearDown(self) -> None:
        for key, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _make_question_bank(self) -> None:
        conn = sqlite3.connect(self.question_bank)
        conn.executescript(
            """
            CREATE TABLE sources(
              source_id TEXT PRIMARY KEY,source_mode TEXT,original_path TEXT,
              processing_status TEXT,notes TEXT
            );
            CREATE TABLE passages(
              passage_id TEXT PRIMARY KEY,passage_text TEXT,source_id TEXT,
              source_page INTEGER,verification_status TEXT
            );
            CREATE TABLE questions(
              question_id TEXT PRIMARY KEY,passage_id TEXT,source_id TEXT,
              question_type TEXT,original_number TEXT,source_page INTEGER,stem TEXT,
              answer TEXT,primary_test_point TEXT,secondary_test_points TEXT,
              verification_status TEXT,source_path TEXT,source_ordinal INTEGER
            );
            CREATE TABLE options(
              question_id TEXT,option_label TEXT,option_text TEXT,option_order INTEGER
            );
            CREATE TABLE duplicate_map(
              canonical_question_id TEXT,duplicate_question_id TEXT,similarity_type TEXT
            );
            INSERT INTO sources VALUES
              ('SRC-1','question_only','verified-paper.pdf','source_checked','');
            INSERT INTO questions VALUES
              ('Q-CLI-1',NULL,'SRC-1','翻译','1',1,'Translate: Knowledge grows through use.',
               'Knowledge grows through use.','translation','',
               'source_checked','verified-paper.pdf',1);
            """
        )
        conn.commit()
        conn.close()

    def run_cli(self, *arguments: str) -> tuple[int, dict, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(["--config", str(self.config), *arguments])
        result = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
        error = json.loads(stderr.getvalue()) if stderr.getvalue() else {}
        return status, result, error

    def write_json(self, name: str, value: dict) -> Path:
        target = self.root / name
        target.write_text(json.dumps(value), encoding="utf-8")
        return target

    def _contract(self) -> dict:
        return {
            "rubric_version": "rubric-v1",
            "policy_version": "policy-v1",
            "schema_version": "schema-v1",
        }

    def _selection_payload(self) -> dict:
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        return {
            "training_mode": "transfer",
            "data_as_of": now,
            "candidate_question_ids": ["Q-CLI-1"],
            "candidate_context": {
                "Q-CLI-1": {
                    "reason_codes": ["transfer_check"],
                    "knowledge_codes": ["tense"],
                    "evidence_references": [
                        {
                            "entity_type": "teacher_target",
                            "entity_id": "TARGET-CLI-1",
                            "as_of": now,
                        }
                    ],
                    "priority": 1,
                }
            },
            "target_knowledge_codes": ["tense"],
            "idempotency_key": "selection:cli:v1",
            "explanation_contract": self._contract(),
        }

    def _explanation_payload(self) -> dict:
        return {
            "question_id": "Q-CLI-1",
            "explanation_status": "source_checked",
            "explanation": {
                "standard_answer": "Knowledge grows through use.",
                "reasoning": ["Preserve the general statement and present tense."],
            },
            "created_by": "codex-local",
            "confirmed_by": "teacher-001",
            "explanation_contract": self._contract(),
        }

    def test_parser_exposes_stage4_local_commands(self) -> None:
        parser = cli.build_parser()
        cases = (
            (
                ["selection", "create", "--student", "STU-001", "--input", "selection.json"],
                cli.cmd_selection_create,
            ),
            (
                ["selection", "show", "--student", "STU-001", "--manifest", "SEL-1"],
                cli.cmd_selection_show,
            ),
            (["explanation", "cache", "--input", "explanation.json"], cli.cmd_explanation_cache),
            (["explanation", "lookup", "--question", "Q-1"], cli.cmd_explanation_lookup),
            (["explanation", "invalidate", "--question", "Q-1"], cli.cmd_explanation_invalidate),
        )
        for arguments, expected in cases:
            with self.subTest(command=arguments[:2]):
                self.assertIs(parser.parse_args(arguments).func, expected)

    def test_selection_create_show_and_student_conflict(self) -> None:
        selection_input = self.write_json("selection.json", self._selection_payload())
        status, created, error = self.run_cli(
            "selection",
            "create",
            "--student",
            "stu-001",
            "--input",
            str(selection_input),
        )
        self.assertEqual((status, error), (0, {}))
        self.assertEqual(created["manifest"]["student_id"], "STU-001")
        self.assertEqual(created["manifest"]["selected_question_count"], 1)
        self.assertTrue(created["backup"])
        self.assertTrue(Path(created["backup"]).is_file())
        manifest_id = created["manifest"]["selection_manifest_id"]

        status, shown, error = self.run_cli(
            "selection",
            "show",
            "--student",
            "STU-001",
            "--manifest",
            manifest_id,
        )
        self.assertEqual((status, error), (0, {}))
        self.assertEqual(shown["manifest"]["selection_manifest_id"], manifest_id)
        self.assertNotIn("backup", shown)

        conflicting = self._selection_payload()
        conflicting["student_id"] = "STU-002"
        conflict_input = self.write_json("selection-conflict.json", conflicting)
        status, _, error = self.run_cli(
            "selection",
            "create",
            "--student",
            "STU-001",
            "--input",
            str(conflict_input),
        )
        self.assertEqual(status, 1)
        self.assertIn("conflicts with explicit --student", error["error"])

    def test_explanation_cache_lookup_and_invalidate_use_configured_bank_and_backups(self) -> None:
        explanation_input = self.write_json("explanation.json", self._explanation_payload())
        contract_input = self.write_json("contract.json", self._contract())
        status, cached, error = self.run_cli(
            "explanation",
            "cache",
            "--input",
            str(explanation_input),
        )
        self.assertEqual((status, error), (0, {}))
        self.assertEqual(cached["status"], "created")
        self.assertTrue(Path(cached["backup"]).is_file())

        status, lookup, error = self.run_cli(
            "explanation",
            "lookup",
            "--question",
            "Q-CLI-1",
            "--input",
            str(contract_input),
        )
        self.assertEqual((status, error), (0, {}))
        self.assertEqual(lookup["status"], "hit")
        self.assertNotIn("backup", lookup)

        source = sqlite3.connect(self.question_bank)
        source.execute(
            "UPDATE questions SET answer='Use helps knowledge grow.' WHERE question_id='Q-CLI-1'"
        )
        source.commit()
        source.close()
        status, invalidated, error = self.run_cli(
            "explanation",
            "invalidate",
            "--question",
            "Q-CLI-1",
            "--input",
            str(contract_input),
            "--reason",
            "source_answer_changed",
        )
        self.assertEqual((status, error), (0, {}))
        self.assertEqual(invalidated["status"], "invalidated")
        self.assertEqual(invalidated["invalidated_count"], 1)
        self.assertTrue(Path(invalidated["backup"]).is_file())


if __name__ == "__main__":
    unittest.main()
