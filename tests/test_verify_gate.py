"""Prove the verify gate with the pinned Hermes tool and reply dispatchers."""

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
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import test_hermes_explicit_memory

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "custom_build"
GATE_CONFIG = Path("guards/verify_gate.toml")
CLAIM = "The MEMORY.md contains a synthetic assertion."


def _scenario(mode):
    from agent.turn_finalizer import apply_llm_output_transform
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from hermes_logging import setup_logging
    from model_tools import _emit_post_tool_call_hook, handle_function_call

    home = Path(os.environ["HERMES_HOME"])
    setup_logging(hermes_home=home)
    discover_plugins()
    manager = get_plugin_manager()
    loaded = next(plugin for plugin in manager.list_plugins() if plugin["name"] == "custom_build")
    assert loaded["enabled"] and not loaded["error"], loaded

    path = home / "memories" / "MEMORY.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("Original memory line.\n", encoding="utf-8")

    def call(tool, target=path, session="first", **args):
        return json.loads(handle_function_call(
            tool, {"path": str(target), **args}, task_id="verify", session_id=session, turn_id="turn",
        ))

    def reply(session):
        held = manager.held_transforms()
        agent = SimpleNamespace(_hold_stream=bool(held), _held_transforms=held, session_id=session, model="test")
        return apply_llm_output_transform(agent, CLAIM, turn_id="reply")[0]

    original = path.read_text(encoding="utf-8")

    def change(session="first"):
        return call("patch", session=session, old_string="Original memory line.",
                    new_string="Patched memory line.")

    if mode == "one-shot":
        callback = manager.iter_hook_callbacks("pre_tool_call")[0]
        module = sys.modules[callback.__module__.split(".guards.", 1)[0]]
        connecting = threading.Event()
        release = threading.Event()

        def stalled_connection(_url):
            connecting.set()
            release.wait()

        with patch.object(module, "_connect", side_effect=stalled_connection):
            changed = change()
            assert connecting.wait(timeout=5), "the decision did not reach its persistence worker"
        return {"changed": changed}

    result = {"held": bool(manager.held_transforms()), "reply": reply("first")}
    result["first_write"] = change()
    result["unchanged"] = path.read_text(encoding="utf-8") == original
    if mode != "on":
        return result

    result["read"] = call("read_file")
    result["read_reply"] = reply("first")
    result["second_session_write"] = change("second")
    result["allowed_write"] = change()
    result["changed"] = path.read_text(encoding="utf-8").endswith("Patched memory line.\n")
    target = home / "memories" / "USER.md"
    result["failed_read"] = call("read_file", target)
    result["write_after_failed_read"] = call("write_file", target, content="test")
    result["absent"] = not target.exists()
    first = home / "plugins" / "custom_build" / "plugin.yaml"
    other = first.parent / "guard-test-new.txt"
    result["plugin_read"] = call("read_file", first)
    result["sibling_write"] = call("write_file", other, content="test")
    result["sibling_absent"] = not other.exists()
    from hermes_cli.plugins_context import PluginContext

    with patch.object(PluginContext, "state", new_callable=PropertyMock, side_effect=ValueError(CLAIM)):
        result["crash_write"] = change("third")
        result["crash_reply"] = reply("third")
    def terminal_hook(command, *, workdir=home, completed=None):
        args = {"command": command, "workdir": str(workdir)}
        if completed is not None:
            _emit_post_tool_call_hook(
                function_name="terminal", function_args=args, result=json.dumps(completed),
                task_id="verify", session_id="terminal",
            )
            return None
        return manager.invoke_hook(
            "pre_tool_call", tool_name="terminal", args=args, session_id="terminal", task_id="verify",
        )

    result["terminal_unread"] = terminal_hook("touch .env")
    result["terminal_other_path"] = terminal_hook("touch .env.bak")
    result["terminal_cd"] = terminal_hook('cd "$HERMES_HOME" && touch .env', workdir=home.parent)
    result["terminal_parent_relative"] = terminal_hook("touch ../.env", workdir=path.parent)
    result["terminal_stdout"] = terminal_hook("printf test 1>.env")
    result["terminal_combined_stdout"] = terminal_hook("printf test &>.env")
    foreground = {"output": "synthetic fixture", "exit_code": 0, "error": None}
    terminal_hook("printf test 1>.env", completed=foreground)
    result["write_after_redirect"] = terminal_hook("touch .env")
    terminal_hook("cat .env", completed={**foreground, "exit_code": None, "status": "yielded_to_background",
                                         "session_id": "synthetic-process"})
    result["write_after_yield"] = terminal_hook("touch .env")
    terminal_hook("cat .env", completed={**foreground, "session_id": "synthetic-process"})
    result["write_after_background_start"] = terminal_hook("touch .env")
    terminal_hook('cd "$HERMES_HOME" && cd memories && cat .env', workdir=home.parent, completed=foreground)
    result["write_after_ambiguous_cwd"] = terminal_hook("touch .env")
    terminal_hook('cd "$HERMES_HOME"; cat .env', workdir=home.parent, completed=foreground)
    result["write_after_unconditional_cwd"] = terminal_hook("touch .env")
    terminal_hook('cd "$HERMES_HOME" && cat .env', workdir=home.parent, completed=foreground)
    result["write_after_foreground"] = terminal_hook("touch .env")
    result["setting_unread"] = terminal_hook('hermes config set "memory.provider" custom_build')
    terminal_hook("hermes config get memory.provider", completed={**foreground, "exit_code": 1, "error": "failed"})
    result["setting_failed_read"] = terminal_hook("hermes config unset memory.provider")
    terminal_hook("hermes config show", completed={**foreground, "output": "compression.in_place = true"})
    result["setting_after_show_summary"] = terminal_hook("hermes config unset memory.provider")
    terminal_hook("hermes config get 'memory.provider'", completed=foreground)
    result["setting_read"] = terminal_hook("hermes config set memory.provider custom_build")
    return result


