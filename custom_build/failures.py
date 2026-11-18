"""Record first-milestone failures without logging their input or exception text."""

import json
import logging
from functools import wraps
from traceback import walk_tb

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
    "recall.timeout": "The memory search exceeded its deadline.",
    "store.invalid": "An explicit fact must be nonempty text.",
    "store.read_only": "Only the primary Hermes session can store an explicit fact.",
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
) -> bool:
    """Log and attempt an insert, returning whether it succeeded.

    The caller supplies a configured source label and owns the transaction.
    """
    if code not in REASONS:
        raise ValueError("Unknown failure code")
    if result not in ("failed", "degraded"):
        raise ValueError("Unknown failure result")

    reason = REASONS[code]
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
    logger.warning("%s", json.dumps(fields))
    if conn is None:
        return _write_failed(source, code)

    statement = "INSERT INTO cha_final.events (source, code, result, reason) VALUES (%s, %s, %s, %s)"
    values = (source, code, result, reason)
    try:
        conn.execute(statement, values)
    except (OperationalError, InFailedSqlTransaction, InsufficientPrivilege, ReadOnlySqlTransaction):
        return _write_failed(source, code)
    return True


def boundary(failure_result):
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
                record(None, "unhandled", "failed", source="custom_build", diagnostic=failure.__cause__)
                return failure_result("unhandled")

        return call

    return decorate


class _BoundaryFailure(Exception):
    """Carry a cause to the shared boundary without exposing it to Hermes."""
