from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from english_tracker.cli import build_parser, main
from english_tracker.db import connect, database_path
from english_tracker.runtime import CONFIG_ENV, ENV_BY_KEY, apply_runtime_config, load_runtime_config
from english_tracker.server_control import _pid_alive


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RuntimeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.data_dir = self.root / "private-data"
        self.saved_environment = {
            key: os.environ.get(key) for key in (CONFIG_ENV, *ENV_BY_KEY.values())
        }
        self.config.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "data_dir": str(self.data_dir),
                    "db_name": "test.sqlite",
                    "question_bank": str(self.root / "questions.sqlite"),
                    "library_root": str(self.root / "library"),
                    "project_root": str(self.root),
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        for key, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--config", str(self.config), *arguments])
        payload = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
        return status, payload, stderr.getvalue()

    def test_config_is_loaded_and_applied_to_environment(self):
        runtime = load_runtime_config(self.config)
        environment: dict[str, str] = {}
        apply_runtime_config(runtime, environ=environment)
        self.assertEqual(environment[CONFIG_ENV], str(self.config.resolve()))
        self.assertEqual(environment["ENGLISH_TRACKER_DATA_DIR"], str(self.data_dir.resolve()))
        self.assertEqual(environment["ENGLISH_TRACKER_DB_NAME"], "test.sqlite")
        self.assertEqual(environment["OPEN_TUTOR_PROJECT_ROOT"], str(self.root.resolve()))

    def test_init_and_upgrade_never_create_a_student(self):
        status, initialized, _ = self.run_cli("init")
        self.assertEqual(status, 0)
        self.assertEqual(initialized["student_count_created"], 0)
        with closing(connect(database_path(self.data_dir), readonly=True)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM students").fetchone()[0], 0)
        status, upgraded, _ = self.run_cli("upgrade")
        self.assertEqual(status, 0)
        self.assertEqual(upgraded["status"], "up_to_date")
        self.assertEqual(upgraded["student_count_created"], 0)

    def test_info_reports_pending_and_ordinary_commands_explain_upgrade(self):
        self.run_cli("init")
        with closing(connect(database_path(self.data_dir))) as conn:
            latest = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            conn.execute("DELETE FROM schema_migrations WHERE version=?", (latest,))
            conn.commit()
        status, info, _ = self.run_cli("info")
        self.assertEqual(status, 0)
        self.assertEqual(info["status"], "upgrade_required")
        self.assertIn(latest, info["pending_migrations"])
        status, _, error = self.run_cli("student", "list")
        self.assertEqual(status, 1)
        self.assertIn("cassian upgrade", json.loads(error)["error"])

    def test_checksum_mismatch_is_reported_and_blocks_commands_and_upgrade(self):
        self.run_cli("init")
        with closing(connect(database_path(self.data_dir))) as conn:
            conn.execute(
                "UPDATE schema_migrations SET checksum_sha256=? WHERE version='001'",
                ("0" * 64,),
            )
            conn.commit()
        status, info, _ = self.run_cli("info")
        self.assertEqual(status, 0)
        self.assertEqual(info["status"], "schema_mismatch")
        self.assertEqual(info["migration_status"]["checksum_mismatches"][0]["version"], "001")
        self.assertIsNone(info["upgrade_command"])
        status, _, error = self.run_cli("student", "list")
        self.assertEqual(status, 1)
        self.assertIn("checksum mismatch", json.loads(error)["error"])
        status, _, error = self.run_cli("upgrade")
        self.assertEqual(status, 1)
        self.assertIn("checksum mismatch", json.loads(error)["error"])

    def test_unknown_applied_migration_is_reported_and_blocks_upgrade(self):
        self.run_cli("init")
        with closing(connect(database_path(self.data_dir))) as conn:
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum_sha256) VALUES (?, ?, ?)",
                ("999", "2026-09-02T00:00:00Z", "1" * 64),
            )
            conn.commit()
        status, info, _ = self.run_cli("info")
        self.assertEqual(status, 0)
        self.assertEqual(info["status"], "schema_mismatch")
        self.assertEqual(info["migration_status"]["unknown_applied_versions"], ["999"])
        status, _, error = self.run_cli("upgrade")
        self.assertEqual(status, 1)
        self.assertIn("unavailable in this package", json.loads(error)["error"])

    def test_cli_identity_and_serve_have_no_implicit_student(self):
        parser = build_parser()
        self.assertEqual(parser.prog, "cassian")
        self.assertIsNone(parser.parse_args(["serve"]).student)
        self.assertIsNone(parser.parse_args(["server", "start"]).student)

    def test_primary_and_legacy_cli_entry_points_share_the_same_control_plane(self):
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["name"], "cassian-atlas")
        self.assertEqual(
            project["urls"]["Repository"],
            "https://github.com/wulie0508-gif/cassian-atlas",
        )
        scripts = project["scripts"]
        self.assertEqual(scripts["cassian"], "english_tracker.cli:main")
        self.assertEqual(scripts["cassian-atlas"], "english_tracker.cli:main")
        for alias in ("opentutor", "open-tutor-ledger", "english-tracker"):
            self.assertEqual(scripts[alias], "english_tracker.cli:main")

    def test_server_status_is_read_only_when_no_process_is_managed(self):
        self.run_cli("init")
        status, result, _ = self.run_cli("server", "status", "--port", "65530")
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "stopped")

    def test_process_probe_rejects_an_exited_child(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        self.assertFalse(_pid_alive(process.pid))


if __name__ == "__main__":
    unittest.main()
