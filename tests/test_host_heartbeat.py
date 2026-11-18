"""Execute the heartbeat via the pinned Hermes CLI against disposable profiles."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

import test_hermes_explicit_memory as hermes


class HostHeartbeatTests(unittest.TestCase):
    def _run(self, directory, database_url=None, *, wrapper=False):
        home = Path(directory) / "hermes"
        installed = home / "plugins" / "custom_build"
        shutil.copytree(hermes.PLUGIN, installed, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        (home / "config.yaml").write_text("plugins:\n  enabled: [custom_build]\n", encoding="utf-8")
        if database_url is not None:
            (home / ".env").write_text(f"CHA_FINAL_DATABASE_URL={json.dumps(database_url)}\n", encoding="utf-8")
        if wrapper:
            script = installed / "scripts" / "custom-build-heartbeat.py"
            self.assertTrue(script.is_file(), "the Hermes cron wrapper is missing")
            command = [sys.executable, "-B", str(script)]
        else:
            command = [sys.executable, "-B", "-m", "hermes_cli.main", "custom-build", "heartbeat"]
        return subprocess.run(
            command,
            cwd=directory,
            env={"HOME": str(directory), "HERMES_HOME": str(home), "PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _assert_failure(self, completed, code):
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(completed.stdout.strip(), code)

    def test_reports_missing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            self._assert_failure(self._run(directory), "config.missing")

    def test_reports_unreachable_store_without_connection_details(self):
        with tempfile.TemporaryDirectory() as directory:
            private_socket = str(Path(directory) / "synthetic-private-socket")
            completed = self._run(directory, make_conninfo(host=private_socket))
            self._assert_failure(completed, "db.unreachable")
            self.assertNotIn(private_socket, completed.stdout + completed.stderr)

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_cron_wrapper_persists_one_quiet_heartbeat_from_the_database_role(self):
        with tempfile.TemporaryDirectory() as directory, hermes.HermesExplicitMemoryTests()._database() as url:
            completed = self._run(directory, url, wrapper=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            with psycopg.connect(url) as conn:
                result = conn.execute("select code, result, source = session_user from cha_final.events").fetchall()
            self.assertEqual(result, [("host.heartbeat", "ok", True)])

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_reports_rejected_event_as_a_failed_run(self):
        with tempfile.TemporaryDirectory() as directory, hermes.HermesExplicitMemoryTests()._database() as url:
            read_only = make_conninfo(url, options="-c default_transaction_read_only=on")
            self._assert_failure(self._run(directory, read_only), "events.write_failed")
            with psycopg.connect(url) as conn:
                self.assertEqual(conn.execute("select count(*) from cha_final.events").fetchone(), (0,))
