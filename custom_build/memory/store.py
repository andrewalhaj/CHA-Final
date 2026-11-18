"""Persist and recall explicit facts after the provider redacts their text."""

from psycopg import Connection


def save(conn: Connection, body: str, *, project: str | None = None) -> int:
    """Insert one fact in its scope and return its ID in the caller's transaction."""
    if project is None:
        row = conn.execute(
            "insert into cha_final.facts (body) values (%s) returning id",
            (body,),
        ).fetchone()
    else:
        row = conn.execute(
            "insert into cha_final.project_facts (project, body) values (%s, %s) returning id",
            (project, body),
        ).fetchone()
    return row[0]


def recall(conn: Connection, query: str, *, project: str | None = None) -> list[str]:
    """Return up to eight lexical matches in the given scope, newest first."""
    # shortcut: scan facts directly for the disposable milestone; index the search expression before large stores.
    lexical_match = (
        "to_tsvector('english', body) @@ plainto_tsquery('english', %s) "
        "order by id desc limit 8"
    )
    if project is None:
        rows = conn.execute("select body from cha_final.facts where " + lexical_match, (query,))
    else:
        rows = conn.execute(
            "select body from cha_final.project_facts where project = %s and " + lexical_match,
            (project, query),
        )
    return [body for (body,) in rows]
