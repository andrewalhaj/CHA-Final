"""Exercise the lexical baseline against the pinned Hermes provider and disposable PostgreSQL."""

import json
import os
import subprocess
import sys
import unittest

from pathlib import Path


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

    @unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
    def test_pinned_provider_runs_the_checked_in_lexical_baseline(self):
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "evals.lexical"], cwd=ROOT, text=True,
            capture_output=True, check=False, timeout=30,
        )
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
