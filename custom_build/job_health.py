"""Find configured database roles with no successful heartbeat in the last 24 hours."""

import re

import psycopg
from agent.secret_scope import get_secret
from psycopg.errors import InsufficientPrivilege

from . import SOURCE, _connect, failures

_ROLE = re.compile(r"[A-Za-z0-9_-]{1,63}")


class _EventWriteFailed(Exception):
    """The recorder rejected a finding and the whole run must roll back."""


def _exit_failure(code: str) -> int:
    print(code)
    return 1


def _fail(code: str, connection=None) -> int:
    failures.record(connection, code, "failed", source=SOURCE)
    return _exit_failure(code)


def _check(connection, roles: list[str]) -> None:
    with connection.transaction():
        connection.execute("set local statement_timeout = '3s'")
        source, fresh = connection.execute(
            """
            select session_user, array(
                select distinct source from cha_final.events
                where source = any(%s) and code = 'host.heartbeat' and result = 'ok'
                  and created_at between now() - interval '24 hours' and now()
            )
            """,
            (roles,),
        ).fetchone()
        for role in roles:
            if role not in fresh and not failures.record(
                connection, "host.missing", "failed", source=source,
                reason=f"Expected a successful heartbeat from {role} in the last 24 hours.",
            ):
                raise _EventWriteFailed


@failures.boundary(_exit_failure)
def run(ctx) -> int:
    roles = ctx.get_config("agent_hosts")
    if (
        not isinstance(roles, list) or not roles
        or not all(isinstance(role, str) and _ROLE.fullmatch(role) for role in roles)
        or len(set(roles)) != len(roles)
    ):
        return _fail("config.missing")
    database_url = (get_secret("CHA_FINAL_DATABASE_URL") or "").strip()
    if not database_url:
        return _fail("config.missing")
    try:
        psycopg.conninfo.conninfo_to_dict(database_url)
    except psycopg.ProgrammingError:
        return _fail("config.missing")

    try:
        connection = _connect(database_url)
    except psycopg.OperationalError:
        return _fail("db.unreachable")
    with connection:
        try:
            _check(connection, roles)
        except _EventWriteFailed:
            return _exit_failure("events.write_failed")
        except InsufficientPrivilege:
            return _fail("db.query", connection)
        except psycopg.OperationalError:
            return _fail("db.unreachable")
    return 0
