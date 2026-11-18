"""Extract live legacy facts from a read-only snapshot."""

from uuid import UUID

from agent.redact import redact_sensitive_text, register_redaction_patterns
from psycopg import Connection
from psycopg.pq import TransactionStatus

from .. import REDACTION_PATTERNS
from .project import key_for_origin


class LegacySourceError(ValueError):
    """The legacy snapshot or its project mapping cannot be used safely."""


def read_snapshot(conn: Connection, project_origins: dict[str, str]) -> list[tuple[UUID, str, str | None]]:
    """Return eligible rows in creation order with redacted bodies and current project keys."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise LegacySourceError("The legacy source connection must be idle")
    with conn.transaction():
        conn.execute("set transaction read only")
        rows = conn.execute(
            "select id, body, project from memory.facts "
            "where kind = 'fact' and retired_at is null and superseded_by is null "
            "order by created_at, id",
        ).fetchall()

    register_redaction_patterns(REDACTION_PATTERNS, source="custom_build")
    keys: dict[str | None, str | None] = {None: None}
    used: set[str] = set()
    facts: list[tuple[UUID, str, str | None]] = []
    for fact_id, body, label in rows:
        if label not in keys:
            origin = project_origins.get(label)
            if not isinstance(origin, str):
                raise LegacySourceError("An eligible legacy project has no origin mapping")
            try:
                key = key_for_origin(origin)
            except ValueError:
                raise LegacySourceError("A legacy project origin mapping is invalid") from None
            if key is None:
                raise LegacySourceError("A legacy project mapping requires a Git network origin")
            if key in used:
                raise LegacySourceError("Distinct legacy projects map to the same project key")
            keys[label] = key
            used.add(key)
        facts.append((fact_id, redact_sensitive_text(body, force=True, redact_url_credentials=True), keys[label]))
    return facts
