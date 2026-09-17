"""The offline CLI must remain usable without a live client or model connection."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cs_duty.__main__ import main as cs_duty_main
from cs_duty.database import Database
from cs_duty.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_help_without_site_packages(self):
        for module, arguments in [(module, args) for module in ('cs_duty',)
                                  for args in ([], ['serve'])]:
            with self.subTest(module=module, command=arguments):
                result = subprocess.run(
                    [sys.executable, "-S", "-m", module, *arguments, "--help"],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_feishu_check_fails_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cs_duty_main(["feishu-check", "--data-dir", temp])
            self.assertEqual(code, 1)
            self.assertIn("App ID", err.getvalue())

    def test_feishu_check_reports_bot_and_test_message(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "business.sqlite3")
            Settings(db, None).save_runtime({"feishu_mode": "app", "feishu_app_id": "cli_fixture",
                "feishu_app_secret": "secret-fixture", "feishu_chat_id": "oc_group"})
            db.close()
            out = io.StringIO()
            with patch("cs_duty.feishu.FeishuAPI") as api:
                api.return_value.bot_id.return_value = "ou_bot"
                with contextlib.redirect_stdout(out):
                    code = cs_duty_main(["feishu-check", "--data-dir", temp, "--send"])
            self.assertEqual(code, 0)
            self.assertIn("ou_bot", out.getvalue())
            self.assertIn("oc_group", out.getvalue())
            api.return_value.send.assert_called_once()
