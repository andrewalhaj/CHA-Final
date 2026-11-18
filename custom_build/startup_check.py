"""Check the currently deployed provider and database without writing to the store."""

import psycopg
from agent.secret_scope import get_secret
from hermes_cli.config import cfg_get, load_config_readonly
from plugins.memory import load_memory_provider
from psycopg.errors import (
    InsufficientPrivilege,
    QueryCanceled,
    UndefinedColumn,
    UndefinedTable,
)

from . import SOURCE, failures


def _exit_failure(code: str) -> int:
    print(code)
    return 1


def _fail(code: str) -> int:
    failures.record(None, code, "failed", source=SOURCE, local_only=True)
    return _exit_failure(code)


def _probe(connection) -> bool:
    connection.read_only = True
    connection.execute("set local statement_timeout = '3s'")
    connection.execute("select id, body from cha_final.facts limit 0")
    connection.execute("select id, project, body from cha_final.project_facts limit 0")

    # EXPLAIN without ANALYZE checks statement access without executing inserts or defaults.
    connection.execute("explain insert into cha_final.facts (body) values ('') returning id")
    connection.execute("explain insert into cha_final.project_facts (project, body) values ('', '') returning id")
    connection.execute("explain insert into cha_final.events (source, code, result, reason) values ('', '', '', '')")
    # Project facts use an explicit nextval default. The other identity defaults need no sequence grant.
    return connection.execute(
        "select pg_catalog.has_sequence_privilege('cha_final.facts_id_seq', 'USAGE, UPDATE')",
    ).fetchone()[0]


@failures.boundary(_exit_failure)
def run(_args) -> int:
    if cfg_get(load_config_readonly(), "memory", "provider") != SOURCE:
        return _fail("guard.not_loaded")
    provider = load_memory_provider(SOURCE, register_skills=False)
    if getattr(provider, "name", None) != SOURCE:
        return _fail("guard.not_loaded")

    database_url = (get_secret("CHA_FINAL_DATABASE_URL") or "").strip()
    if not database_url:
        return _fail("config.missing")
    try:
        psycopg.conninfo.conninfo_to_dict(database_url)
    except psycopg.ProgrammingError:
        return _fail("config.missing")

    try:
        connection = psycopg.connect(database_url, connect_timeout=2)
    except psycopg.OperationalError:
        return _fail("db.unreachable")
    with connection:
        try:
            usable = _probe(connection)
        except (UndefinedTable, UndefinedColumn):
            return _fail("db.behind")
        except (InsufficientPrivilege, QueryCanceled):
            return _fail("db.query")
        except psycopg.OperationalError:
            return _fail("db.unreachable")
    return 0 if usable else _fail("db.query")
