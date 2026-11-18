"""Verify the legacy CLI and Hermes recall against separate disposable databases."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import test_hermes_explicit_memory as hermes

_LEGACY_PROJECT = "legacy-harbor-fixture"
_MATCHED_ORIGIN = "https://git.example.invalid/fixture/harbor.git"
_OTHER_ORIGIN = "https://git.example.invalid/fixture/lighthouse.git"
_FACT = "The amber isolation ledger from the legacy snapshot is in the cabinet."
_RETIRED_FACT = "The retired isolation ledger from the legacy snapshot is no longer valid."


@contextmanager
def _legacy_database():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = f"cha_legacy_{uuid4().hex}"
    database = sql.Identifier(name)
    url = make_conninfo(hermes.TEST_DATABASE_URL, dbname=name)
    with psycopg.connect(hermes.TEST_DATABASE_URL, autocommit=True) as server:
        server.execute(sql.SQL("create database {}").format(database))
        try:
            with psycopg.connect(url) as conn:
                conn.execute("""
                    create schema memory;
                    create table memory.facts (
                        id uuid primary key,
                        body text not null,
                        project text,
                        kind text not null,
                        created_at timestamptz not null,
                        superseded_by uuid references memory.facts(id),
                        retired_at timestamptz
                    );
                    """)
                conn.execute(
                    """insert into memory.facts (id, body, project, kind, created_at, retired_at)
                       values (%s, %s, %s, 'fact', now(), null), (%s, %s, %s, 'fact', now(), now())""",
                    (uuid4(), _FACT, _LEGACY_PROJECT, uuid4(), _RETIRED_FACT, _LEGACY_PROJECT),
                )
            yield url
        finally:
            server.execute(sql.SQL("drop database {}").format(database))


def _local_repo(workspace, origin):
    env = {
        "HOME": str(workspace.parent),
        "PATH": os.defpath,
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for args in (
        ("init", "--quiet", "--template=", str(workspace)),
        ("-C", str(workspace), "remote", "add", "origin", origin),
    ):
        subprocess.run(["git", *args], env=env, capture_output=True, check=True, timeout=10)


@unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
class LegacyRehearsalTests(unittest.TestCase):
    def _import(self, mapping, legacy_url, target_url, *, apply=False):
        args = [sys.executable, "-B", "-m", "scripts.import_legacy", "--project-map", str(mapping)]
        if apply:
            args.append("--apply")
        completed = subprocess.run(
            args,
            cwd=hermes.ROOT,
            env={
                "HOME": str(mapping.parent),
                "HERMES_HOME": str(mapping.parent / "operator-home"),
                "PATH": os.defpath,
                "CHA_FINAL_LEGACY_DATABASE_URL": legacy_url,
                "CHA_FINAL_DATABASE_URL": target_url,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for private in (_FACT, _RETIRED_FACT, legacy_url, target_url):
            self.assertNotIn(private, completed.stderr)
        return json.loads(completed.stdout)

    def test_legacy_import_is_idempotent_and_project_scoped_in_hermes(self):
        import psycopg
        from psycopg.conninfo import make_conninfo

        harness = hermes.HermesExplicitMemoryTests()
        with (
            tempfile.TemporaryDirectory(prefix="cha-rehearsal-") as scratch_dir,
            _legacy_database() as legacy_url,
            harness._database() as target_url,
        ):
            scratch = Path(scratch_dir)
            mapping = scratch / "project-map.json"
            mapping.write_text(json.dumps({_LEGACY_PROJECT: _MATCHED_ORIGIN}), encoding="utf-8")

            # Dry-run must succeed even when no target socket can be reached.
            dead_url = make_conninfo(host=str(scratch / "absent-socket"), dbname="cha_target", connect_timeout=2)
            self.assertEqual(self._import(mapping, legacy_url, dead_url), {"eligible": 1, "imported": 0})
            with psycopg.connect(target_url) as conn:
                self.assertEqual(conn.execute("select count(*) from cha_final.project_facts").fetchone(), (0,))

            self.assertEqual(self._import(mapping, legacy_url, target_url, apply=True), {"eligible": 1, "imported": 1})
            self.assertEqual(self._import(mapping, legacy_url, target_url, apply=True), {"eligible": 1, "imported": 0})

            matched, other = scratch / "matched", scratch / "other"
            _local_repo(matched, _MATCHED_ORIGIN)
            _local_repo(other, _OTHER_ORIGIN)
            recall, _ = harness._run(
                scratch, "imported-recall", target_url,
                workspace_actions=[(str(matched), None), (str(other), None)],
            )
            found, excluded = recall["outcomes"]
            self.assertTrue(found.startswith("Recalled notes"), found)
            self.assertEqual(found.count(_FACT), 1)
            self.assertNotIn(_RETIRED_FACT, found)
            self.assertEqual(excluded, "")
