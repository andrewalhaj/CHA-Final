"""Prove the write gate through real approval dispatch on disposable files."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import test_hermes_explicit_memory

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CONTENT = "Synthetic private file contents for the approval test."


def _scenario(mode):
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from hermes_logging import setup_logging
    from model_tools import handle_function_call
    from tools import approval, approval_context
    from tools.terminal_tool import set_approval_callback

    home = Path(os.environ["HERMES_HOME"])
    setup_logging(hermes_home=home)
    discover_plugins()
    manager = get_plugin_manager()
    entry = next(plugin for plugin in manager.list_plugins() if plugin["name"] == "custom_build")
    assert entry["enabled"] and not entry["error"], entry

    prompts = []
    choices = iter(("deny", "always", "deny", "once"))

    def answer(target, description, **kwargs):
        prompts.append([target, description])
        return next(choices)

    def edit(path, tool="write_file"):
        args = {"path": str(path), "content": FIXTURE_CONTENT}
        if tool == "patch":
            args = {"path": str(path), "old_string": "Original.", "new_string": FIXTURE_CONTENT}
        result = json.loads(handle_function_call(
            tool, args, task_id="write-fixture", session_id="fixture-session", turn_id="fixture-turn", tool_call_id=path.name,
        ))
        return {"changed": path.exists() and FIXTURE_CONTENT in path.read_text(encoding="utf-8"),
                "allowed": "error" not in result}

    plugin = home / "plugins" / "custom_build"
    original = home / "skills" / "fixture" / "example.txt"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("Original.\n", encoding="utf-8")
    token = approval_context.set_current_session_key("fixture-session")
    set_approval_callback(answer)
    try:
        if mode == "api":
            set_approval_callback(None)
            notices = []

            def notify(request):
                notices.append(request["pattern_key"])
                approval.resolve_gateway_approval("fixture-session", "once", request_id=request["request_id"])

            with patch.dict(os.environ, {"HERMES_SESSION_PLATFORM": "api_server", "HERMES_EXEC_ASK": "1",
                                         "HERMES_INTERACTIVE": ""}):
                offline = edit(plugin / "without-notifier.txt")
                approval.register_gateway_notify("fixture-session", notify)
                try:
                    online = edit(plugin / "with-notifier.txt")
                finally:
                    approval.unregister_gateway_notify("fixture-session")
            return {"offline": offline, "online": online, "notices": notices}

        outcomes = {"first": edit(plugin / "first.txt"), "prompts": prompts}
        if mode == "on":
            outcomes["approved"] = edit(plugin / "approved.txt")
            outcomes["cached"] = edit(plugin / "cached.txt")
            outcomes["different_rule"] = edit(original, "patch")
            with patch.dict(os.environ, {"HERMES_CRON_SESSION": "1"}):
                outcomes["unattended_cached"] = edit(plugin / "unattended.txt")
            with patch.dict(os.environ, {"HERMES_INTERACTIVE": ""}):
                outcomes["without_bridge"] = edit(plugin / "headless.txt")
            outcomes["patch_approved"] = edit(original, "patch")
            set_approval_callback(None)
            outcomes["interactive_marker_only"] = edit(plugin / "fake-interactive.txt")
        return outcomes
    finally:
        set_approval_callback(None)
        approval_context.reset_current_session_key(token)


class WriteGateTests(unittest.TestCase):
    def _run(self, mode, *, approvals=None, database_url=None):
        with tempfile.TemporaryDirectory(prefix="cha-write-gate-") as scratch:
            home = Path(scratch)
            plugin = home / "plugins" / "custom_build"
            shutil.copytree(ROOT / "custom_build", plugin)
            config = plugin / "guards" / "write_gate.toml"
            if config.exists():
                configured = "on" if mode in ("bypass", "api") else mode
                text = config.read_text(encoding="utf-8")
                config.write_text(text.replace('mode = "log"', f'mode = "{configured}"'), encoding="utf-8")
            (home / "config.yaml").write_text(json.dumps({
                "plugins": {"enabled": ["custom_build"]},
                "approvals": approvals or {"mode": "manual"},
            }), encoding="utf-8")
            env = {"HOME": scratch, "HERMES_HOME": scratch, "HERMES_INTERACTIVE": "1", "PATH": os.defpath}
            if database_url:
                env["CHA_FINAL_DATABASE_URL"] = database_url
            completed = subprocess.run(
                [sys.executable, "-B", __file__, mode], cwd=scratch, env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            log = (home / "logs" / "agent.log").read_text(encoding="utf-8")
        return json.loads(completed.stdout.splitlines()[-1]), log

    def test_enforcement_obeys_real_approval_and_keeps_remembered_allow_rules_scoped(self):
        outcome, log = self._run("on")
        for denied in ("first", "different_rule", "unattended_cached", "without_bridge", "interactive_marker_only"):
            self.assertEqual(outcome[denied], {"changed": False, "allowed": False}, denied)
        for allowed in ("approved", "cached", "patch_approved"):
            self.assertEqual(outcome[allowed], {"changed": True, "allowed": True}, allowed)
        self.assertEqual(len(outcome["prompts"]), 4)
        self.assertIn('"code": "write_gate.always"', log)
        self.assertIn('"code": "write_gate.denied"', log)
        self.assertNotIn(FIXTURE_CONTENT, log)
        self.assertNotIn(FIXTURE_CONTENT, repr(outcome["prompts"]))

    def test_api_server_requires_live_approval_notifier(self):
        outcome, log = self._run("api")
        self.assertEqual(outcome["offline"], {"changed": False, "allowed": False})
        self.assertEqual(outcome["online"], {"changed": True, "allowed": True})
        self.assertEqual(outcome["notices"], ["plugin_rule:custom_build.write_gate.plugin"])
        self.assertIn('"code": "write_gate.approved"', log)

    def test_bypass_settings_block_without_prompting(self):
        for settings in ({"mode": "off"}, {"cron_mode": "approve"}, {"single_query_mode": "approve"},
                         {"unattended_mode": "approve"}):
            with self.subTest(settings=settings):
                outcome, log = self._run("bypass", approvals=settings)
                self.assertEqual(outcome["first"], {"changed": False, "allowed": False})
                self.assertEqual(outcome["prompts"], [])
                self.assertIn('"code": "write_gate.blocked"', log)

    def test_log_and_off_permit_edit_without_prompting(self):
        for mode in ("log", "off"):
            with self.subTest(mode=mode):
                outcome, log = self._run(mode)
                self.assertEqual(outcome["first"], {"changed": True, "allowed": True})
                self.assertEqual(outcome["prompts"], [])
                self.assertEqual('"code": "write_gate.would_ask"' in log, mode == "log")
                self.assertNotIn(FIXTURE_CONTENT, log)

    @unittest.skipUnless(os.environ.get("CHA_FINAL_TEST_DATABASE_URL"), "requires disposable PostgreSQL")
    def test_logs_a_sanitized_decision_to_the_existing_store(self):
        import psycopg

        with test_hermes_explicit_memory.HermesExplicitMemoryTests._database(self) as database_url:
            self._run("log", database_url=database_url)
            with psycopg.connect(database_url) as conn:
                rows = conn.execute("SELECT code, result, reason FROM cha_final.events").fetchall()
        self.assertEqual(Counter((code, result) for code, result, _ in rows), {
            ("verify_gate.would_block", "ok"): 1, ("write_gate.would_ask", "ok"): 1,
        })
        self.assertNotIn(FIXTURE_CONTENT, repr(rows))


if __name__ == "__main__":
    observed = _scenario(sys.argv[1])
    for worker in threading.enumerate():
        if worker.name == "cha-final-event":
            worker.join(timeout=5)
    print(json.dumps(observed))
