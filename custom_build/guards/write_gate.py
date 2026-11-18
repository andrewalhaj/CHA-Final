"""Require Hermes human approval for protected actions."""

import re
import tomllib
from pathlib import Path

from agent.tool_dispatch_helpers import _extract_file_mutation_targets
from hermes_constants import get_hermes_home
from tools import approval, approval_context
from tools.file_tools_paths import _resolve_path_for_task

from .. import failures
from .verify_gate import _command_names, _profile_change

CONFIG = Path(__file__).with_suffix(".toml")
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
RULE_PREFIX = "custom_build.write_gate."
APPROVAL_PATTERN_PREFIX = f"plugin_rule:{RULE_PREFIX}"
UNATTENDED_MODES = ("cron_mode", "single_query_mode", "unattended_mode")
RESPONSES = {
    "once": "write_gate.approved",
    "session": "write_gate.approved",
    "always": "write_gate.always",
    "deny": "write_gate.denied",
    "timeout": "write_gate.unanswered",
    "cancelled": "write_gate.unanswered",
    "notify_failed": "write_gate.unanswered",
}


def _matches(rules, tool_name, args, task_id):
    home = get_hermes_home()
    paths = {name: PLUGIN_ROOT if path == "{plugin}" else home / path
             for name, (path, _) in rules.items() if path}
    if tool_name in ("write_file", "patch"):
        targets = [Path(_resolve_path_for_task(path, task_id)).resolve()
                   for path in _extract_file_mutation_targets(tool_name, args)]
        return tuple(name for name, path in paths.items()
                     if any(target.is_relative_to(path.resolve()) for target in targets))

    if tool_name != "terminal" or not isinstance(command := args.get("command"), str):
        return ()
    command = re.sub(r"/(?:\./)+|//+", "/", command)
    cwd = Path(_resolve_path_for_task(args.get("workdir") or ".", task_id))
    profile_change = _profile_change(command, home) is not None
    return tuple(
        name for name, (_, pattern) in rules.items()
        if (name in paths and _command_names(paths[name], home, cwd, command, profile_change=profile_change))
        or (pattern and pattern.search(command))
    )


def _blocking_reason():
    # Hermes checks remembered approvals before checking whether anyone can answer.
    if approval._yolo_active() or approval.is_approval_bypass_active():
        return "approval bypass is active"
    if any(approval_context._binary_approval_mode(key) == "approve" for key in UNATTENDED_MODES):
        return "an unattended approval setting allows protected actions"
    if approval_context._no_user_can_answer():
        return "this session is unattended"
    _, interactive, gateway, ask = approval._presence()
    if not interactive and not (
        (gateway or ask) and approval._gateway_notify_cb(approval_context.get_current_session_key())
    ):
        return "no human approval channel is available"
    return None


def register(ctx, record_event):
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    mode = config["mode"]
    if mode not in ("on", "log", "off"):
        raise ValueError("Invalid write gate mode")
    if mode == "off":
        return

    rules = {
        name: (rule.get("path"), re.compile(pattern) if (pattern := rule.get("command")) else None)
        for name, rule in config["rules"].items()
    }
    enforce = mode == "on"

    def block(message):
        return {"action": "block", "message": message} if enforce else None

    def guard_crash(code, error):
        record_event(code, "failed", diagnostic=error)

    @failures.boundary(lambda code: block(f"Write gate blocked the action because {code}."), on_error=guard_crash)
    def before(*, tool_name, args, task_id="", **kwargs):
        matched = _matches(rules, tool_name, args, task_id or "default")
        if not matched:
            return None
        label = ", ".join(matched)
        if reason := _blocking_reason():
            record_event("write_gate.blocked" if enforce else "write_gate.would_block", "ok",
                         reason=f"{label}: {reason}")
            return block(f"Write gate blocked {label}: {reason}.")
        record_event("write_gate.requested" if enforce else "write_gate.would_ask", "ok", reason=label)
        if enforce:
            return {"action": "approve", "message": f"Write gate requires approval for {label}.",
                    "rule_key": RULE_PREFIX + "+".join(matched)}
        return None

    @failures.boundary(lambda code: None, on_error=guard_crash)
    def after(*, pattern_key="", choice="", **kwargs):
        if not isinstance(pattern_key, str) or not pattern_key.startswith(APPROVAL_PATTERN_PREFIX):
            return
        if not isinstance(choice, str) or choice not in RESPONSES:
            return
        matched = pattern_key.removeprefix(APPROVAL_PATTERN_PREFIX).split("+")
        if all(name in rules for name in matched):
            record_event(RESPONSES[choice], "ok", reason=f"{', '.join(matched)}: {choice}")

    ctx.register_hook("pre_tool_call", before)
    ctx.register_hook("post_approval_response", after)
