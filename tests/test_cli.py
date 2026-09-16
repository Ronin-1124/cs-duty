"""The offline CLI must remain usable without Windows or browser dependencies."""
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_help_without_site_packages(self):
        for module, arguments in [(module, args) for module in ('mock_dongdong', 'cs_duty')
                                  for args in ([], ['serve'], ['demo'])]:
            with self.subTest(module=module, command=arguments):
                result = subprocess.run(
                    [sys.executable, "-S", "-m", module, *arguments, "--help"],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
