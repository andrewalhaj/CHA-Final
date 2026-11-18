"""Prove stalled recall and unexpected writes reach the Hermes caller safely."""

import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_TEST_DSN = "postgresql://cha_fixture.invalid/cha_final_test"


class ProviderBoundaryTests(unittest.TestCase):
    def setUp(self):
        plugin = Path(__file__).resolve().parents[1] / "custom_build" / "plugin.yaml"
        self.assertTrue(plugin.is_file(), "the custom_build plugin manifest is missing")

    def test_stalled_recall_returns_and_records_before_worker_releases(self):
        import custom_build

        provider = custom_build.CustomBuildProvider()
        provider._database_url = _TEST_DSN
        connection = MagicMock()
        connection.__enter__.return_value = connection
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def stalled_recall(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            finished.set()
            return ["A fact that arrived too late."]

        record = custom_build.failures.record
        with (
            patch("psycopg.connect", return_value=connection),
            patch.object(custom_build, "RECALL_TIMEOUT", 0.25),
            patch.object(custom_build.store, "recall", side_effect=stalled_recall),
            patch.object(custom_build.failures, "record", wraps=record) as recorded,
            self.assertLogs(custom_build.failures.__name__, level="WARNING") as logs,
        ):
            try:
                note = provider.prefetch("brass compass", session_id="boundary-recall")
                self.assertTrue(started.is_set(), "the test did not reach the stalled query")
                self.assertFalse(finished.is_set(), "the caller waited for the query")
                self.assertIn("recall.timeout", note)
                self.assertNotIn("A fact that arrived too late.", note)
                recorded_events = [
                    inspect.signature(record).bind(*call.args, **call.kwargs).arguments
                    for call in recorded.call_args_list
                ]
                self.assertTrue(any(
                    event["conn"] is None and event["code"] == "recall.timeout" and event["result"] == "failed"
                    for event in recorded_events
                ))
            finally:
                release.set()
                if started.is_set():
                    self.assertTrue(finished.wait(timeout=2), "the fixture worker did not finish")

        self.assertIn("recall.timeout", "\n".join(logs.output))

    def test_unexpected_write_returns_only_the_safe_failure_code(self):
        import custom_build

        provider = custom_build.CustomBuildProvider()
        provider._database_url = _TEST_DSN
        connection = MagicMock()
        connection.__enter__.return_value = connection
        payload = "private fixture sentence in an unexpected exception"
        with (
            patch("psycopg.connect", return_value=connection),
            patch.object(custom_build.store, "save", side_effect=RuntimeError(payload)) as save,
            self.assertLogs(custom_build.failures.__name__, level="WARNING") as logs,
        ):
            response = provider.handle_tool_call("store_fact", {"fact": "A fixture fact."}, session_id="boundary-tool")

        save.assert_called_once()
        self.assertEqual(json.loads(response).get("error"), "unhandled")
        diagnostics = "\n".join(logs.output)
        self.assertIn("unhandled", diagnostics)
        self.assertNotIn(payload, diagnostics)
        self.assertNotIn(payload, response)

    def test_explicit_write_from_subagent_does_not_save(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        import custom_build

        provider = custom_build.CustomBuildProvider()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with (
            tempfile.TemporaryDirectory(prefix="cha-subagent-") as home,
            patch("psycopg.connect", return_value=connection),
            patch.object(custom_build.store, "save") as save,
        ):
            token = set_secret_scope({"CHA_FINAL_DATABASE_URL": _TEST_DSN}, profile_home=home)
            try:
                provider.initialize("boundary-subagent", hermes_home=home, agent_context="subagent")
                response = provider.handle_tool_call(
                    "store_fact", {"fact": "A fixture fact."}, session_id="boundary-subagent",
                )
            finally:
                reset_secret_scope(token)

        save.assert_not_called()
        self.assertIn("error", json.loads(response))

    def test_malformed_database_setting_is_reported_before_connecting(self):
        import psycopg

        import custom_build

        provider = custom_build.CustomBuildProvider()
        with (
            patch.object(custom_build, "get_secret", return_value="synthetic invalid connection setting"),
            patch("psycopg.connect", side_effect=psycopg.ProgrammingError("synthetic invalid connection setting"))
            as connect,
            self.assertLogs(custom_build.failures.__name__, level="WARNING") as logs,
        ):
            provider.initialize("malformed-profile")
            note = provider.prefetch("brass compass", session_id="malformed-profile")

        connect.assert_not_called()
        self.assertIn("config.missing", note)
        self.assertNotIn("synthetic invalid connection setting", "\n".join(logs.output))
