from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from english_tracker import cli


def _target_config() -> dict[str, object]:
    return {
        "primary_target": {
            "tenant_display_name": "Cassian Learning Lab | 学习工作室",
            "app_name": "Cassian Learning Ops",
            "cli_profile": "cassian-learning-hub",
            "identity": "user",
            "students": {
                "STU-001": {
                    "folder": {
                        "name": "Student learning archive",
                        "token": "fldrToken001",
                        "url": "https://cassian.feishu.cn/drive/folder/fldrToken001",
                    },
                    "base": {
                        "name": "Student operations",
                        "token": "baseToken001",
                        "url": "https://cassian.feishu.cn/base/baseToken001",
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


class ProjectionCliContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage_input = self.root / "projection.json"
        self.target_input = self.root / "target.json"
        self.receipt_input = self.root / "receipt.json"
        self.stage_input.write_text(
            json.dumps(
                {
                    "idempotency_key": "projection:student-overview:1",
                    "projection_name": "student_overview",
                    "subject_code": "english",
                    "data_as_of": "2026-09-02T08:00:00Z",
                    "records": [],
                }
            ),
            encoding="utf-8",
        )
        self.target_input.write_text(
            json.dumps(_target_config(), ensure_ascii=False),
            encoding="utf-8",
        )
        self.receipt_input.write_text(
            json.dumps(
                {
                    "student_id": "stu-001",
                    "idempotency_key": "delivery:1",
                    "outbox_id": "FBPOUT-1",
                    "attempt_no": 1,
                    "outcome": "succeeded",
                    "remote_record_id": "rec-1",
                    "readback_payload_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _args(self, **values: object) -> Namespace:
        defaults: dict[str, object] = {
            "student": "stu-001",
            "input": str(self.stage_input),
            "target_config": str(self.target_input),
            "run": "FBPRUN-1",
            "output": None,
        }
        defaults.update(values)
        return Namespace(**defaults)

    def test_parser_exposes_the_six_local_projection_commands(self) -> None:
        parser = cli.build_parser()
        cases = {
            "contract": ([], cli.cmd_projection_contract),
            "target-check": (
                ["--student", "STU-001", "--input", "target.json"],
                cli.cmd_projection_target_check,
            ),
            "stage": (
                [
                    "--student",
                    "STU-001",
                    "--input",
                    "payload.json",
                    "--target-config",
                    "target.json",
                ],
                cli.cmd_projection_stage,
            ),
            "claim": (
                ["--student", "STU-001", "--run", "FBPRUN-1"],
                cli.cmd_projection_claim,
            ),
            "receipt": (
                ["--student", "STU-001", "--input", "receipt.json"],
                cli.cmd_projection_receipt,
            ),
            "show": (
                ["--student", "STU-001", "--run", "FBPRUN-1"],
                cli.cmd_projection_show,
            ),
        }
        for command, (arguments, expected) in cases.items():
            with self.subTest(command=command):
                parsed = parser.parse_args(["projection", command, *arguments])
                self.assertIs(parsed.func, expected)

    def test_target_check_emits_only_the_redacted_local_identity(self) -> None:
        with patch.object(cli, "_emit") as emit:
            self.assertEqual(
                cli.cmd_projection_target_check(
                    self._args(input=str(self.target_input))
                ),
                0,
            )
        result = emit.call_args.args[0]
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["target_identity"]["student_id"], "STU-001")
        for forbidden in (
            "fldrToken001",
            "baseToken001",
            "https://",
            "Student learning archive",
            "Student operations",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_stage_forces_student_and_returns_private_backup(self) -> None:
        conn = MagicMock()
        with (
            patch.object(cli, "_open", return_value=(self.root, conn)),
            patch.object(cli, "_backup", return_value="private-backup.sqlite") as backup,
            patch.object(cli, "stage_projection_run", return_value={"status": "created"}) as stage,
            patch.object(cli, "_emit") as emit,
        ):
            self.assertEqual(cli.cmd_projection_stage(self._args()), 0)
        self.assertEqual(stage.call_args.args[1]["student_id"], "STU-001")
        self.assertEqual(stage.call_args.kwargs["target_config"], _target_config())
        backup.assert_called_once_with(self.root, "projection-stage")
        self.assertEqual(emit.call_args.args[0]["backup"], "private-backup.sqlite")
        conn.close.assert_called_once()

    def test_claim_receipt_and_show_pass_explicit_owner(self) -> None:
        claim_conn = MagicMock()
        with (
            patch.object(cli, "_open", return_value=(self.root, claim_conn)),
            patch.object(cli, "_backup", return_value="claim-backup.sqlite") as backup,
            patch.object(
                cli,
                "claim_next_projection_record",
                return_value={"outbox_id": "FBPOUT-1"},
            ) as claim,
            patch.object(cli, "_emit") as emit,
        ):
            self.assertEqual(cli.cmd_projection_claim(self._args()), 0)
        self.assertEqual(claim.call_args.kwargs["student_id"], "stu-001")
        backup.assert_called_once_with(self.root, "projection-claim")
        self.assertEqual(emit.call_args.args[0]["backup"], "claim-backup.sqlite")

        receipt_conn = MagicMock()
        with (
            patch.object(cli, "_open", return_value=(self.root, receipt_conn)),
            patch.object(cli, "_backup", return_value="receipt-backup.sqlite") as backup,
            patch.object(
                cli,
                "record_projection_delivery_result",
                return_value={"status": "recorded"},
            ) as receipt,
            patch.object(cli, "_emit") as emit,
        ):
            self.assertEqual(
                cli.cmd_projection_receipt(
                    self._args(input=str(self.receipt_input))
                ),
                0,
            )
        self.assertEqual(receipt.call_args.kwargs["student_id"], "STU-001")
        self.assertNotIn("student_id", receipt.call_args.args[1])
        backup.assert_called_once_with(self.root, "projection-receipt")
        self.assertEqual(emit.call_args.args[0]["backup"], "receipt-backup.sqlite")

        show_conn = MagicMock()
        with (
            patch.object(cli, "_open", return_value=(self.root, show_conn)),
            patch.object(cli, "projection_run_detail", return_value={"status": "staged"}) as show,
            patch.object(cli, "_emit"),
        ):
            self.assertEqual(cli.cmd_projection_show(self._args()), 0)
        self.assertEqual(show.call_args.kwargs["student_id"], "stu-001")

    def test_stage_and_receipt_reject_cross_student_payload_before_backup(self) -> None:
        self.stage_input.write_text(
            json.dumps({"student_id": "STU-002"}),
            encoding="utf-8",
        )
        with (
            patch.object(cli, "_open") as open_db,
            patch.object(cli, "_backup") as backup,
        ):
            with self.assertRaisesRegex(ValueError, "conflicts with explicit --student"):
                cli.cmd_projection_stage(self._args())
        open_db.assert_not_called()
        backup.assert_not_called()

        self.receipt_input.write_text(
            json.dumps({"student_id": "STU-002"}),
            encoding="utf-8",
        )
        with (
            patch.object(cli, "_open") as open_db,
            patch.object(cli, "_backup") as backup,
        ):
            with self.assertRaisesRegex(ValueError, "conflicts with explicit --student"):
                cli.cmd_projection_receipt(
                    self._args(input=str(self.receipt_input))
                )
        open_db.assert_not_called()
        backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
