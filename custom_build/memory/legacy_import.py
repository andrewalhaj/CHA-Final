"""Import a fixed snapshot of already-redacted legacy facts into an empty store."""

import hashlib
import json
from uuid import UUID

from psycopg import Connection

from custom_build.memory import store


class LegacyImportError(ValueError):
    """The target's existing facts or marker conflict with this import."""


def apply_snapshot(conn: Connection, rows: list[tuple[UUID, str, str | None]]) -> int:
    """Import at READ COMMITTED, or return zero for an identical completed snapshot."""
    digest = hashlib.sha256()
    for source_id, body, project in rows:
        encoded_row = json.dumps((str(source_id), body, project), separators=(",", ":"))
        digest.update(encoded_row.encode("utf-8"))
    snapshot_digest = digest.hexdigest()

    with conn.transaction():
        # Take these locks in the same order on every retry, before reading any target tables.
        conn.execute(
            "lock table cha_final.facts, cha_final.project_facts, cha_final.legacy_import "
            "in share row exclusive mode"
        )
        marker = conn.execute("select snapshot_digest from cha_final.legacy_import").fetchone()
        if marker is not None:
            if marker[0] == snapshot_digest:
                return 0
            raise LegacyImportError("The target already has a different snapshot.")

        nonempty, = conn.execute(
            "select exists (select 1 from cha_final.facts) or exists (select 1 from cha_final.project_facts)"
        ).fetchone()
        if nonempty:
            raise LegacyImportError("The target already contains facts.")

        for _, body, project in rows:
            store.save(conn, body, project=project)
        conn.execute(
            "insert into cha_final.legacy_import (snapshot_digest) values (%s)",
            (snapshot_digest,),
        )
    return len(rows)
