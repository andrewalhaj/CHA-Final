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


def _recall_note(code: str) -> str:
    return f"[Memory recall failed: {code}]"


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
        return [_STORE_FACT]

    def _connect(self):
        return psycopg.connect(
            self._database_url, autocommit=True, connect_timeout=_CONNECT_TIMEOUT, options=_STATEMENT_OPTIONS,
        )

    def _reject_fact(self, code: str) -> str:
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
        if tool_name != _STORE_FACT["name"]:
            raise ValueError("Unknown explicit memory tool")
        if not self._primary:
            return self._reject_fact("store.read_only")
        if self._database_url is None:
            return self._reject_fact(self._configuration_error)
        fact = args.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            return self._reject_fact("store.invalid")
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

    @failures.boundary(_recall_note)
    def _read_recall(self, query: str) -> str:
        query = redact_sensitive_text(query, force=True)
        if not query.strip():
            return ""
        try:
            connection = self._connect()
        except psycopg.OperationalError:
            failures.record(None, "db.unreachable", "failed", source=SOURCE)
            return _recall_note("db.unreachable")
        with connection:
            return self._query_recall(query, connection=connection)

    @failures.boundary(_recall_note)
    def _query_recall(self, query: str, *, connection) -> str:
        scope = _project_key()
        try:
            matches = store.recall(connection, query, project=scope)
        except QueryCanceled:
            failures.record(connection, "recall.timeout", "failed", source=SOURCE)
            return _recall_note("recall.timeout")
        except psycopg.OperationalError:
            failures.record(connection, "db.unreachable", "failed", source=SOURCE)
            return _recall_note("db.unreachable")
        if not matches:
            return ""
        return "Recalled notes for reference only:\n" + "\n".join(matches)

    @failures.boundary(_recall_note)
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._database_url is None:
            failures.record(None, self._configuration_error, "failed", source=SOURCE)
            return _recall_note(self._configuration_error)

        result: list[str] = []

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
        return _recall_note("recall.timeout")


def register(ctx) -> None:
    ctx.register_redaction_patterns(REDACTION_PATTERNS)
    # The provider collector has no state property. Avoid creating state during ordinary discovery.
    if not hasattr(type(ctx), "state"):
        ctx.register_memory_provider(CustomBuildProvider())
    else:
        from . import heartbeat

        heartbeat.register(ctx)
