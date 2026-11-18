import importlib
import json
import logging
import unittest
from pathlib import Path
from unittest.mock import Mock

import psycopg

SOURCE = "test-agent"
PRIVATE_CONTENT = "synthetic private fact"


class FailureRecordingTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "custom_build/failures.py").is_file(), "the failure recorder is missing")
        self.record = importlib.import_module("custom_build.failures").record

    def test_warning_precedes_insert_and_matches_row(self):
        conn = Mock()

        with self.assertLogs("custom_build.failures") as logs:
            def check_insert(query, params):
                self.assertIn("INSERT INTO cha_final.events", query)
                self.assertEqual(len(logs.records), 1)
                original = json.loads(logs.records[0].getMessage())
                self.assertEqual(params, (SOURCE, "db.unreachable", "failed", original["reason"]))
                self.assertEqual(original, dict(zip(("source", "code", "result", "reason"), params, strict=True)))

            conn.execute.side_effect = check_insert
            self.assertIs(self.record(conn, "db.unreachable", "failed", source=SOURCE), True)

        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        conn.execute.assert_called_once()

    def test_missing_connection_keeps_original_failure_in_local_log(self):
        with self.assertLogs("custom_build.failures") as logs:
            self.assertIs(self.record(None, "config.missing", "failed", source=SOURCE), False)

        self.assert_local_fallback(logs, "config.missing")

    def test_expected_database_errors_keep_original_failure_without_private_content(self):
        for error in (
            psycopg.OperationalError,
            psycopg.errors.InFailedSqlTransaction,
            psycopg.errors.InsufficientPrivilege,
            psycopg.errors.ReadOnlySqlTransaction,
        ):
            with self.subTest(error=error.__name__):
                conn = Mock()
                conn.execute.side_effect = error(PRIVATE_CONTENT)

                with self.assertLogs("custom_build.failures") as logs:
                    self.assertIs(self.record(conn, "unhandled", "degraded", source=SOURCE), False)

                self.assert_local_fallback(logs, "unhandled")
                self.assertNotIn(PRIVATE_CONTENT, repr(conn.execute.call_args))

    def test_unexpected_programming_error_propagates(self):
        conn = Mock()
        conn.execute.side_effect = psycopg.errors.SyntaxError(PRIVATE_CONTENT)

        with self.assertLogs("custom_build.failures") as logs, self.assertRaises(psycopg.errors.SyntaxError):
            self.record(conn, "unhandled", "failed", source=SOURCE)

        self.assertEqual(len(logs.records), 1)
        self.assertEqual(json.loads(logs.records[0].getMessage())["code"], "unhandled")
        self.assertNotIn(PRIVATE_CONTENT, logs.records[0].getMessage())

    def test_unknown_code_and_result_are_rejected_before_writing(self):
        conn = Mock()
        for code, result in ((PRIVATE_CONTENT, "failed"), ("unhandled", PRIVATE_CONTENT)):
            with self.subTest(code=code, result=result):
                with self.assertNoLogs("custom_build.failures", logging.DEBUG), self.assertRaises(ValueError) as raised:
                    self.record(conn, code, result, source=SOURCE)
                self.assertNotIn(PRIVATE_CONTENT, str(raised.exception))

        conn.execute.assert_not_called()

    def assert_local_fallback(self, logs, original_code):
        original, fallback = logs.records
        self.assertEqual(original.levelno, logging.WARNING)
        self.assertEqual(json.loads(original.getMessage())["code"], original_code)
        self.assertEqual(fallback.levelno, logging.ERROR)
        self.assertEqual(json.loads(fallback.getMessage())["code"], "events.write_failed")
        self.assertEqual(json.loads(fallback.getMessage())["failed_code"], original_code)
        self.assertNotIn(PRIVATE_CONTENT, "\n".join(logs.output))
