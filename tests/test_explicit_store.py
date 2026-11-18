"""Exercise the first memory schema on a fresh disposable PostgreSQL database."""

import os
import unittest
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0001_bootstrap.sql"
TEST_DATABASE_URL = os.environ.get("CHA_FINAL_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to a fresh disposable database")
class ExplicitStoreTests(unittest.TestCase):
    def test_save_and_lexically_recall_across_connections(self):
        import psycopg

        self.assertTrue(MIGRATION.is_file(), "the first store migration is missing")
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))

        from custom_build.memory.store import recall, save

        body = "The harbor keeper's brass compass is tagged cobalt."
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            self.assertIsInstance(save(conn, body), int)
            for serial in range(10):
                save(conn, f"Lighthouse inventory marker {serial}")

        with psycopg.connect(TEST_DATABASE_URL) as conn:
            self.assertEqual(recall(conn, "brass compasses"), [body])
            self.assertEqual(recall(conn, "elephants"), [])
            self.assertEqual(
                recall(conn, "lighthouse inventory"),
                [f"Lighthouse inventory marker {serial}" for serial in range(9, 1, -1)],
            )
            event = conn.execute(
                "insert into cha_final.events (source, code, result, reason)"
                " values (%s, %s, %s, %s) returning id, created_at",
                ("test-machine", "db.unreachable", "failed", "Memory store unavailable"),
            ).fetchone()
            self.assertIsInstance(event[0], int)
            self.assertIsNotNone(event[1])

        with self.assertRaises(psycopg.OperationalError):
            recall(conn, "brass compass")
