"""Require a completed lookup before delivering a checkable reply."""

import re
import threading
import tomllib
from pathlib import Path

from agent.message_content import flatten_message_text
from utils import safe_json_loads

from .. import failures

CONFIG = Path(__file__).with_suffix(".toml")
TURNS_KEPT = 1000
REMINDER = (
    "Fact gate: run a lookup before answering this checkable question. "
    "Use web_search or web_extract if the reply gives a procedure or diagnosis."
)


def _first_match(patterns, text):
    return next((rule for rule, pattern in patterns.items() if pattern.search(text)), None)


def _completed(tool_name, result):
    """Hermes reports some background dispatches and nested failures as ok."""
    if tool_name not in {"terminal", "delegate_task", "web_extract"}:
        return result is not None
    payload = safe_json_loads(result) if isinstance(result, str) else result
    if not isinstance(payload, dict) or payload.get("error"):
        return False
    if tool_name == "terminal":
        return payload.get("exit_code") == 0 and "session_id" not in payload
    results = payload.get("results")
    if not isinstance(results, list):
        return False
    if tool_name == "delegate_task":
        return any(isinstance(entry, dict) and entry.get("status") == "completed" for entry in results)
    return any(isinstance(entry, dict) and not entry.get("error") and bool(entry.get("content")) for entry in results)


def register(ctx, record_event):
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    mode = config["mode"]
    if mode not in ("log", "on", "off"):
        raise ValueError("Invalid fact gate mode")
    if mode == "off":
        return
    enforce = mode == "on"
    questions = {rule: re.compile(pattern, re.IGNORECASE) for rule, pattern in config["questions"].items()}
    needs_docs = {rule: re.compile(pattern, re.IGNORECASE) for rule, pattern in config["needs_documentation"].items()}
    lookups = frozenset(config["lookups"])
    documentation = frozenset(config["documentation"])
    turns = {}
    lock = threading.Lock()

    def crash(code, error):
        record_event(code, "failed", diagnostic=error)

    def block_turn(code):
        if enforce:
            return {"action": "block", "message": f"Fact gate blocked the turn because {code}."}
        return None

    @failures.boundary(block_turn, on_error=crash)
    def arm(*, turn_id="", user_message=None, platform="", **kwargs):
        if platform == "subagent":
            return None
        rule = _first_match(questions, flatten_message_text(user_message))
        if rule is None:
            return None
        if not turn_id:
            raise ValueError("A checkable turn is missing its ID")
        with lock:
            if turn_id not in turns and len(turns) >= TURNS_KEPT:
                raise RuntimeError("Too many fact gate turns are pending")
            turns.setdefault(turn_id, (rule, set()))
        record_event("fact_gate.armed", "ok", reason=rule)
        return {"context": REMINDER} if enforce else None

    @failures.boundary(lambda code: None, on_error=crash)
    def count(*, turn_id="", tool_name, status=None, result=None, **kwargs):
        if status != "ok" or tool_name not in lookups or not _completed(tool_name, result):
            return
        with lock:
            if turn := turns.get(turn_id):
                turn[1].add(tool_name)

    @failures.boundary(
        lambda code: f"Fact gate withheld the reply because {code}." if enforce else None,
        on_error=crash,
    )
    def check(*, turn_id="", response_text, **kwargs):
        # The turn ID survives compression even when Hermes rotates the session ID.
        with lock:
            turn = turns.pop(turn_id, None)
        if turn is None:
            return None
        rule, completed = turn
        requirement = None
        if not completed:
            requirement = "lookup"
        elif not (completed & documentation) and (doc_rule := _first_match(needs_docs, response_text or "")):
            requirement = "documentation lookup"
            rule = doc_rule

        if requirement is None:
            record_event("fact_gate.passed", "ok", reason=rule)
            return None
        code = "fact_gate.withheld" if enforce else "fact_gate.would_withhold"
        record_event(code, "ok", reason=f"{rule}: missing {requirement}")
        return f"Fact gate withheld the reply. It needs a {requirement} in this turn." if enforce else None

    ctx.register_hook("pre_llm_call", arm)
    ctx.register_hook("post_tool_call", count)
    ctx.register_hook("transform_llm_output", check, hold_stream=enforce)
