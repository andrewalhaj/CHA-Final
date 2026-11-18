"""Record first-milestone failures without logging their input or exception text."""

import json
import logging

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


def record(conn: Connection | None, code: str, result: str, *, source: str) -> bool:
    """Log and attempt an insert, returning whether it succeeded.

    The caller supplies a configured source label and owns the transaction.
    """
    if code not in REASONS:
        raise ValueError("Unknown failure code")
    if result not in ("failed", "degraded"):
        raise ValueError("Unknown failure result")

    reason = REASONS[code]
    logger.warning("%s", json.dumps({
        "source": source,
        "code": code,
        "result": result,
        "reason": reason,
    }))
    if conn is None:
        return _write_failed(source, code)

    statement = "INSERT INTO cha_final.events (source, code, result, reason) VALUES (%s, %s, %s, %s)"
    values = (source, code, result, reason)
    try:
        conn.execute(statement, values)
    except (OperationalError, InFailedSqlTransaction, InsufficientPrivilege, ReadOnlySqlTransaction):
        return _write_failed(source, code)
    return True
