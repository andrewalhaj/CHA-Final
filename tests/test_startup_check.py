"""Exercise the preflight through the pinned Hermes command."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import psycopg
import test_hermes_explicit_memory as hermes
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_disposable_rollout import _app_login


class StartupCheckTests(unittest.TestCase):
    def _run(self, folder, database_url=None, *, provider="custom_build", broken_provider=False):
        home = Path(folder) / "hermes"
        installed = home / "plugins" / "custom_build"
        shutil.copytree(hermes.PLUGIN, installed, ignore=shutil.ignore_patterns("__pycache__"))
        config = home / "config.yaml"
        config.write_text(f"plugins:\n  enabled: [custom_build]\nmemory:\n  provider: {provider}\n", encoding="utf-8")
        dotenv = home / ".env"
        if database_url is not None:
            dotenv.write_text(f"CHA_FINAL_DATABASE_URL={json.dumps(database_url)}\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in (config, dotenv) if path.is_file()}
        if broken_provider:
            module = installed / "__init__.py"
            original = module.read_text(encoding="utf-8")
            constructor = "    def __init__(self) -> None:\n"
            self.assertIn(constructor, original)
            module.write_text(original.replace(constructor, constructor + "        raise RuntimeError('unusable provider')\n", 1),
                              encoding="utf-8")

        completed = subprocess.run(
            [sys.executable, "-B", "-m", "hermes_cli.main", "custom-build", "check"],
            cwd=folder, env={"HOME": str(folder), "HERMES_HOME": str(home), "PATH": os.defpath},
            capture_output=True, text=True, timeout=12, check=False,
        )
        self.assertEqual({path: path.read_bytes() for path in (config, dotenv) if path.is_file()}, before)
        return completed

    def _assert_failure(self, completed, code):
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), code)
        self.assertNotIn("events.write_failed", completed.stderr)

    def test_refuses_other_provider_or_unusable_selected_provider(self):
        for config in ({"provider": "disabled"}, {"broken_provider": True}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as folder:
                self._assert_failure(self._run(folder, **config), "guard.not_loaded")

    def test_refuses_missing_or_invalid_database_configuration(self):
        private = "malformed-private-setting"
        for value in (None, private):
            with self.subTest(configured=value is not None), tempfile.TemporaryDirectory() as folder:
                completed = self._run(folder, value)
                self._assert_failure(completed, "config.missing")
                self.assertNotIn(private, completed.stdout + completed.stderr)

    def test_refuses_unreachable_store_without_printing_its_address(self):
        with tempfile.TemporaryDirectory() as folder:
            private = str(Path(folder) / "synthetic-private-socket")
            completed = self._run(folder, make_conninfo(host=private))
            self._assert_failure(completed, "db.unreachable")
            self.assertNotIn(private, completed.stdout + completed.stderr)

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_refuses_store_without_required_project_fact_schema(self):
        fixture = hermes.HermesExplicitMemoryTests()
        with tempfile.TemporaryDirectory() as folder, fixture._database(hermes.MIGRATIONS[:1]) as url:
            self._assert_failure(self._run(folder, url), "db.behind")

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_reports_blocked_schema_query_within_statement_deadline(self):
        fixture = hermes.HermesExplicitMemoryTests()
        with tempfile.TemporaryDirectory() as folder, fixture._database() as url, psycopg.connect(url) as owner:
            owner.execute("lock table cha_final.facts in access exclusive mode")
            self._assert_failure(self._run(folder, url), "db.query")

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_read_only_check_uses_restricted_runtime_role_without_inserts(self):
        fixture = hermes.HermesExplicitMemoryTests()
        with _app_login() as (username, password), fixture._database() as url:
            login = sql.Identifier(username)
            runtime_url = make_conninfo(url, user=username, password=password, options="-c default_transaction_read_only=on")
            with psycopg.connect(url) as owner:
                owner.execute(sql.SQL("grant usage on schema cha_final to {}").format(login))
                owner.execute(sql.SQL("grant select, insert on cha_final.facts, cha_final.project_facts to {}").format(login))
                owner.execute(sql.SQL("grant insert on cha_final.events to {}").format(login))
                owner.execute(sql.SQL("grant usage on sequence cha_final.facts_id_seq to {}").format(login))
            with tempfile.TemporaryDirectory() as folder:
                completed = self._run(folder, runtime_url)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(completed.stdout, "")

            with psycopg.connect(url) as owner:
                self.assertEqual(owner.execute("""
                    select (select count(*) from cha_final.facts),
                           (select count(*) from cha_final.project_facts),
                           (select count(*) from cha_final.events),
                           (select is_called from cha_final.facts_id_seq),
                           (select is_called from cha_final.events_id_seq)
                """).fetchone(), (0, 0, 0, False, False))
                owner.execute(sql.SQL("revoke insert on cha_final.events from {}").format(login))
            with tempfile.TemporaryDirectory() as folder:
                self._assert_failure(self._run(folder, runtime_url), "db.query")

            with psycopg.connect(url) as owner:
                owner.execute(sql.SQL("grant insert on cha_final.events to {}").format(login))
                owner.execute(sql.SQL("revoke usage on sequence cha_final.facts_id_seq from {}").format(login))
            with tempfile.TemporaryDirectory() as folder:
                self._assert_failure(self._run(folder, runtime_url), "db.query")
