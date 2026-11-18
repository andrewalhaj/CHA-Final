"""Verify explicit memory through Hermes v0.23.1 against disposable Postgres."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "custom_build"
MIGRATION = ROOT / "migrations" / "0001_bootstrap.sql"
TEST_DATABASE_URL = os.environ.get("CHA_FINAL_TEST_DATABASE_URL")
FACT = "The harbor keeper's brass compass is tagged cobalt."
SECRET_MARKER = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"


def _scenario(mode):
    """One isolated Hermes process, with the profile and .env prepared by the test."""
    from agent.memory_manager import MemoryManager
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
    def _run(self, scratch, mode, database_url=None):
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
        completed = subprocess.run(
            [sys.executable, "-B", __file__, mode],
            cwd=scratch,
            env={
                "HOME": str(scratch),
                "HERMES_HOME": str(home),
                "HERMES_REDACT_SECRETS": "false",
                "PATH": os.defpath,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, f"{mode} scenario failed:\n{completed.stderr}")
        return json.loads(completed.stdout.splitlines()[-1]), completed.stderr

    def test_fact_survives_a_new_hermes_session_and_outage_is_visible(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        self.assertTrue((PLUGIN / "plugin.yaml").is_file(), "the custom_build plugin manifest is missing")
        database_name = f"cha_hermes_{uuid4().hex}"
        database = sql.Identifier(database_name)
        database_url = make_conninfo(TEST_DATABASE_URL, dbname=database_name)
        with tempfile.TemporaryDirectory(prefix="cha-hermes-") as scratch_dir:
            scratch = Path(scratch_dir)
            with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as server:
                # Store tests migrate their own database. Migrate a new database for this harness.
                server.execute(sql.SQL("create database {}").format(database))
                try:
                    with psycopg.connect(database_url) as conn:
                        conn.execute(MIGRATION.read_text(encoding="utf-8"))

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
                finally:
                    server.execute(sql.SQL("drop database {}").format(database))

            dead_url = make_conninfo(host=str(scratch / "absent-socket"), dbname=database_name, connect_timeout=2)
            for mode, url, code in (
                ("outage", dead_url, "db.unreachable"),
                ("unconfigured", None, "config.missing"),
            ):
                with self.subTest(mode=mode):
                    failure, log = self._run(scratch, mode, url)
                    self.assertIn(code, failure["prefetch"])
                    self.assertEqual(failure["tool"].get("error"), code)
                    self.assertIn(code, log)


if __name__ == "__main__":
    print(json.dumps(_scenario(sys.argv[1])))
