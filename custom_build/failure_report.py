"""Generate a Hermes attachment from newly persisted failure outcomes."""

from datetime import datetime

import psycopg
from agent.redact import redact_sensitive_text
from agent.secret_scope import get_secret
from psycopg.errors import (
    InsufficientPrivilege,
    QueryCanceled,
    ReadOnlySqlTransaction,
    UndefinedColumn,
    UndefinedTable,
)

from . import SOURCE, failures

_MARKER = "daily-report"


class _MarkerWriteFailed(RuntimeError):
    """Report generation cannot advance its checkpoint."""


def _exit_failure(code: str) -> int:
    print(code)
    return 1


def _fail(code: str) -> int:
    failures.record(None, code, "failed", source=SOURCE, local_only=True)
    return _exit_failure(code)


def _cell(text: str) -> str:
    return " ".join(text.splitlines()).replace("\\", "\\\\").replace("|", "\\|")


def _render(rows) -> str:
    lines = ["# Failure report", "", "| Code | Source | Outcome | Count |", "|---|---|---|---:|"]
    for code, source, result, count in rows:
        lines.append(f"| {_cell(code)} | {_cell(source)} | {result} | {count} |")
    return redact_sensitive_text("\n".join(lines) + "\n", force=True, redact_url_credentials=True)


def _attachment(rows, day: str) -> str:
    from gateway.platforms.base import cache_document_from_bytes

    return cache_document_from_bytes(_render(rows).encode("utf-8"), f"failures-{day}.md")


def _generate(connection) -> tuple[str, str | None]:
    with connection.transaction():
        connection.execute("set transaction isolation level read committed")
        connection.execute("set local statement_timeout = '3s'")
        # Serialize this snapshot and marker against new inserts, using row IDs rather than event clocks.
        connection.execute(
            "select pg_catalog.pg_advisory_xact_lock(%s)", (failures.EVENT_REPORT_LOCK,),
        )
        source, = connection.execute("select session_user").fetchone()
        rows = connection.execute(
            """
            select code, source, result, count(*)
              from cha_final.events
             where result in ('failed', 'degraded')
               and id > coalesce((select max(id) from cha_final.events
                                   where source = %s and code = 'job.succeeded' and reason = %s), 0)
             group by code, source, result
             order by code, source, result
            """,
            (source, _MARKER),
        ).fetchall()
        day = datetime.now().astimezone().date().isoformat()
        output = _attachment(rows, day) if rows else None
        if not failures.record(connection, "job.succeeded", "ok", source=source, reason=_MARKER):
            raise _MarkerWriteFailed
    total = sum(row[3] for row in rows)
    if not total:
        return f"All clear for {day}. No new failure events.", None
    noun = "event" if total == 1 else "events"
    return f"Failure report for {day}. {total} {noun}.", output


@failures.boundary(_exit_failure)
def run(_args) -> int:
    url = (get_secret("CHA_FINAL_DATABASE_URL") or "").strip()
    if not url:
        return _fail("config.missing")
    try:
        psycopg.conninfo.conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return _fail("config.missing")

    try:
        connection = psycopg.connect(url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        return _fail("db.unreachable")
    with connection:
        try:
            summary, attachment = _generate(connection)
        except _MarkerWriteFailed:
            return _exit_failure("events.write_failed")
        except (InsufficientPrivilege, QueryCanceled, ReadOnlySqlTransaction):
            return _fail("db.query")
        except (UndefinedColumn, UndefinedTable):
            return _fail("db.behind")
        except psycopg.OperationalError:
            return _fail("db.unreachable")
    print(summary)
    if attachment is not None:
        print(f"MEDIA:{attachment}")
    return 0
