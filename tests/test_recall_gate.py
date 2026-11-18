"""Exercise the recall gate through pinned Hermes turn and result dispatch."""

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

PLUGIN = Path(__file__).resolve().parents[1] / "custom_build"
QUESTION = "What did we decide about the backup?"
ANSWER = "We chose the synthetic backup."
EMPTY_MEMORY = {"matches": []}
EMPTY_SESSION = {"success": True, "results": []}


def scenario():
    from agent.turn_context import _collect_pre_llm_call_context
    from agent.turn_finalizer import apply_llm_output_transform
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from model_tools import _emit_post_tool_call_hook

    discover_plugins()
    manager = get_plugin_manager()
    held = manager.held_transforms()
    active = next((m for name, m in sys.modules.items() if name.endswith(".guards.recall_gate")), None)
    recorder = Mock(wraps=active.failures.record) if active else None
    if active:
        active.failures.record = recorder

    def arm(turn, text, platform):
        agent = SimpleNamespace(session_id="fixture-a", model="fixture", platform=platform, _persist_disabled=False)
        return _collect_pre_llm_call_context(
            agent, effective_task_id=turn, turn_id=turn, original_user_message=text,
            messages=[], conversation_history=[], blocks=[],
        )

    def receipt(turn, tool, result, status=None):
        _emit_post_tool_call_hook(function_name=tool, function_args={}, result=json.dumps(result),
                                 task_id=turn, session_id="fixture-a", turn_id=turn, status=status)

    def reply(turn):
        agent = SimpleNamespace(_hold_stream=bool(held), _held_transforms=held, session_id="fixture-b",
                                model="fixture", platform="cli")
        return apply_llm_output_transform(agent, ANSWER, turn_id=turn)[0]

    outcomes = {"held": bool(held)}
    cases = {
        "none": (),
        "memory-only": (("memory_search", EMPTY_MEMORY),),
        "both": (("memory_search", EMPTY_MEMORY), ("session_search", EMPTY_SESSION)),
        "memory-error": (("session_search", EMPTY_SESSION), ("memory_search", {"error": "db.unreachable"})),
        "session-error": (("memory_search", EMPTY_MEMORY), ("session_search", {"success": False})),
        "blocked": (("memory_search", EMPTY_MEMORY), ("session_search", EMPTY_SESSION, "blocked")),
        "stale": (("memory_search", EMPTY_MEMORY),),
    }
    for turn, receipts in cases.items():
        arm(turn, QUESTION, "cli")
        for tool, result, *status in receipts:
            receipt(turn, tool, result, *status)
        if turn == "stale":
            receipt("previous-turn", "session_search", EMPTY_SESSION)
        outcomes[turn] = reply(turn)

    for turn, message, platform in (
        ("ordinary", "The backup completed.", "cli"),
        ("subagent", QUESTION, "subagent"),
        ("parts", [{"type": "text", "text": QUESTION}], "cli"),
        ("open", "What is still left to do?", "cli"),
    ):
        arm(turn, message, platform)
        outcomes[turn] = reply(turn)
    if active:
        with patch.object(active, "TURNS_KEPT", 1):
            arm("abandoned", QUESTION, "cli")
            invoke_hook("on_session_end", turn_id="abandoned", completed=False, interrupted=True)
            arm("next", QUESTION, "cli")
            outcomes["next"] = reply("next")
    outcomes["codes"] = sorted({args[1] for args, _ in recorder.call_args_list}) if recorder else []
    return outcomes


class RecallGateTests(unittest.TestCase):
    def _run(self, mode):
        with tempfile.TemporaryDirectory(prefix="cha-recall-gate-") as scratch:
            home = Path(scratch)
            plugin = home / "plugins" / "custom_build"
            shutil.copytree(PLUGIN, plugin, ignore=shutil.ignore_patterns("__pycache__"))
            config = plugin / "guards" / "recall_gate.toml"
            if config.exists():
                settings = config.read_text(encoding="utf-8").replace('mode = "log"', f'mode = "{mode}"')
                config.write_text(settings, encoding="utf-8")
            (home / "config.yaml").write_text("plugins:\n  enabled: [custom_build]\n", encoding="utf-8")
            run = subprocess.run(
                [sys.executable, "-B", __file__], cwd=scratch,
                env={"HOME": scratch, "HERMES_HOME": scratch, "PATH": os.defpath},
                capture_output=True, text=True, timeout=30, check=False,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.splitlines()[-1])

    def test_enforcement_requires_both_successful_current_turn_searches(self):
        outcome = self._run("on")
        self.assertTrue(outcome["held"])
        for case in ("both", "ordinary", "subagent"):
            self.assertEqual(outcome[case], ANSWER)
        for case in ("none", "memory-only", "memory-error", "session-error", "blocked", "stale", "parts", "open", "next"):
            with self.subTest(case=case):
                self.assertIn("Recall gate withheld", outcome[case])
        self.assertIn("session_search", outcome["memory-only"])
        self.assertEqual(outcome["codes"], ["recall_gate.armed", "recall_gate.passed", "recall_gate.withheld"])

    def test_log_mode_records_without_changing_output_and_off_disables_the_gate(self):
        for mode, codes in (("log", ["recall_gate.armed", "recall_gate.passed", "recall_gate.would_withhold"]),
                            ("off", [])):
            with self.subTest(mode=mode):
                outcome = self._run(mode)
                self.assertFalse(outcome["held"])
                self.assertEqual(outcome["none"], ANSWER)
                self.assertEqual(outcome["codes"], codes)


if __name__ == "__main__":
    print(json.dumps(scenario()))
