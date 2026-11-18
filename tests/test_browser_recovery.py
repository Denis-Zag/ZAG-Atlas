"""Запуск сетевых сценариев браузера без модели, Docker и реального ожидания."""
import shutil
import subprocess
import unittest
from pathlib import Path


class BrowserRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Для проверки JavaScript нужен Node.js")
    def test_browser_network_scenarios(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [shutil.which("node"), "--test", "--test-reporter=tap", "tests/test_browser_recovery.cjs"],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# pass 10", result.stdout)
