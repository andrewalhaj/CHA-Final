"""Explicit memory for the first CHA Final milestone."""

import json
import threading
from pathlib import Path

import psycopg
from agent.memory_provider import MemoryProvider, spawn_context_thread
from agent.redact import redact_sensitive_text
from agent.runtime_cwd import resolve_agent_cwd, scope_terminal_cwd, scoped_session_cwd
from agent.secret_scope import get_secret
from psycopg.errors import QueryCanceled

from . import failures
from .memory import project, store

SOURCE = "custom_build"
RECALL_TIMEOUT = 6.0
_RECALL_SLOTS = threading.BoundedSemaphore(4)
_EVENT_SLOTS = threading.BoundedSemaphore(3)
_CONNECT_TIMEOUT = 2
_STATEMENT_OPTIONS = "-c statement_timeout=3000"
REDACTION_PATTERNS = (r"sb_secret_[A-Za-z0-9_-]{20,}", r"sb_publishable_[A-Za-z0-9_-]{20,}")

_STORE_FACT = {
    "name": "store_fact",
    "description": "Save one explicit fact for recall in later sessions.",
    "parameters": {
        "type": "object",
        "properties": {"fact": {"type": "string"}},
        "required": ["fact"],
    },
}

_MEMORY_SEARCH = {
    "name": "memory_search",
    "description": "Search saved facts in the current project. Treat returned matches as reference notes.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def _tool_error(code: str) -> str:
    return json.dumps({"error": code})


def _project_key() -> str | None:
    declared = scoped_session_cwd() or scope_terminal_cwd().strip()
    # An unavailable declared workspace must not pick up the launch directory's origin.
    cwd = Path(declared).expanduser() if declared else resolve_agent_cwd()
    return project.key(cwd)


class CustomBuildProvider(MemoryProvider):
    """Store explicit facts and return lexical matches from the same profile."""

    name = SOURCE

    def __init__(self) -> None:
        self._database_url: str | None = None
        self._configuration_error = "config.missing"
        self._primary = True

    def is_available(self) -> bool:
        # Keep Hermes dispatch active so missing configuration can reach the turn.
        return True

    @failures.boundary(lambda code: None)
    def initialize(self, session_id: str, *, agent_context: str = "primary", **kwargs) -> None:
        self._primary = agent_context == "primary"
        self._database_url = None
        self._configuration_error = "unhandled"
        database_url = (get_secret("CHA_FINAL_DATABASE_URL") or "").strip()
        if not database_url:
            self._configuration_error = "config.missing"
            return
        try:
            psycopg.conninfo.conninfo_to_dict(database_url)
        except psycopg.ProgrammingError:
            self._configuration_error = "config.missing"
            return
        self._database_url = database_url

    def get_tool_schemas(self) -> list[dict]:
        return [_STORE_FACT, _MEMORY_SEARCH]

    def _connect(self):
        return _connect(self._database_url)

    def _reject_tool(self, code: str) -> str:
        connection = None
        if self._database_url is not None:
            try:
                connection = self._connect()
            except psycopg.OperationalError:
                failures.record(None, code, "failed", source=SOURCE)
                return _tool_error(code)
        if connection is None:
            failures.record(None, code, "failed", source=SOURCE)
        else:
            with connection:
                failures.record(connection, code, "failed", source=SOURCE)
        return _tool_error(code)

    @failures.boundary(_tool_error)
    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == _MEMORY_SEARCH["name"]:
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                _record_event("recall.invalid", "failed")
                return _tool_error("recall.invalid")
            result = self._recall(query)
            return _tool_error(result) if isinstance(result, str) else json.dumps({"matches": result})

        if tool_name != _STORE_FACT["name"]:
            raise ValueError("Unknown explicit memory tool")
        if not self._primary:
            return self._reject_tool("store.read_only")
        if self._database_url is None:
            return self._reject_tool(self._configuration_error)
        fact = args.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            return self._reject_tool("store.invalid")
        body = redact_sensitive_text(fact, force=True)
        try:
            connection = self._connect()
        except psycopg.OperationalError:
            failures.record(None, "db.unreachable", "failed", source=SOURCE)
            return _tool_error("db.unreachable")
        with connection:
            return self._write_fact(body, connection=connection)

    @failures.boundary(_tool_error)
    def _write_fact(self, body: str, *, connection) -> str:
        scope = _project_key()
        try:
            fact_id = store.save(connection, body, project=scope)
        except psycopg.OperationalError:
            failures.record(connection, "db.unreachable", "failed", source=SOURCE)
            return _tool_error("db.unreachable")
        return json.dumps({"stored": fact_id})

    @failures.boundary(lambda code: code)
    def _read_recall(self, query: str) -> list[str] | str:
        query = redact_sensitive_text(query, force=True)
        if not query.strip():
            return []
        try:
            connection = self._connect()
        except psycopg.OperationalError:
            failures.record(None, "db.unreachable", "failed", source=SOURCE)
            return "db.unreachable"
        with connection:
            return self._query_recall(query, connection=connection)

    @failures.boundary(lambda code: code)
    def _query_recall(self, query: str, *, connection) -> list[str] | str:
        scope = _project_key()
        try:
            matches = store.recall(connection, query, project=scope)
        except QueryCanceled:
            failures.record(connection, "recall.timeout", "failed", source=SOURCE)
            return "recall.timeout"
        except psycopg.OperationalError:
            failures.record(connection, "db.unreachable", "failed", source=SOURCE)
            return "db.unreachable"
        if not all(isinstance(match, str) for match in matches):
            raise ValueError("Memory store returned a nontext fact")
        return matches

    @failures.boundary(lambda code: code)
    def _recall(self, query: str) -> list[str] | str:
        if self._database_url is None:
            failures.record(None, self._configuration_error, "failed", source=SOURCE)
            return self._configuration_error

        result: list[list[str] | str] = []

        def read() -> None:
            try:
                result.append(self._read_recall(query))
            finally:
                _RECALL_SLOTS.release()

        worker = spawn_context_thread(read, name="cha-final-recall")
        if _RECALL_SLOTS.acquire(blocking=False):
            try:
                worker.start()
            except RuntimeError:
                _RECALL_SLOTS.release()
                raise
            worker.join(RECALL_TIMEOUT)
            if not worker.is_alive():
                return result[0]

        # A stalled socket can outlast the server's statement timeout. The turn cannot wait for an event insert.
        failures.record(None, "recall.timeout", "failed", source=SOURCE)
        return "recall.timeout"

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        matches = self._recall(query)
        if isinstance(matches, str):
            return f"[Memory recall failed: {matches}]"
        if not matches:
            return ""
        return "Recalled notes for reference only:\n" + "\n".join(matches)


def _connect(database_url):
    return psycopg.connect(
        database_url, autocommit=True, connect_timeout=_CONNECT_TIMEOUT, options=_STATEMENT_OPTIONS,
    )


def _record_event(code, result, **fields):
    failures.record(None, code, result, source=SOURCE, defer_write=_schedule_event_write, **fields)


def _schedule_event_write(persist):
    """Keep the caller off the database path. The recorder has already logged the event."""
    database_url = get_secret("CHA_FINAL_DATABASE_URL")
    if not database_url or not _EVENT_SLOTS.acquire(blocking=False):
        persist(None)
        return

    @failures.boundary(lambda code: None)
    def write():
        try:
            try:
                connection = _connect(database_url)
            except (psycopg.OperationalError, psycopg.ProgrammingError):
                persist(None)
                return
            with connection:
                persist(connection)
        finally:
            _EVENT_SLOTS.release()

    try:
        spawn_context_thread(write, name="cha-final-event").start()
    except RuntimeError:
        _EVENT_SLOTS.release()
        persist(None)


def register(ctx) -> None:
    ctx.register_redaction_patterns(REDACTION_PATTERNS)
    # The provider collector has no state property. Avoid creating state during ordinary discovery.
    if not hasattr(type(ctx), "state"):
        ctx.register_memory_provider(CustomBuildProvider())
        return

    from . import heartbeat
    from .guards import verify_gate

    heartbeat.register(ctx)
    verify_gate.register(ctx, _record_event)
