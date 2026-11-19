"""Require both Hermes search receipts before answering about prior work."""

import re
import threading
import tomllib
from pathlib import Path

from agent.message_content import flatten_message_text
from utils import safe_json_loads

from .. import failures

CONFIG = Path(__file__).with_suffix(".toml")
SEARCHES = ("memory_search", "session_search")
TURNS_KEPT = 1000
_REMINDER = "Recall gate: run memory_search and session_search in this turn before answering about prior work."


def register(ctx, record_event):
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    mode = config["mode"]
    if mode not in ("log", "on", "off"):
        raise ValueError("Invalid recall gate mode")
    if mode == "off":
        return
    enforce = mode == "on"
    patterns = tuple(
        (f"{kind}.{rule}", re.compile(pattern))
        for kind in ("open_work", "past_work") for rule, pattern in config[kind].items()
    )
    turns = {}
    lock = threading.Lock()

    def crash(code, error):
        record_event(code, "failed", diagnostic=error)

    def block_turn(code):
        if enforce:
            return {"action": "block", "message": f"Recall gate blocked the turn because {code}."}
        return None

    @failures.boundary(block_turn, on_error=crash)
    def arm(*, turn_id="", user_message=None, platform="", **_kwargs):
        if platform == "subagent":
            return None
        text = flatten_message_text(user_message)
        rule = next((label for label, pattern in patterns if pattern.search(text)), None)
        if rule is None:
            return None
        if not turn_id:
            raise ValueError("A recall turn is missing its ID")
        with lock:
            if turn_id not in turns and len(turns) >= TURNS_KEPT:
                raise RuntimeError("Too many recall gate turns are pending")
            turns.setdefault(turn_id, (rule, set()))
        record_event("recall_gate.armed", "ok", reason=rule)
        return {"context": _REMINDER} if enforce else None

    @failures.boundary(lambda code: None, on_error=crash)
    def count(*, turn_id="", tool_name, status=None, result=None, **_kwargs):
        if status != "ok" or tool_name not in SEARCHES:
            return
        payload = safe_json_loads(result) if isinstance(result, str) else result
        if not isinstance(payload, dict) or payload.get("error"):
            return
        if tool_name == "memory_search" and not isinstance(payload.get("matches"), list):
            return
        if tool_name == "session_search" and payload.get("success") is not True:
            return
        with lock:
            turn = turns.get(turn_id)
            if turn is not None:
                turn[1].add(tool_name)

    @failures.boundary(lambda code: f"Recall gate withheld the reply because {code}." if enforce else None,
                       on_error=crash)
    def check(*, turn_id="", **_kwargs):
        with lock:
            turn = turns.pop(turn_id, None)
        if turn is None:
            return None
        rule, completed = turn
        missing = [tool for tool in SEARCHES if tool not in completed]
        if not missing:
            record_event("recall_gate.passed", "ok", reason=rule)
            return None

        missing_names = " and ".join(missing)
        code = "recall_gate.withheld" if enforce else "recall_gate.would_withhold"
        record_event(code, "ok", reason=f"{rule} missing {missing_names}")
        if enforce:
            return f"Recall gate withheld the reply. Run {missing_names} in this turn before answering."
        return None

    @failures.boundary(lambda code: None, on_error=crash)
    def retire(*, turn_id="", **_kwargs):
        with lock:
            turns.pop(turn_id, None)

    ctx.register_hook("pre_llm_call", arm)
    ctx.register_hook("post_tool_call", count)
    ctx.register_hook("transform_llm_output", check, hold_stream=enforce)
    ctx.register_hook("on_session_end", retire)
