"""Exercise the fact gate with Hermes's pinned pre-turn, tool, and reply dispatchers."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "custom_build"
CONFIG = Path("guards/fact_gate.toml")
VERIFY_CONFIG = Path("guards/verify_gate.toml")
QUESTION = "How many entries are in the synthetic inventory?"
ANSWER = "There are 42 synthetic entries."
DIAGNOSIS = "The root cause is an expired lease."
PROCEDURE = "1. Open the page.\n2. Check the version."


def _scenario(mode):
    from agent.turn_context import _collect_pre_llm_call_context
    from agent.turn_finalizer import apply_llm_output_transform
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from model_tools import _emit_post_tool_call_hook, handle_function_call

    discover_plugins()
    plugins = get_plugin_manager()
    entry = next(plugin for plugin in plugins.list_plugins() if plugin["name"] == "custom_build")
    assert entry["enabled"] and not entry["error"], entry
    held = plugins.held_transforms()
    home = Path(os.environ["HERMES_HOME"])
    document = home / "inventory.txt"
    document.write_text("42 synthetic entries\n", encoding="utf-8")

    def arm(turn, message=QUESTION, *, platform="cli"):
        agent = SimpleNamespace(session_id="session", model="test", platform=platform, _persist_disabled=False)
        blocks = []
        context = _collect_pre_llm_call_context(
            agent, effective_task_id=f"task-{turn}", turn_id=turn, original_user_message=message,
            messages=[], conversation_history=[], blocks=blocks,
        )
        return context, blocks

    def reply(turn, value=ANSWER, *, session="session"):
        agent = SimpleNamespace(
            _hold_stream=bool(held), _held_transforms=held, session_id=session, model="test", platform="cli",
        )
        return apply_llm_output_transform(agent, value, turn_id=turn)[0]

    def read(turn, path=document, *, session="session"):
        handle_function_call("read_file", {"path": str(path)}, task_id=f"task-{turn}", session_id=session, turn_id=turn)

    def post(turn, tool, result, *, status=None):
        _emit_post_tool_call_hook(
            function_name=tool, function_args={}, result=json.dumps(result),
            task_id=f"task-{turn}", session_id="session", turn_id=turn, status=status,
        )

    detector = next((m for name, m in sys.modules.items() if name.endswith("custom_build.guards.fact_gate")), None)
    recorder = None
    if detector is not None:
        # The disposable process keeps real persistence/fallback behavior under this spy.
        recorder = Mock(wraps=detector.failures.record)
        detector.failures.record = recorder
    def decisions():
        if recorder is None:
            return []
        return [
            {"code": args[1], "reason": fields.get("reason")}
            for args, fields in recorder.call_args_list
            if args[1].startswith(("fact_gate.", "verify_gate.")) or args[1] == "guard.crash"
        ]

    arm("managed")
    managed = reply("managed", "MEMORY.md contains a synthetic assertion.")
    managed_decisions = decisions()
    outcomes = {"held": bool(held), "reminder": arm("bare"), "missing": reply("bare"), "managed": managed,
                "managed_decisions": managed_decisions}
    if detector is not None and mode != "off":
        with patch.object(detector, "flatten_message_text", side_effect=ValueError("private detector failure")):
            outcomes["crash"] = arm("failed-arm")
        arm("failed-transform")
        read("failed-transform")
        with patch.object(detector, "_first_match", side_effect=ValueError("private transform failure")):
            outcomes["crash_reply"] = reply("failed-transform")

    cases = {
        "checked": (lambda: read("checked", session="rotated"), ANSWER),
        "stale": (lambda: read("earlier-turn"), ANSWER),
        "failed": (lambda: read("failed", home / "missing"), ANSWER),
        "blocked": (lambda: post("blocked", "read_file", {}, status="blocked"), ANSWER),
        "terminal-pending": (lambda: post("terminal-pending", "terminal", {"exit_code": 0, "session_id": "bg"}), ANSWER),
        "terminal-done": (lambda: post("terminal-done", "terminal", {"exit_code": 0, "output": "42"}), ANSWER),
        "diagnosis": (lambda: read("diagnosis"), DIAGNOSIS),
        "procedure": (lambda: read("procedure"), PROCEDURE),
        "extract-failed": (lambda: post("extract-failed", "web_extract", {"results": [{"error": "denied"}]}), DIAGNOSIS),
        "delegate-pending": (lambda: post("delegate-pending", "delegate_task", {"status": "dispatched"}), ANSWER),
        "delegate-done": (lambda: post("delegate-done", "delegate_task", {"results": [{"status": "completed"}]}), ANSWER),
        "documented": (lambda: post("documented", "web_search", {"success": True, "data": {"web": []}}), DIAGNOSIS),
    }
    for turn, (lookup, answer) in cases.items():
        arm(turn)
        lookup()
        outcomes[turn] = reply(turn, answer, session="rotated" if turn == "checked" else "session")
    arm("child", platform="subagent")
    outcomes["child"] = reply("child")
    arm("chat", "Thanks, that worked.")
    outcomes["chat"] = reply("chat")
    arm("parts", [{"type": "text", "text": QUESTION}])
    outcomes["parts"] = reply("parts")
    for rule, message in {
        "version": "What version of Hermes runs?",
        "location": "Where is the inventory stored?",
        "state": "Is the gateway running?",
        "value": "What is the default size?",
        "count": QUESTION,
        "support": "Does PostgreSQL support this type?",
        "date": "When did the release ship?",
        "error": "Why did the test fail?",
    }.items():
        arm(rule, message)
        reply(rule)
    outcomes["decisions"] = decisions()
    return outcomes


class FactGateTests(unittest.TestCase):
    def _run(self, mode, *, verify_mode="log"):
        with tempfile.TemporaryDirectory(prefix="cha-fact-") as scratch:
            home = Path(scratch)
            plugin = home / "plugins" / "custom_build"
            shutil.copytree(PLUGIN, plugin, ignore=shutil.ignore_patterns("__pycache__"))
            for path, gate_mode in ((CONFIG, mode), (VERIFY_CONFIG, verify_mode)):
                config = plugin / path
                if config.exists() and gate_mode != "log":
                    settings = config.read_text(encoding="utf-8").replace('mode = "log"', f'mode = "{gate_mode}"')
                    config.write_text(settings, encoding="utf-8")
            (home / "config.yaml").write_text("plugins:\n  enabled: [custom_build]\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-B", __file__, mode], cwd=scratch,
                env={"HOME": scratch, "HERMES_HOME": scratch, "PATH": os.defpath},
                capture_output=True, text=True, timeout=30, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.splitlines()[-1]), completed.stderr

    def test_on_mode_holds_only_unverified_checkable_replies(self):
        outcomes, _ = self._run("on")
        self.assertTrue(outcomes["held"])
        self.assertIn("lookup", outcomes["reminder"][0].lower())
        self.assertEqual(outcomes["reminder"][1], [])
        for turn in ("missing", "stale", "failed", "blocked", "terminal-pending", "extract-failed",
                     "delegate-pending", "parts"):
            with self.subTest(turn=turn):
                self.assertIn("withheld", outcomes[turn])
        for turn in ("diagnosis", "procedure"):
            with self.subTest(turn=turn):
                self.assertIn("documentation lookup", outcomes[turn])
        for turn in ("checked", "terminal-done", "delegate-done", "child", "chat"):
            with self.subTest(turn=turn):
                self.assertEqual(outcomes[turn], ANSWER)
        self.assertEqual(outcomes["documented"], DIAGNOSIS)
        self.assertIn("guard.crash", outcomes["crash"][1][0])
        self.assertIn("guard.crash", outcomes["crash_reply"])
        self.assertIn("fact_gate.withheld", {event["code"] for event in outcomes["decisions"]})

    def test_default_log_records_decisions_without_changing_the_turn(self):
        outcomes, log = self._run("log")
        self.assertFalse(outcomes["held"])
        self.assertEqual(outcomes["reminder"], ["", []])
        self.assertEqual(outcomes["missing"], ANSWER)
        self.assertEqual(outcomes["diagnosis"], DIAGNOSIS)
        decisions = [event for event in outcomes["decisions"] if not event["code"].startswith("verify_gate.")]
        self.assertEqual({event["code"] for event in decisions},
                         {"fact_gate.armed", "fact_gate.would_withhold", "fact_gate.passed", "guard.crash"})
        self.assertEqual(outcomes["crash"], ["", []])
        self.assertEqual(outcomes["crash_reply"], ANSWER)
        armed_by = {event["reason"] for event in decisions if event["code"] == "fact_gate.armed"}
        self.assertEqual(armed_by, {"version", "location", "state", "value", "count", "support", "date", "error"})
        for private in (QUESTION, ANSWER, "private detector failure", "private transform failure", "failed-arm"):
            self.assertNotIn(private, log + json.dumps(decisions))

    def test_off_neither_holds_nor_records(self):
        outcomes, log = self._run("off")
        self.assertFalse(outcomes["held"])
        self.assertEqual(outcomes["missing"], ANSWER)
        self.assertFalse(any(event["code"].startswith("fact_gate.") for event in outcomes["decisions"]))
        self.assertNotIn('"code": "fact_gate.', log)


    def test_mixed_modes_enforce_and_record_each_gates_decision(self):
        for mode, verify_mode, enforcing_gate, observed in (
            ("on", "log", "Fact", {"fact_gate.withheld", "verify_gate.would_withhold"}),
            ("log", "on", "Verify", {"verify_gate.withheld", "fact_gate.would_withhold"}),
        ):
            with self.subTest(mode=mode, verify_mode=verify_mode):
                outcomes, _ = self._run(mode, verify_mode=verify_mode)
                self.assertTrue(outcomes["held"])
                self.assertIn(f"{enforcing_gate} gate withheld", outcomes["managed"])
                self.assertEqual({event["code"] for event in outcomes["managed_decisions"]},
                                 {"fact_gate.armed", *observed})


if __name__ == "__main__":
    print(json.dumps(_scenario(sys.argv[1])))