class VerifyGateTests(unittest.TestCase):
    def _run(self, mode, database_url=None):
        with tempfile.TemporaryDirectory(prefix="cha-verify-") as scratch:
            home = Path(scratch)
            plugin = home / "plugins" / "custom_build"
            shutil.copytree(PLUGIN, plugin)
            config = plugin / GATE_CONFIG
            if config.exists():
                profile_mode = "log" if mode == "one-shot" else mode
                settings = config.read_text(encoding="utf-8").replace('mode = "log"', f'mode = "{profile_mode}"')
                config.write_text(settings, encoding="utf-8")
            (home / "config.yaml").write_text("plugins:\n  enabled: [custom_build]\n", encoding="utf-8")
            env = {"HOME": scratch, "HERMES_HOME": scratch, "PATH": os.defpath}
            if database_url:
                env["CHA_FINAL_DATABASE_URL"] = database_url
            completed = subprocess.run(
                [sys.executable, "-B", __file__, mode], cwd=scratch, env=env,
                capture_output=True, text=True, timeout=30, check=False,
            )
            log = (home / "logs" / "agent.log").read_text(encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.splitlines()[-1]), log

    def test_enforcement_blocks_unread_changes_and_holds_claims(self):
        result, log = self._run("on")
        self.assertTrue(result["unchanged"], result["first_write"])
        self.assertIn("verify", result["first_write"]["error"].lower())
        self.assertTrue(result["held"])
        self.assertIn("withheld", result["reply"].lower())
        self.assertNotIn(CLAIM, result["reply"])
        self.assertNotIn("error", result["read"])
        self.assertEqual(result["read_reply"], CLAIM)
        self.assertIn("verify", result["second_session_write"]["error"].lower())
        self.assertNotIn("error", result["allowed_write"])
        self.assertTrue(result["changed"])
        self.assertIn("error", result["failed_read"])
        self.assertIn("verify", result["write_after_failed_read"]["error"].lower())
        self.assertTrue(result["absent"])
        self.assertNotIn("error", result["plugin_read"])
        self.assertIn("verify", result["sibling_write"]["error"].lower())
        self.assertTrue(result["sibling_absent"])
        self.assertIn("guard.crash", result["crash_write"]["error"])
        self.assertIn("guard.crash", result["crash_reply"])
        self.assertIn("guard.crash", log)
        self.assertNotIn(CLAIM, log)

    def test_terminal_gate_respects_managed_paths_and_setting_receipts(self):
        result, _ = self._run("on")
        for case in ("terminal_unread", "terminal_cd", "terminal_parent_relative", "terminal_stdout",
                     "terminal_combined_stdout", "write_after_redirect", "write_after_yield",
                     "write_after_background_start", "write_after_ambiguous_cwd", "write_after_unconditional_cwd",
                     "setting_unread",
                     "setting_failed_read", "setting_after_show_summary"):
            with self.subTest(case=case):
                self.assertEqual([item["action"] for item in result[case]], ["block"])
        self.assertEqual(result["terminal_other_path"], [])
        self.assertEqual(result["write_after_foreground"], [])
        self.assertEqual(result["setting_read"], [])

    def test_log_mode_records_while_off_mode_does_not(self):
        logged, log = self._run("log")
        off, off_log = self._run("off")
        for result in (logged, off):
            self.assertFalse(result["held"])
            self.assertEqual(result["reply"], CLAIM)
            self.assertFalse(result["unchanged"])
        self.assertIn("verify_gate.would_block", log)
        self.assertIn("verify_gate.would_withhold", log)
        self.assertIn("events.write_failed", log)
        self.assertNotIn(CLAIM, log)
        self.assertNotIn("verify_gate.", off_log)

    def test_guard_decision_is_logged_before_a_one_shot_process_can_exit(self):
        result, log = self._run("one-shot", "dbname=disposable")
        self.assertTrue(result["changed"]["success"])
        self.assertIn('"code": "verify_gate.would_block"', log)

    @unittest.skipUnless(os.environ.get("CHA_FINAL_TEST_DATABASE_URL"), "requires disposable PostgreSQL")
    def test_decision_events_reach_the_existing_store(self):
        import psycopg

        with test_hermes_explicit_memory.HermesExplicitMemoryTests._database(self) as database_url:
            self._run("log", database_url)
            with psycopg.connect(database_url) as conn:
                rows = conn.execute("SELECT code, result, reason FROM cha_final.events").fetchall()
        self.assertEqual(Counter((code, result) for code, result, _ in rows), {
            ("verify_gate.would_block", "ok"): 1, ("verify_gate.would_withhold", "ok"): 1,
        })
        for _, _, reason in rows:
            self.assertIn("MEMORY.md", reason)
            self.assertNotIn(CLAIM, reason)


if __name__ == "__main__":
    outcome = _scenario(sys.argv[1])
    if sys.argv[1] != "one-shot":
        for worker in threading.enumerate():
            if worker.name == "cha-final-event":
                worker.join(timeout=5)
    print(json.dumps(outcome))
