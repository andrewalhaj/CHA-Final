"""Exercise atomic legacy imports on a fresh disposable PostgreSQL database."""

import os
import unittest
from pathlib import Path
from uuid import UUID, uuid4

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TEST_DATABASE_URL = os.environ.get("CHA_FINAL_TEST_DATABASE_URL")
SNAPSHOT = [
    (UUID(int=1), "Legacy snapshot opening", None),
    (UUID(int=2), "Legacy snapshot telescope", "project-a"),
    (UUID(int=3), "Legacy snapshot closing", None),
]


@unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to a disposable database")
class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        marker = MIGRATIONS / "0003_legacy_import.sql"
        self.assertTrue(marker.is_file(), "the legacy import migration is missing")
        database_name = f"cha_final_import_{uuid4().hex}"
        database = sql.Identifier(database_name)
        server = self.enterContext(psycopg.connect(TEST_DATABASE_URL, autocommit=True))
        server.execute(sql.SQL("create database {}").format(database))
        self.addCleanup(server.execute, sql.SQL("drop database {}").format(database))
        self.database_url = make_conninfo(TEST_DATABASE_URL, dbname=database_name)
        self.conn = self.enterContext(psycopg.connect(self.database_url, autocommit=True))
        with self.conn.transaction():
            for name in ("0001_bootstrap.sql", "0002_project_facts.sql", marker.name):
                self.conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))

    def test_snapshot_is_ordered_scoped_and_repeatable(self):
        import psycopg

        from custom_build.memory import legacy_import, store

        self.assertEqual(legacy_import.apply_snapshot(self.conn, SNAPSHOT), 3)
        with psycopg.connect(self.database_url) as observer:
            self.assertEqual(store.recall(observer, "legacy snapshot"), [SNAPSHOT[2][1], SNAPSHOT[0][1]])
            self.assertEqual(store.recall(observer, "legacy snapshot", project="project-a"), [SNAPSHOT[1][1]])
            self.assertEqual(store.recall(observer, "legacy snapshot", project="project-b"), [])
            self.assertEqual(observer.execute("select count(*) from cha_final.legacy_import").fetchone(), (1,))

            # The first runtime still writes and reads projectless facts after the additive migration.
            observer.execute("insert into cha_final.facts (body) values (%s) returning id", ("Legacy snapshot live",))
            rows = observer.execute(
                "select body from cha_final.facts "
                "where to_tsvector('english', body) @@ plainto_tsquery('english', %s) order by id desc limit 8",
                ("legacy snapshot",),
            )
            self.assertEqual(list(rows), [("Legacy snapshot live",), (SNAPSHOT[2][1],), (SNAPSHOT[0][1],)])

        self.assertEqual(legacy_import.apply_snapshot(self.conn, SNAPSHOT), 0)
        source_id, body, project = SNAPSHOT[0]
        for differing in (
            list(reversed(SNAPSHOT)),
            [(UUID(int=4), body, project), *SNAPSHOT[1:]],
            [(source_id, "changed text", project), *SNAPSHOT[1:]],
            [(source_id, body, "project-b"), *SNAPSHOT[1:]],
        ):
            with self.assertRaisesRegex(legacy_import.LegacyImportError, "different snapshot"):
                legacy_import.apply_snapshot(self.conn, differing)
        self.assertEqual(self.conn.execute("select count(*) from cha_final.facts").fetchone(), (3,))
        self.assertEqual(self.conn.execute("select count(*) from cha_final.project_facts").fetchone(), (1,))

    def _assert_refuses_committed_fact(self, project):
        from custom_build.memory import legacy_import, store

        store.save(self.conn, "Legacy snapshot live", project=project)
        with self.assertRaisesRegex(legacy_import.LegacyImportError, "already contains facts"):
            legacy_import.apply_snapshot(self.conn, SNAPSHOT)
        self.assertEqual(store.recall(self.conn, "legacy snapshot", project=project), ["Legacy snapshot live"])
        self.assertEqual(self.conn.execute("select count(*) from cha_final.legacy_import").fetchone(), (0,))

    def test_refuses_uninitialized_target_with_projectless_fact(self):
        self._assert_refuses_committed_fact(None)

    def test_refuses_uninitialized_target_with_scoped_fact(self):
        self._assert_refuses_committed_fact("project-a")

    def test_refuses_a_target_connection_with_a_stale_snapshot(self):
        import psycopg

        from custom_build.memory import legacy_import, store

        with self.conn.transaction():
            self.conn.execute("set transaction isolation level repeatable read")
            self.assertEqual(self.conn.execute("select count(*) from cha_final.facts").fetchone(), (0,))
            with psycopg.connect(self.database_url) as writer:
                store.save(writer, "Runtime fact committed after the snapshot")
            with self.assertRaisesRegex(legacy_import.LegacyImportError, "must be idle"):
                legacy_import.apply_snapshot(self.conn, SNAPSHOT)
        self.assertEqual(self.conn.execute("select count(*) from cha_final.facts").fetchone(), (1,))
        self.assertEqual(self.conn.execute("select count(*) from cha_final.legacy_import").fetchone(), (0,))

    def test_failed_insert_rolls_back_the_snapshot_and_allows_retry(self):
        import psycopg

        from custom_build.memory import legacy_import

        broken = [*SNAPSHOT[:2], (UUID(int=3), "rejected\x00body", None)]
        with self.assertRaises(psycopg.DataError):
            legacy_import.apply_snapshot(self.conn, broken)
        with psycopg.connect(self.database_url) as observer:
            self.assertEqual(observer.execute("select count(*) from cha_final.facts").fetchone(), (0,))
            self.assertEqual(observer.execute("select count(*) from cha_final.project_facts").fetchone(), (0,))
            self.assertEqual(observer.execute("select count(*) from cha_final.legacy_import").fetchone(), (0,))
        self.assertEqual(legacy_import.apply_snapshot(self.conn, SNAPSHOT), 3)
        self.assertEqual(legacy_import.apply_snapshot(self.conn, SNAPSHOT), 0)

    def test_import_waits_for_an_in_flight_runtime_write(self):
        import psycopg

        from custom_build.memory import legacy_import, store

        with psycopg.connect(self.database_url, autocommit=True) as other:
            other.execute("set lock_timeout = '200ms'")
            with self.conn.transaction():
                store.save(self.conn, "Writer has a scoped fact", project="project-a")
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    legacy_import.apply_snapshot(other, SNAPSHOT)
            with self.assertRaisesRegex(legacy_import.LegacyImportError, "already contains facts"):
                legacy_import.apply_snapshot(other, SNAPSHOT)
