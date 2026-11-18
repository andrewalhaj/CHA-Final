"""Exercise safe argument and environment failures in the operator CLI."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LegacyCLITests(unittest.TestCase):
    def _run(self, mapping: str, *args, source_url=None):
        self.assertTrue((ROOT / "scripts" / "import_legacy.py").is_file(), "the legacy rehearsal CLI is missing")
        with tempfile.TemporaryDirectory(prefix="cha-legacy-cli-") as directory:
            home = Path(directory)
            project_map = home / "projects.json"
            project_map.write_text(mapping, encoding="utf-8")
            env = {"HOME": str(home), "HERMES_HOME": str(home / "hermes"), "PATH": os.defpath}
            if source_url is not None:
                env["CHA_FINAL_LEGACY_DATABASE_URL"] = source_url
            return subprocess.run(
                [sys.executable, "-B", "-m", "scripts.import_legacy", "--project-map", str(project_map), *args],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )

    def test_missing_source_setting_returns_named_error(self):
        result = self._run("{}")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"error": "config.missing"})

    def test_apply_requires_a_destination_before_opening_source(self):
        marker = "private synthetic connection setting"
        result = self._run("{}", "--apply", source_url=marker)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"error": "config.missing"})
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_duplicate_project_mapping_fails_without_printing_its_values(self):
        marker = "private-fixture-password"
        mapping = '{"old":"https://github.com/team/old","old":"https://user:' + marker + '@github.com/team/new"}'
        result = self._run(mapping, source_url="invalid fixture source")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"error": "legacy.mapping_invalid"})
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_unrecognized_arguments_do_not_echo_secrets(self):
        marker = "private-synthetic-connection-url"
        result = self._run("{}", marker)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"error": "legacy.arguments_invalid"})
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_apply_flag_cannot_be_abbreviated(self):
        result = self._run("{}", "--a")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"error": "legacy.arguments_invalid"})
