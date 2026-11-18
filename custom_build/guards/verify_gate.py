"""Require a successful read before the session changes or describes a managed file."""

import os
import re
import threading
import tomllib
from pathlib import Path

from agent.tool_dispatch_helpers import _extract_file_mutation_targets
from hermes_constants import get_hermes_home
from tools.file_tools_paths import _resolve_path_for_task

from .. import failures

CONFIG = Path(__file__).with_suffix(".toml")
SESSIONS_KEPT = 100
CONFIG_COMMAND = re.compile(r"\bhermes(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+config\s+(set|unset|get|show)\b(?:\s+(\S+))?")
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n")
STATE_LOCK = threading.Lock()


def _whole(pattern, text):
    return re.search(rf"(?<![\w.-]){pattern}(?![\w.-])", text) is not None


def _paths(files, task_id, target):
    resolved = Path(_resolve_path_for_task(target, task_id or "default")).resolve()
    for path in files.values():
        if resolved.is_relative_to(path.resolve()):
            yield path, str(resolved)


def _command_names(path, home, cwd, command):
    """Match a whole managed path, as written from the task's cwd, home or Hermes profile."""
    command = re.sub(r"/(?:\./)+|//+", "/", command)
    names = {str(path), str(path.resolve())}
    alternatives = [re.escape(name) for name in names]
    task_prefix = r"(?<![/~])(?:\./)?"
    home_prefix = r"(?:~|\$HOME|\$\{HOME\})[\x22\x27]?/"
    profile_variable = r"\$(?:HERMES_HOME|\{HERMES_HOME\})"
    bases = [(cwd, task_prefix), (Path.home(), home_prefix), (home, profile_variable + r"[\x22\x27]?/")]
    profile_names = {re.escape(str(home)), re.escape(str(home.resolve())), profile_variable}
    for base in {Path.home(), Path.home().resolve()}:
        profile_names.add(home_prefix + re.escape(os.path.relpath(home, base)))
    profile = "|".join(sorted(profile_names))
    if re.search(rf"(?:^|[;&|])\s*cd\s+(?:--\s+)?['\x22]?(?:{profile})['\x22]?\s*(?:[;&|]|\n)", command):
        bases.append((home, task_prefix))
    for base, prefix in bases:
        for base_path in {base, base.resolve()}:
            for name in names:
                alternatives.append(prefix + re.escape(os.path.relpath(name, base_path)))
    return any(_whole(pattern, command) for pattern in alternatives)


def _terminal_matches(config, files, command, cwd, home):
    writing = bool(config["writes"].search(command))
    for rule, path in files.items():
        if _command_names(path, home, cwd, command):
            yield writing, rule, path
    for verb, key in CONFIG_COMMAND.findall(command):
        if verb == "show":
            yield from ((False, setting, None) for setting in config["settings"])
        elif key in config["settings"]:
            yield verb != "get", key, None


def _decision_paths(config, home, tool_name, args, task_id, *, writing):
    files = {rule: home / name for rule, name in config["files"].items()}
    if writing and tool_name in ("write_file", "patch"):
        for target in _extract_file_mutation_targets(tool_name, args):
            for path, resolved in _paths(files, task_id, target):
                yield path.name, resolved, str(path.resolve())
    elif not writing and tool_name == "read_file" and isinstance(target := args.get("path"), str):
        for path, resolved in _paths(files, task_id, target):
            yield path.name, resolved, None
    elif tool_name == "terminal" and isinstance(command := args.get("command"), str):
        cwd = Path(_resolve_path_for_task(args.get("workdir") or ".", task_id or "default"))
        for writes, rule, path in _terminal_matches(config, files, command, cwd, home):
            if writes == writing:
                yield (path.name, None, str(path.resolve())) if path else (rule, None, None)


def _seen(reads, label, exact, root):
    if label in reads:
        return True
    if exact is not None:
        return exact in reads
    return root is not None and any(Path(item).is_relative_to(root) for item in reads)


def register(ctx, record_event):
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    mode = config["mode"]
    if mode not in ("on", "log", "off"):
        raise ValueError("Invalid verify gate mode")
    if mode == "off":
        return
    config["writes"] = re.compile(config["writes"])
    config["contents"] = re.compile(config["contents"])
    enforce = mode == "on"

    def reads_for(session_id):
        return set(ctx.state.get("verify_reads", {}).get(session_id, [])) if session_id else set()

    def block(message):
        return {"action": "block", "message": message} if enforce else None

    def guard_crash(code, error):
        record_event(code, "failed", diagnostic=error)

    @failures.boundary(lambda code: block(f"Verify gate blocked the tool because {code}."), on_error=guard_crash)
    def before(*, tool_name, args, session_id="", task_id="", **kwargs):
        home = get_hermes_home()
        changes = list(_decision_paths(config, home, tool_name, args, task_id, writing=True))
        if not changes:
            return None
        reads = reads_for(session_id)
        for label, exact, root in changes:
            if not _seen(reads, label, exact, root):
                record_event("verify_gate.blocked" if enforce else "verify_gate.would_block", "ok", reason=label)
                return block(f"Verify gate blocked this change. Read {label} first in this session.")
        return None

    @failures.boundary(lambda code: None, on_error=guard_crash)
    def after(*, tool_name, args, session_id="", task_id="", status=None, **kwargs):
        if status != "ok" or not session_id:
            return
        reads = {exact or label for label, exact, _ in _decision_paths(
            config, get_hermes_home(), tool_name, args, task_id, writing=False,
        )}
        if reads:
            with STATE_LOCK:
                sessions = ctx.state.get("verify_reads", {})
                sessions[session_id] = sorted(set(sessions.pop(session_id, [])) | reads)
                while len(sessions) > SESSIONS_KEPT:
                    del sessions[next(iter(sessions))]
                ctx.state.set("verify_reads", sessions)

    @failures.boundary(
        lambda code: f"Verify gate withheld the reply because {code}." if enforce else None,
        on_error=guard_crash,
    )
    def reply(*, response_text, session_id="", **kwargs):
        claims = [text for text in SENTENCES.split(response_text or "") if config["contents"].search(text)]
        if not claims:
            return None
        home = get_hermes_home()
        named = [home / name for name in config["files"].values()
                 if any(_whole(re.escape(Path(name).name), text) for text in claims)]
        if not named:
            return None
        reads = reads_for(session_id)
        for path in named:
            if not _seen(reads, path.name, None, str(path.resolve())):
                record_event("verify_gate.withheld" if enforce else "verify_gate.would_withhold", "ok", reason=path.name)
                if enforce:
                    return f"Verify gate withheld the reply. Read {path.name} first in this session."
        return None

    ctx.register_hook("pre_tool_call", before)
    ctx.register_hook("post_tool_call", after)
    ctx.register_hook("transform_llm_output", reply, hold_stream=enforce)
