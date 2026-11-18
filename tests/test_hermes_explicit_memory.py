"""Verify explicit memory through Hermes v0.23.1 against disposable Postgres."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "custom_build"
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))
TEST_DATABASE_URL = os.environ.get("CHA_FINAL_TEST_DATABASE_URL")
FACT = "The harbor keeper's brass compass is tagged cobalt."
SECRET_MARKER = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"
PROJECT_QUERY = "isolation ledger"
PRIMARY_FACT = "The amber isolation ledger belongs to the first project."
OTHER_FACT = "The cobalt isolation ledger belongs to the second project."
UNSCOPED_FACT = "The jade isolation ledger belongs to no project."


def _scenario(mode, workspace_actions=None):
    """One isolated Hermes process, with the profile and .env prepared by the test."""
    from agent.memory_manager import MemoryManager
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_cli.env_loader import resolve_home_dotenv
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from plugins import memory

    home = Path(os.environ["HERMES_HOME"])
    token = set_secret_scope(resolve_home_dotenv(home), profile_home=str(home))
    try:
        discover_plugins()
        entries = get_plugin_manager().list_plugins()
        entry = next((item for item in entries if item["name"] == "custom_build"), None)
        assert entry and entry["error"] is None and entry["enabled"] and entry["kind"] == "standalone", entry

        # Only explicit registration can pass, even if the module defines an instantiable subclass.
        with patch.object(memory, "_instantiate_subclass", return_value=None):
            provider = memory.load_memory_provider("custom_build")
        assert provider is not None and provider.name == "custom_build", "memory-provider registration failed"

        manager = MemoryManager()
        manager.add_provider(provider)
        session_id = f"explicit-memory-{mode}"
        manager.initialize_all(session_id, hermes_home=str(home))
        assert manager.has_tool("store_fact"), "Hermes did not register the store_fact tool"

        def store_fact(fact):
            return json.loads(manager.handle_tool_call("store_fact", {"fact": fact}, session_id=session_id))

        def prefetch(query):
            return manager.prefetch_all(query, session_id=session_id)

        if workspace_actions is not None:
            outcomes = []
            # Scope each call after initialization so an initial or cached project cannot pass.
            for workspace, fact in json.loads(workspace_actions):
                cwd_token = set_session_cwd(workspace)
                try:
                    outcomes.append(prefetch(PROJECT_QUERY) if fact is None else store_fact(fact))
                finally:
                    reset_session_cwd(cwd_token)
            return {"outcomes": outcomes}

        if mode == "write":
            stored = store_fact(FACT)
            rejected = store_fact(" ")
            # Keep dispatch real, but isolate the plugin's own forced redaction from Hermes's first pass.
            with patch("agent.memory_manager._redact_for_provider", side_effect=lambda value: value):
                redacted = store_fact(f"Harbor marker credential {SECRET_MARKER}")
            return {"stored": stored["stored"], "redacted": redacted["stored"], "rejected": rejected}
        if mode == "read":
            return {"found": prefetch("brass compasses"), "empty": prefetch("elephants")}
        return {"prefetch": prefetch("brass compass"), "tool": store_fact(FACT)}
    finally:
        reset_secret_scope(token)


@unittest.skipUnless(TEST_DATABASE_URL, "set CHA_FINAL_TEST_DATABASE_URL to a disposable PostgreSQL server")
class HermesExplicitMemoryTests(unittest.TestCase):
    def _run(self, scratch, mode, database_url=None, *, workspace_actions=None):
        home = scratch / mode
        shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
        (home / "config.yaml").write_text(
            "memory:\n  provider: custom_build\nplugins:\n  enabled:\n    - custom_build\n"
            "security:\n  redact_secrets: false\n",
            encoding="utf-8",
        )
        if database_url:
            (home / ".env").write_text(f"CHA_FINAL_DATABASE_URL={json.dumps(database_url)}\n", encoding="utf-8")

        # Keep the process registry, dotenv and profile files separate from the test runner and host.
        command = [sys.executable, "-B", __file__, mode]
        if workspace_actions is not None:
            command.append(json.dumps(workspace_actions))
        completed = subprocess.run(
            command,
            cwd=scratch,
            env={
                "HOME": str(scratch),
                "HERMES_HOME": str(home),
                "HERMES_REDACT_SECRETS": "false",
                "GIT_ALLOW_PROTOCOL": "file",
                "GIT_CONFIG_NOSYSTEM": "1",
                "PATH": os.defpath,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, f"{mode} scenario failed:\n{completed.stderr}")
        return json.loads(completed.stdout.splitlines()[-1]), completed.stderr

    @contextmanager
    def _database(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        self.assertTrue((PLUGIN / "plugin.yaml").is_file(), "the custom_build plugin manifest is missing")
        database_name = f"cha_hermes_{uuid4().hex}"
        database = sql.Identifier(database_name)
        database_url = make_conninfo(TEST_DATABASE_URL, dbname=database_name)
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as server:
            server.execute(sql.SQL("create database {}").format(database))
            try:
                self.assertTrue(MIGRATIONS, "the database migrations are missing")
                with psycopg.connect(database_url) as conn:
                    for migration in MIGRATIONS:
                        conn.execute(migration.read_text(encoding="utf-8"))
                yield database_url
            finally:
                server.execute(sql.SQL("drop database {}").format(database))

    def test_fact_survives_a_new_hermes_session_and_outage_is_visible(self):
        import psycopg
        from psycopg.conninfo import make_conninfo

        with tempfile.TemporaryDirectory(prefix="cha-hermes-") as scratch_dir:
            scratch = Path(scratch_dir)
            with self._database() as database_url:
                saved, _ = self._run(scratch, "write", database_url)
                self.assertIs(type(saved["stored"]), int)
                self.assertIs(type(saved["redacted"]), int)
                self.assertEqual(saved["rejected"], {"error": "store.invalid"})
                with psycopg.connect(database_url) as conn:
                    body, = conn.execute(
                        "select body from cha_final.facts where id = %s", (saved["stored"],),
                    ).fetchone()
                    redacted, = conn.execute(
                        "select body from cha_final.facts where id = %s", (saved["redacted"],),
                    ).fetchone()
                    failures = conn.execute("select code, result from cha_final.events").fetchall()
                self.assertEqual(failures, [("store.invalid", "failed")])
                self.assertEqual(body, FACT)
                self.assertIn("Harbor marker credential", redacted)
                self.assertNotIn(SECRET_MARKER, redacted)

                recall, _ = self._run(scratch, "read", database_url)
                self.assertIn(FACT, recall["found"])
                self.assertTrue(recall["found"].startswith("Recalled notes"))
                self.assertEqual(recall["empty"], "")

            dead_url = make_conninfo(host=str(scratch / "absent-socket"), dbname="cha_hermes", connect_timeout=2)
            for mode, url, code in (
                ("outage", dead_url, "db.unreachable"),
                ("unconfigured", None, "config.missing"),
            ):
                with self.subTest(mode=mode):
                    failure, log = self._run(scratch, mode, url)
                    self.assertIn(code, failure["prefetch"])
                    self.assertEqual(failure["tool"].get("error"), code)
                    self.assertIn(code, log)

    def test_project_recall_is_exclusive_across_origins(self):
        with tempfile.TemporaryDirectory(prefix="cha-projects-") as scratch_dir, self._database() as database_url:
            scratch = Path(scratch_dir)
            primary = scratch / "primary"
            alias = scratch / "alias"
            other = scratch / "other"
            unscoped = scratch / "unscoped"
            unscoped.mkdir()

            def git(*args):
                subprocess.run(
                    ["git", *args], cwd=scratch,
                    env={"HOME": str(scratch), "PATH": os.defpath,
                         "GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_NOSYSTEM": "1"},
                    capture_output=True, text=True, check=True, timeout=10,
                )

            git("init", "--quiet", "--template=", str(primary))
            git("-C", str(primary), "remote", "add", "origin", "https://github.com/team/harbor.git")
            git("clone", "--quiet", "--local", "--template=", str(primary), str(alias))
            git("-C", str(alias), "remote", "set-url", "origin", "git@github.com:team/harbor.git")
            git("init", "--quiet", "--template=", str(other))
            git("-C", str(other), "remote", "add", "origin", "https://github.com/team/lighthouse.git")

            writes = [(primary, PRIMARY_FACT), (other, OTHER_FACT), (unscoped, UNSCOPED_FACT)]
            saved, _ = self._run(scratch, "scoped-write", database_url,
                                 workspace_actions=[(str(workspace), fact) for workspace, fact in writes])
            self.assertEqual(len(saved["outcomes"]), len(writes))
            for outcome in saved["outcomes"]:
                self.assertIs(type(outcome.get("stored")), int, outcome)

            reads = [(primary, PRIMARY_FACT), (alias, PRIMARY_FACT), (other, OTHER_FACT), (unscoped, UNSCOPED_FACT)]
            recall, _ = self._run(scratch, "scoped-read", database_url,
                                  workspace_actions=[(str(workspace), None) for workspace, _ in reads])
            possible_facts = [fact for _, fact in writes]
            for (workspace, expected), note in zip(reads, recall["outcomes"], strict=True):
                with self.subTest(workspace=workspace.name):
                    self.assertTrue(note.startswith("Recalled notes"), note)
                    self.assertEqual([fact for fact in possible_facts if fact in note], [expected])


if __name__ == "__main__":
    print(json.dumps(_scenario(*sys.argv[1:])))
