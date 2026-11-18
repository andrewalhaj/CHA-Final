"""Exercise the lexical baseline against the pinned Hermes provider and disposable PostgreSQL."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import test_hermes_explicit_memory as hermes

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("CHA_FINAL_TEST_DATABASE_URL")


class LexicalEvalTests(unittest.TestCase):
    def test_a_failed_recall_cannot_earn_abstention_credit(self):
        from evals.lexical import score_case

        cases = {"fact-a": "Brass compass"}
        self.assertEqual(score_case("", cases, None), {"status": "abstained", "fact_ids": []})
        self.assertEqual(
            score_case("[Memory recall failed: db.unreachable]", cases, None),
            {"status": "failed", "code": "db.unreachable", "fact_ids": []},
        )

    def _run(self, url):
        return subprocess.run(
            [sys.executable, "-B", "-m", "evals.lexical"], cwd=ROOT, text=True,
            capture_output=True, check=False, timeout=30,
            env={"PATH": os.defpath, "CHA_FINAL_EVAL_DATABASE_URL": url},
        )

    def test_expected_fact_below_five_contributes_only_to_mrr(self):
        from evals.lexical import summarize

        corpus = {"cases": [{"id": "below-five", "expected": "fact-a"}]}
        results = {"below-five": {"status": "hit", "rank": 6, "fact_ids": ["fact-a"]}}
        report = summarize(corpus, results)
        self.assertEqual(report["recall_at_5"], 0.0)
        self.assertAlmostEqual(report["mrr"], 1 / 6, places=4)

    def test_bad_database_settings_report_only_named_failures(self):
        import tempfile

        from psycopg.conninfo import make_conninfo

        with tempfile.TemporaryDirectory(prefix="cha-eval-test-") as scratch:
            dead_url = make_conninfo(host=str(Path(scratch) / "absent-socket"), dbname="cha_eval")
            for url, code, private_text in (
                ("password='private-invalid-value", "config.missing", "private-invalid-value"),
                (dead_url, "db.unreachable", scratch),
            ):
                with self.subTest(code=code):
                    completed = self._run(url)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertNotIn(private_text, completed.stdout + completed.stderr)
                    self.assertEqual(json.loads(completed.stdout), {"error": code})
                    self.assertIn(code, completed.stderr)

    @unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_pinned_provider_runs_the_checked_in_lexical_baseline(self):
        with hermes.HermesExplicitMemoryTests()._database() as url:
            completed = self._run(url)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        cases = report["cases"]
        self.assertEqual(report["recall_failures"], 0)
        self.assertEqual(cases["ranked-compass"]["fact_ids"], ["spare-compass", "blue-compass"])
        self.assertEqual(cases["ranked-compass"]["rank"], 2)
        self.assertEqual(cases["project-scope"]["fact_ids"], ["harbor-ledger"])
        self.assertEqual(cases["wrong-project"]["status"], "abstained")
        self.assertEqual(cases["absent-note"]["status"], "abstained")
        self.assertEqual(cases["paraphrase"]["status"], "miss")
        self.assertEqual(report["recall_at_5"], 0.6667)
        self.assertEqual(report["mrr"], 0.5)
        self.assertEqual(report["abstention"], {"correct": 2, "total": 2})

    @unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_nonempty_fixture_is_rejected_before_any_facts_are_written(self):
        import psycopg

        with hermes.HermesExplicitMemoryTests()._database() as url:
            with psycopg.connect(url) as conn:
                conn.execute(
                    "insert into cha_final.project_facts (project, body) values ('existing', 'Existing fact')"
                )
            completed = self._run(url)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("The eval database must have an empty cha_final schema", completed.stderr)
            with psycopg.connect(url) as conn:
                self.assertEqual(conn.execute("select body from cha_final.project_facts").fetchall(),
                                 [("Existing fact",)])
                self.assertEqual(conn.execute("select count(*) from cha_final.facts").fetchone(), (0,))
