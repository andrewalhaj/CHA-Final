"""Exercise the memory schemas on a fresh disposable PostgreSQL database."""

import os
import unittest
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TEST_DATABASE_URL = os.environ.get("CHA_FINAL_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to a fresh disposable database")
class ExplicitStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute((MIGRATIONS / "0001_bootstrap.sql").read_text(encoding="utf-8"))

    def test_save_and_lexically_recall_across_connections(self):
        import psycopg

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

    def test_scoped_facts_are_isolated_from_other_scopes_and_the_old_runtime(self):
        import psycopg

        from custom_build.memory.store import recall, save

        legacy_insert = "insert into cha_final.facts (body) values (%s) returning id"
        legacy_recall = (
            "select body from cha_final.facts "
            "where to_tsvector('english', body) @@ plainto_tsquery('english', %s) "
            "order by id desc limit 8"
        )
        before = "Project scope before the migration"
        after = "Project scope after the migration"
        projectless = "Project scope without a project"
        oak = "Project scope for oak"
        birch = "Project scope for birch"

        with psycopg.connect(TEST_DATABASE_URL) as conn:
            before_id, = conn.execute(legacy_insert, (before,)).fetchone()
            project_migration = MIGRATIONS / "0002_project_facts.sql"
            self.assertTrue(project_migration.is_file(), "the project facts migration is missing")
            conn.execute(project_migration.read_text(encoding="utf-8"))
            oak_id = save(conn, oak, project="oak")
            after_id, = conn.execute(legacy_insert, (after,)).fetchone()
            birch_id = save(conn, birch, project="birch")
            projectless_id = save(conn, projectless, project=None)

        ids = [before_id, oak_id, after_id, birch_id, projectless_id]
        self.assertEqual(ids, sorted(set(ids)))
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            query = "project scope"
            self.assertEqual(recall(conn, query, project="oak"), [oak])
            self.assertEqual(recall(conn, query, project="birch"), [birch])
            self.assertEqual(recall(conn, query, project="fir"), [])
            self.assertEqual(recall(conn, query, project=None), [projectless, after, before])
            self.assertEqual(
                list(conn.execute(legacy_recall, (query,))),
                [(projectless,), (after,), (before,)],
            )
