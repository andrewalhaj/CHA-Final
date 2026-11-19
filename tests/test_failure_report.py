"""Exercise scheduled failure report output in a disposable Hermes profile."""

import tempfile
import unittest
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

import test_hermes_explicit_memory as hermes
import test_host_heartbeat as heartbeat

PRIVATE_REASON = "private fixture event detail"
INSERT = "insert into cha_final.events (source, code, result, reason, created_at) values (%s, %s, %s, %s, %s)"


@unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
class FailureReportTests(unittest.TestCase):
    def _run(self, directory, url, *, wrapper=False):
        return heartbeat.HostHeartbeatTests()._run(directory, url, wrapper=wrapper, subcommand="report")

    def _attachment(self, run):
        self.assertEqual(run.returncode, 0, run.stderr)
        summary, media = run.stdout.splitlines()
        self.assertTrue(media.startswith("MEDIA:"))
        attachment = Path(media.removeprefix("MEDIA:"))
        self.assertRegex(attachment.name, r"doc_.+_failures-\d{4}-\d{2}-\d{2}\.md")
        rendered = attachment.read_text(encoding="utf-8")
        self.assertNotIn(PRIVATE_REASON, rendered + run.stdout + run.stderr)
        return summary, rendered

    def test_report_counts_only_new_failed_and_degraded_events(self):
        with tempfile.TemporaryDirectory() as directory, hermes.HermesExplicitMemoryTests()._database() as url:
            with psycopg.connect(url) as conn:
                with conn.cursor() as cursor:
                    cursor.executemany(INSERT, [
                        ("source-a", "db.unreachable", "failed", PRIVATE_REASON, "2020-01-01"),
                        ("source-a", "db.unreachable", "failed", PRIVATE_REASON, "2020-01-02"),
                        ("source-b", "recall.timeout", "degraded", PRIVATE_REASON, "2020-01-03"),
                        ("source-a", "host.heartbeat", "ok", PRIVATE_REASON, "2020-01-03"),
                    ])
            summary, report = self._attachment(self._run(directory, url, wrapper=True))
            self.assertIn("3 events", summary)
            self.assertIn("| db.unreachable | source-a | failed | 2 |", report)
            self.assertIn("| recall.timeout | source-b | degraded | 1 |", report)
            self.assertNotIn("host.heartbeat", report)

            # A newly committed event can carry an older timestamp than the report's marker.
            with psycopg.connect(url) as conn:
                conn.execute(INSERT, ("source-a", "unhandled", "failed", PRIVATE_REASON, "2010-01-01"))
            summary, report = self._attachment(self._run(directory, url))
            self.assertIn("1 event", summary)
            self.assertIn("| unhandled | source-a | failed | 1 |", report)
            self.assertNotIn("db.unreachable", report)

            clear = self._run(directory, url)
            self.assertEqual(clear.returncode, 0, clear.stderr)
            self.assertIn("All clear", clear.stdout)
            self.assertEqual(len(clear.stdout.splitlines()), 1)
            self.assertNotIn("MEDIA:", clear.stdout)

    def test_read_only_runtime_cannot_generate_or_advance_report(self):
        with tempfile.TemporaryDirectory() as directory, hermes.HermesExplicitMemoryTests()._database() as url:
            read_only = make_conninfo(url, options="-c default_transaction_read_only=on")
            failure = self._run(directory, read_only)
            self.assertEqual(failure.returncode, 1, failure.stderr)
            self.assertEqual(failure.stdout.strip(), "db.query")
            with psycopg.connect(url) as conn:
                self.assertEqual(conn.execute("select count(*) from cha_final.events").fetchone(), (0,))
