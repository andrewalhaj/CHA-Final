"""Record runtime events without logging their input or exception text."""

import json
import logging
from functools import wraps
from traceback import walk_tb

from agent.redact import redact_sensitive_text
from psycopg import Connection, OperationalError
from psycopg.errors import (
    InFailedSqlTransaction,
    InsufficientPrivilege,
    ReadOnlySqlTransaction,
)

logger = logging.getLogger(__name__)

REASONS = {
    "config.missing": "A required memory setting is missing or invalid.",
    "db.unreachable": "The memory store could not be reached.",
    "embed.unavailable": "The local embedding service could not give a usable response.",
    "embed.model_mismatch": "The local embedding service returned a different model or vector size.",
    "recall.timeout": "The memory search exceeded its deadline.",
    "store.invalid": "An explicit fact must be nonempty text.",
    "store.read_only": "Only the primary Hermes session can store an explicit fact.",
    "legacy.arguments_invalid": "The legacy import arguments are invalid. See --help.",
    "legacy.mapping_invalid": "The legacy project map could not be read or is invalid.",
    "legacy.source_invalid": "The legacy snapshot contains an unresolved project association.",
    "legacy.target_conflict": "The target already contains facts or a different legacy snapshot.",
    "host.heartbeat": "The agent host recorded a heartbeat.",
    "guard.crash": "An unexpected guard failure occurred.",
    "verify_gate.blocked": "An unread managed change was blocked.",
    "verify_gate.withheld": "An unread managed claim was withheld.",
    "verify_gate.would_block": "A managed change lacked a successful read.",
    "verify_gate.would_withhold": "A managed claim lacked a successful read.",
    "unhandled": "An unexpected memory failure occurred.",
    "events.write_failed": "The failure event could not be written to the store.",
}


def _write_failed(source: str, code: str) -> bool:
    logger.error("%s", json.dumps({
        "source": source,
        "code": "events.write_failed",
        "result": "failed",
        "reason": REASONS["events.write_failed"],
        "failed_code": code,
    }))
    return False


def record(
    conn: Connection | None,
    code: str,
    result: str,
    *,
    source: str,
    diagnostic: Exception | None = None,
    reason: str | None = None,
    defer_write=None,
) -> bool | None:
    """Log first, then insert and return its success, or schedule the same insert and return None.

    The caller supplies a configured source label and owns the transaction.
    """
    if code not in REASONS:
        raise ValueError("Unknown failure code")
    if result not in ("ok", "failed", "degraded"):
        raise ValueError("Unknown failure result")

    reason = redact_sensitive_text(reason, force=True)[:200] if reason else REASONS[code]
    fields = {
        "source": source,
        "code": code,
        "result": result,
        "reason": reason,
    }
    if diagnostic is not None:
        fields["error.type"] = type(diagnostic).__name__
        fields["frames"] = [
            {"function": frame.f_code.co_name, "line": line}
            for frame, line in walk_tb(diagnostic.__traceback__)
        ]
    logger.log(logging.INFO if result == "ok" else logging.WARNING, "%s", json.dumps(fields))
    values = (source, code, result, reason)
    if defer_write is not None:
        defer_write(lambda connection: _persist(connection, values))
        return None
    return _persist(conn, values)


def _persist(conn, values):
    source, code, _, _ = values
    if conn is None:
        return _write_failed(source, code)
    statement = "INSERT INTO cha_final.events (source, code, result, reason) VALUES (%s, %s, %s, %s)"
    try:
        conn.execute(statement, values)
    except (OperationalError, InFailedSqlTransaction, InsufficientPrivilege, ReadOnlySqlTransaction):
        return _write_failed(source, code)
    return True


def boundary(failure_result, *, on_error=None):
    """Expose a fixed result when Hermes would swallow or expose an unexpected exception."""
    def decorate(method):
        @wraps(method)
        def call(*args, **kwargs):
            try:
                try:
                    return method(*args, **kwargs)
                except Exception as error:
                    # The original cause stays inside this wrapper, where it is inspected without its message.
                    raise _BoundaryFailure from error
            except _BoundaryFailure as failure:
                if on_error is None:
                    code = "unhandled"
                    record(
                        kwargs.get("connection"), code, "failed",
                        source="custom_build", diagnostic=failure.__cause__,
                    )
                else:
                    code = "guard.crash"
                    on_error(code, failure.__cause__)
                return failure_result(code)

        return call

    return decorate


class _BoundaryFailure(Exception):
    """Carry a cause to the shared boundary without exposing it to Hermes."""
