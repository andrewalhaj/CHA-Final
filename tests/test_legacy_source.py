"""Prove legacy snapshots are read-only and facts are safe to import."""

import hashlib
import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.pq import TransactionStatus

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("CHA_FINAL_TEST_DATABASE_URL")
PROJECT_KEY = hashlib.sha256(b"github.com/Team/Project").hexdigest()
PRIVATE_FACT = "A fixture fact carries a private marker."
PRIVATE_TOKEN = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"


class LegacySourceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "custom_build/memory/legacy_source.py").is_file(), "legacy source reader is missing")
        self.source = importlib.import_module("custom_build.memory.legacy_source")

    def connection(self, *labels):
        conn = MagicMock()
        conn.info.transaction_status = TransactionStatus.IDLE
        conn.execute.return_value.fetchall.return_value = [(uuid4(), PRIVATE_FACT, label) for label in labels]
        return conn

    def test_unmapped_or_invalid_origin_refuses_without_private_diagnostics(self):
        label = "fixture-private-label"
        for origins in (
            {},
            {label: "file:///fixture-private-path"},
            {label: "git@git.example:fixture-private-path"},
            {label: "https://fixture-user:fixture-pass@[fixture-private-host]/project"},
        ):
            with self.subTest(origins=origins):
                with self.assertRaises(self.source.LegacySourceError) as caught:
                    self.source.read_snapshot(self.connection(label), origins)
                for private in (label, "fixture-private-path", "fixture-private-host", "fixture-pass", PRIVATE_FACT):
                    self.assertNotIn(private, str(caught.exception))

    def test_distinct_legacy_labels_cannot_share_a_target_key(self):
        conn = self.connection("old-first", "old-second")
        with self.assertRaisesRegex(self.source.LegacySourceError, "same project key"):
            self.source.read_snapshot(conn, {
                "old-first": "https://github.com/Team/Project.git",
                "old-second": "git@github.com:Team/Project.git",
            })

    def test_read_only_snapshot_requires_an_idle_connection(self):
        conn = self.connection(None)
        conn.info.transaction_status = TransactionStatus.INTRANS
        with self.assertRaisesRegex(self.source.LegacySourceError, "idle"):
            self.source.read_snapshot(conn, {})
        conn.execute.assert_not_called()

    @unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to a disposable PostgreSQL server")
    def test_read_only_snapshot_filters_orders_maps_and_redacts(self):
        database_name = f"cha_legacy_source_{uuid4().hex}"
        database = sql.Identifier(database_name)
        database_url = make_conninfo(TEST_DATABASE_URL, dbname=database_name)
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as server:
            server.execute(sql.SQL("create database {}").format(database))
            try:
                self.check_snapshot(database_url)
            finally:
                server.execute(sql.SQL("drop database {}").format(database))

    def check_snapshot(self, database_url):
        first, second, third, retired, superseded, digest = [UUID(int=value) for value in range(1, 7)]
        with psycopg.connect(database_url) as conn:
            conn.execute("create schema memory")
            conn.execute(
                "create table memory.fixture_facts ("
                "id uuid primary key, body text not null, project text, kind text not null,"
                "created_at timestamptz not null, retired_at timestamptz, superseded_by uuid)",
            )
            # The intended rows are visible only when the reader actually turns on READ ONLY.
            conn.execute(
                "create view memory.facts as select * from memory.fixture_facts "
                "where current_setting('transaction_read_only') = 'on'",
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "insert into memory.fixture_facts values (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (third, "Third", "legacy-scope", "fact", "2025-01-02", None, None),
                        (second, f"Second {PRIVATE_TOKEN}", "legacy-scope", "fact", "2025-01-02", None, None),
                        (first, "First", None, "fact", "2025-01-01", None, None),
                        (retired, "Retired", "unused-retired", "fact", "2025-01-01", "2025-01-02", None),
                        (superseded, "Superseded", "unused-older", "fact", "2025-01-01", None, third),
                        (digest, "Digest", "unused-digest", "digest", "2025-01-01", None, None),
                    ],
                )
            self.assertEqual(conn.execute("select count(*) from memory.facts").fetchone(), (0,))

        with (
            patch.dict(os.environ, {"HERMES_REDACT_SECRETS": "false"}),
            psycopg.connect(database_url) as conn,
        ):
            result = self.source.read_snapshot(conn, {
                "legacy-scope": "git@GitHub.com:Team/Project.git",
                "unused-mapping": "file:///unused",
            })
            self.assertEqual(conn.info.transaction_status, TransactionStatus.IDLE)
        self.assertEqual([(item[0], item[2]) for item in result], [
            (first, None), (second, PROJECT_KEY), (third, PROJECT_KEY),
        ])
        self.assertEqual(result[0][1], "First")
        self.assertTrue(result[1][1].startswith("Second "))
        self.assertNotIn(PRIVATE_TOKEN, result[1][1])
        self.assertEqual(result[2][1], "Third")
