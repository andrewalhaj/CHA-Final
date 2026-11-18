"""Record one heartbeat through the pinned Hermes CLI."""

import psycopg
from agent.secret_scope import get_secret

from . import SOURCE, failures


def _exit_failure(code: str) -> int:
    print(code)
    return 1


def _record_failure(code: str) -> int:
    failures.record(None, code, "failed", source=SOURCE)
    return _exit_failure(code)


@failures.boundary(_exit_failure)
def _run(_args) -> int:
    database_url = (get_secret("CHA_FINAL_DATABASE_URL") or "").strip()
    if not database_url:
        return _record_failure("config.missing")
    try:
        psycopg.conninfo.conninfo_to_dict(database_url)
    except psycopg.ProgrammingError:
        return _record_failure("config.missing")
    try:
        connection = psycopg.connect(database_url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        return _record_failure("db.unreachable")

    with connection:
        try:
            source, _ = connection.execute(
                "select session_user, pg_catalog.set_config('statement_timeout', '3000', false)",
            ).fetchone()
        except psycopg.OperationalError:
            return _record_failure("db.unreachable")
        if not failures.record(connection, "host.heartbeat", "ok", source=source):
            return _exit_failure("events.write_failed")
    return 0


def _add_commands(parser) -> None:
    commands = parser.add_subparsers(dest="custom_build_command", required=True)
    commands.add_parser("heartbeat", help="Record this database role's heartbeat")


def register(ctx) -> None:
    ctx.register_cli_command("custom-build", "CHA Final operator commands", _add_commands, _run)
