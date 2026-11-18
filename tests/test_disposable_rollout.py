"""Rehearse a plugin file swap against a populated, migrated disposable database."""

import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import test_hermes_explicit_memory as hermes

# The last main commit before migration 0003 (#23).
PREVIOUS = "bfc17f4dc90c371ded48a95de62d083668efec3a"
MIGRATION = hermes.ROOT / "migrations" / "0003_legacy_import.sql"
AFTER_ROLLBACK = "The blue isolation ledger was written after rollback."


def _stage(home, revision):
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision, "custom_build"],
        cwd=hermes.ROOT, capture_output=True, check=True, timeout=10,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as artifact:
        artifact.extractall(home / "plugins", filter="data")
    return archive


def _files(archive):
    with tarfile.open(fileobj=io.BytesIO(archive)) as artifact:
        return {
            member.name: artifact.extractfile(member).read()
            for member in artifact.getmembers() if member.isfile()
        }


@contextmanager
def _app_login():
    import psycopg
    from psycopg import sql

    username = f"cha_app_{uuid4().hex}"
    password = uuid4().hex
    with psycopg.connect(hermes.TEST_DATABASE_URL, autocommit=True) as cluster:
        cluster.execute(sql.SQL("create role {} login password {}").format(
            sql.Identifier(username), sql.Literal(password),
        ))
        try:
            yield username, password
        finally:
            cluster.execute(sql.SQL("drop role {}").format(sql.Identifier(username)))


@unittest.skipUnless(hermes.TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to disposable PostgreSQL")
class DisposableRolloutTests(unittest.TestCase):
    def test_previous_plugin_recovers_on_a_populated_forward_migrated_store(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        runner = hermes.HermesExplicitMemoryTests()
        before = [path for path in hermes.MIGRATIONS if path.name < MIGRATION.name]
        with (
            tempfile.TemporaryDirectory(prefix="cha-rollout-") as folder,
            _app_login() as (username, password),
            runner._database(before) as owner_url,
        ):
            scratch = Path(folder)
            home = scratch / "profile"
            installed = home / "plugins" / "custom_build"
            previous_archive = _stage(home, PREVIOUS)
            app_url = make_conninfo(owner_url, user=username, password=password)
            with psycopg.connect(owner_url) as owner:
                login = sql.Identifier(username)
                owner.execute(sql.SQL("grant usage on schema cha_final to {}").format(login))
                owner.execute(sql.SQL("grant select, insert on all tables in schema cha_final to {}").format(login))
                owner.execute(sql.SQL("grant usage on all sequences in schema cha_final to {}").format(login))
                self.assertEqual(owner.execute("select to_regclass('cha_final.legacy_import')").fetchone(), (None,))

            (home / "config.yaml").write_text(
                "memory:\n  provider: custom_build\nplugins:\n  enabled:\n    - custom_build\n", encoding="utf-8",
            )
            (home / ".env").write_text(f"CHA_FINAL_DATABASE_URL={json.dumps(app_url)}\n", encoding="utf-8")
            initial, _ = runner._run_installed(scratch, home, "write")
            self.assertIs(type(initial["stored"]), int)

            with psycopg.connect(owner_url) as owner:
                owner.execute(MIGRATION.read_text(encoding="utf-8"))
            candidate_archive = _stage(home, "HEAD")
            self.assertNotEqual(_files(candidate_archive), _files(previous_archive))
            candidate, _ = runner._run_installed(scratch, home, "read")
            self.assertIn(hermes.FACT, candidate["found"])

            # Fault injection only in the candidate's disposable install.
            module = installed / "__init__.py"
            with module.open("a", encoding="utf-8") as stream:
                stream.write('\ndef register(ctx):\n    raise RuntimeError("disposable rollout candidate failed")\n')
            with self.assertRaisesRegex(AssertionError, "disposable rollout candidate failed"):
                runner._run_installed(scratch, home, "read")

            _stage(home, PREVIOUS)
            restored = {
                path.relative_to(home / "plugins").as_posix(): path.read_bytes()
                for path in installed.rglob("*") if path.is_file()
            }
            self.assertEqual(restored.keys(), _files(previous_archive).keys())
            self.assertEqual(restored, _files(previous_archive))

            after, _ = runner._run_installed(
                scratch, home, "rollback-write", workspace_actions=[(os.fspath(scratch), AFTER_ROLLBACK)],
            )
            self.assertIs(type(after["outcomes"][0]["stored"]), int)
            original, _ = runner._run_installed(scratch, home, "read")
            self.assertIn(hermes.FACT, original["found"])
            fresh, _ = runner._run_installed(
                scratch, home, "rollback-recall", workspace_actions=[(os.fspath(scratch), None)],
            )
            self.assertIn(AFTER_ROLLBACK, fresh["outcomes"][0])
            with psycopg.connect(owner_url) as owner:
                self.assertEqual(owner.execute("select count(*) from cha_final.legacy_import").fetchone(), (0,))
