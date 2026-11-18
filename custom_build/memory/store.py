"""Persist and recall explicit facts after the provider redacts their text."""

from psycopg import Connection


def save(conn: Connection, body: str) -> int:
    """Insert one fact in the caller's transaction and return its ID."""
    row = conn.execute(
        "insert into cha_final.facts (body) values (%s) returning id",
        (body,),
    ).fetchone()
    return row[0]


def recall(conn: Connection, query: str) -> list[str]:
    """Return up to eight lexical matches, newest first."""
    # shortcut: scan facts directly for the disposable milestone; index the search expression before large stores.
    rows = conn.execute(
        "select body from cha_final.facts "
        "where to_tsvector('english', body) @@ plainto_tsquery('english', %s) "
        "order by id desc limit 8",
        (query,),
    )
    return [body for (body,) in rows]
