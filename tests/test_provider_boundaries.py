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
        connection.execute.assert_called_once()
        _, values = connection.execute.call_args.args
        self.assertIn("unhandled", values)
        self.assertNotIn(payload, repr(values))

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
        self.assertEqual(json.loads(response), {"error": "store.read_only"})
        connection.execute.assert_called_once()

    def test_blank_fact_records_rejection_when_store_is_available(self):
        import custom_build

        provider = custom_build.CustomBuildProvider()
        provider._database_url = _TEST_DSN
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with (
            patch("psycopg.connect", return_value=connection),
            patch.object(custom_build.store, "save") as save,
            self.assertLogs(custom_build.failures.__name__, level="WARNING") as logs,
        ):
            response = provider.handle_tool_call("store_fact", {"fact": " "})

        save.assert_not_called()
        self.assertEqual(json.loads(response), {"error": "store.invalid"})
        connection.execute.assert_called_once()
        self.assertNotIn("events.write_failed", "\n".join(logs.output))

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

    def test_unavailable_session_workspace_cannot_fall_back_to_another_project(self):
        from agent.runtime_cwd import reset_session_cwd, set_session_cwd

        import custom_build

        provider = custom_build.CustomBuildProvider()
        provider._database_url = _TEST_DSN
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with (
            tempfile.TemporaryDirectory(prefix="cha-scope-") as home,
            patch("psycopg.connect", return_value=connection),
            patch.object(custom_build.store, "save", return_value=1) as save,
            patch.object(custom_build.store, "recall", return_value=["Another workspace's fact."]) as recall,
        ):
            token = set_session_cwd(str(Path(home) / "workspace-not-mounted"))
            try:
                response = provider.handle_tool_call("store_fact", {"fact": "A scoped fact."})
                note = provider.prefetch("workspace fact")
            finally:
                reset_session_cwd(token)

        save.assert_not_called()
        recall.assert_not_called()
        self.assertEqual(json.loads(response), {"error": "unhandled"})
        self.assertIn("unhandled", note)
        self.assertNotIn("Another workspace's fact.", note)

    def test_invalid_search_logs_and_returns_while_event_database_connects(self):
        import custom_build

        provider = custom_build.CustomBuildProvider()
        provider._database_url = _TEST_DSN
        connection = MagicMock()
        connection.__enter__.return_value = connection
        started = threading.Event()
        release = threading.Event()
        persisted = threading.Event()
        result = []
        connection.execute.side_effect = lambda *_args: persisted.set()

        def stalled_connect(*_args, **_kwargs):
            started.set()
            release.wait(timeout=5)
            return connection

        caller = threading.Thread(
            target=lambda: result.append(provider.handle_tool_call("memory_search", {"query": " "})),
            daemon=True,
        )
        with (
            patch.object(custom_build, "get_secret", return_value=_TEST_DSN),
            patch("psycopg.connect", side_effect=stalled_connect),
            self.assertLogs(custom_build.failures.__name__, level="WARNING") as logs,
        ):
            try:
                caller.start()
                self.assertTrue(started.wait(timeout=2))
                caller.join(timeout=0.5)
                self.assertFalse(caller.is_alive(), "invalid query waited for the event store")
                self.assertEqual([json.loads(response) for response in result], [{"error": "recall.invalid"}])
                self.assertIn("recall.invalid", "\n".join(logs.output))
            finally:
                release.set()
                caller.join(timeout=2)
                self.assertTrue(persisted.wait(timeout=2))

    def test_nontext_database_matches_report_a_named_failure_to_both_callers(self):
        import custom_build

        provider = custom_build.CustomBuildProvider()
        provider._database_url = _TEST_DSN
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with (
            patch("psycopg.connect", return_value=connection),
            patch.object(custom_build.store, "recall", return_value=[b"private undecoded body"]),
            self.assertLogs(custom_build.failures.__name__, level="WARNING") as logs,
        ):
            self.assertEqual(provider.prefetch("brass compass"), "[Memory recall failed: unhandled]")
            response = provider.handle_tool_call("memory_search", {"query": "brass compass"})

        self.assertEqual(json.loads(response), {"error": "unhandled"})
        self.assertEqual(connection.execute.call_count, 2)
        self.assertNotIn("private undecoded body", "\n".join(logs.output))
