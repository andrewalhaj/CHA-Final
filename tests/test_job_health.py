"""Execute the host-health CLI and cron wrapper in disposable Hermes homes."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import psycopg
import test_hermes_explicit_memory as hermes
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_disposable_rollout import _app_login


@contextmanager
def _store(*, can_read=True):
    fixture = hermes.HermesExplicitMemoryTests()
    with _app_login() as (name, password), fixture._database() as url:
        login = sql.Identifier(name)
        with psycopg.connect(url) as owner:
            owner.execute(sql.SQL("grant usage on schema cha_final to {}").format(login))
            owner.execute(sql.SQL("grant insert on cha_final.events to {}").format(login))
            if can_read:
                owner.execute(sql.SQL("grant select on cha_final.events to {}").format(login))
        yield url, make_conninfo(url, user=name, password=password), name


class JobHealthTests(unittest.TestCase):
    def _run(self, roles, url=None, *, wrapper=False, reject_second=False):
        with tempfile.TemporaryDirectory(prefix="cha-job-health-") as folder:
            home = Path(folder) / "hermes"
            installed = home / "plugins" / "custom_build"
            shutil.copytree(hermes.PLUGIN, installed, ignore=shutil.ignore_patterns("__pycache__"))
            config = {
                "plugins": {"enabled": ["custom_build"], "entries": {"custom_build": {"settings": {"agent_hosts": roles}}}},
            }
            (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
            if url is not None:
                (home / ".env").write_text(f"CHA_FINAL_DATABASE_URL={json.dumps(url)}\n", encoding="utf-8")
            if reject_second:
                with (installed / "failures.py").open("a", encoding="utf-8") as stream:
                    stream.write("""
_original_record = record

def record(connection, code, result, **fields):
    if code == "host.missing" and "stale-role" in fields["reason"]:
        return False
    return _original_record(connection, code, result, **fields)
""")
            if wrapper:
                script = installed / "scripts" / "custom-build-job-health.py"
                self.assertTrue(script.is_file(), "the job-health cron script is missing")
                scripts = home / "scripts"
                scripts.mkdir()
                command = [sys.executable, "-B", shutil.copy2(script, scripts)]
            else:
                command = [sys.executable, "-B", "-m", "hermes_cli.main", "custom-build", "job-health"]
            return subprocess.run(
                command, cwd=folder, env={"HOME": folder, "HERMES_HOME": str(home), "PATH": os.defpath},
                capture_output=True, text=True, timeout=15, check=False,
            )

    def _assert_failure(self, run, code):
        self.assertEqual(run.returncode, 1, run.stderr + run.stdout)
        self.assertEqual(run.stdout.strip(), code)

    def test_bad_settings_fail_without_exposing_values(self):
        unsafe = "fixture unsafe credential / value"
        with tempfile.TemporaryDirectory() as folder:
            absent = make_conninfo(host=str(Path(folder) / "absent-socket"))
            for roles in (None, "single-role", [], ["same", "same"], [unsafe], [True]):
                with self.subTest(roles=roles):
                    run = self._run(roles, absent)
                    self._assert_failure(run, "config.missing")
                    self.assertNotIn(unsafe, run.stdout + run.stderr)
        self._assert_failure(self._run(["one"]), "config.missing")
        self._assert_failure(self._run(["one"], "malformed-fixture"), "config.missing")

    def test_unreachable_store_returns_safe_code(self):
        with tempfile.TemporaryDirectory() as folder:
            private = str(Path(folder) / "synthetic-private-socket")
            run = self._run(["one"], make_conninfo(host=private))
        self._assert_failure(run, "db.unreachable")
        self.assertNotIn(private, run.stdout + run.stderr)

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_wrapper_records_only_missing_and_stale_roles_for_the_checking_identity(self):
        with _store() as (url, runtime_url, name):
            with psycopg.connect(url) as owner:
                owner.execute("""
                    insert into cha_final.events (source, code, result, reason, created_at)
                    values ('fresh-role', 'host.heartbeat', 'ok', 'fixture', now() - interval '23 hours'),
                           ('stale-role', 'host.heartbeat', 'ok', 'fixture', now() - interval '25 hours'),
                           ('stale-role', 'host.heartbeat', 'failed', 'fixture', now()),
                           ('future-role', 'host.heartbeat', 'ok', 'fixture', now() + interval '1 hour')
                """)
            run = self._run(["fresh-role", "stale-role", "missing-role", "future-role"], runtime_url, wrapper=True)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            self.assertEqual(run.stdout, "")
            with psycopg.connect(url) as owner:
                self.assertEqual(owner.execute(
                    "select source, result, reason from cha_final.events where code = 'host.missing' order by id",
                ).fetchall(), [
                    (name, "failed", "Expected a successful heartbeat from stale-role in the last 24 hours."),
                    (name, "failed", "Expected a successful heartbeat from missing-role in the last 24 hours."),
                    (name, "failed", "Expected a successful heartbeat from future-role in the last 24 hours."),
                ])

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_query_failure_has_a_named_outcome(self):
        with _store(can_read=False) as (url, runtime_url, _):
            self._assert_failure(self._run(["one"], runtime_url), "db.query")
            with psycopg.connect(url) as owner:
                self.assertEqual(owner.execute(
                    "select code, result from cha_final.events",
                ).fetchall(), [("db.query", "failed")])

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_blocked_database_query_times_out_with_query_failure(self):
        with _store() as (url, runtime_url, _), psycopg.connect(url) as locker:
            locker.execute("lock table cha_final.events in access exclusive mode")
            self._assert_failure(self._run(["one"], runtime_url), "db.query")
            self.assertEqual(locker.execute("select count(*) from cha_final.events").fetchone(), (0,))

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_missing_events_relation_reports_behind_instead_of_crashing(self):
        fixture = hermes.HermesExplicitMemoryTests()
        with tempfile.TemporaryDirectory() as folder:
            no_schema = Path(folder) / "empty.sql"
            no_schema.write_text("select 1", encoding="utf-8")
            with fixture._database([no_schema]) as url:
                self._assert_failure(self._run(["one"], url), "db.behind")

    @unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_one_rejected_record_rolls_back_all_findings(self):
        with _store() as (url, runtime_url, _):
            run = self._run(["missing-role", "stale-role"], runtime_url, reject_second=True)
            self._assert_failure(run, "events.write_failed")
            with psycopg.connect(url) as owner:
                self.assertEqual(owner.execute("select count(*) from cha_final.events").fetchone(), (0,))
                # The identity sequence survives rollback, proving the actual first insert ran.
                self.assertEqual(owner.execute(
                    "select last_value, is_called from cha_final.events_id_seq",
                ).fetchone(), (1, True))
