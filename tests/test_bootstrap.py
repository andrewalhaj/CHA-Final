"""Verify the documentation baseline and the bootstrap workflow pins."""

import json
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ("CONTRIBUTING.md", "ARCHITECTURE.md", "OPERATIONS.md")


def _workflow(path):
    parsed = subprocess.run(
        ["ruby", "-rjson", "-ryaml", "-e",
         "puts JSON.generate(YAML.safe_load(File.read(ARGV.fetch(0))))", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(parsed.stdout)


class BootstrapTests(unittest.TestCase):
    def test_readme_links_to_the_governing_guides(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Current scope", readme)
        for filename in GUIDES:
            with self.subTest(filename=filename):
                self.assertTrue((ROOT / filename).is_file(), f"{filename} is missing")
                self.assertIn(f"]({filename})", readme)

    def test_agent_instructions_link_to_the_governing_guides(self):
        instructions = ROOT / "AGENTS.md"
        self.assertTrue(instructions.is_file(), "The root AGENTS.md is missing")
        text = instructions.read_text(encoding="utf-8")
        for filename in GUIDES:
            with self.subTest(filename=filename):
                self.assertIn(f"]({filename})", text)
                self.assertTrue((ROOT / filename).is_file())

    def test_allow_list_exposes_the_guides_and_hides_local_secrets(self):
        def ignore_status(filename):
            return subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", filename],
                cwd=ROOT,
                check=False,
            ).returncode

        for filename in GUIDES:
            with self.subTest(filename=filename):
                self.assertEqual(ignore_status(filename), 1)
        self.assertEqual(ignore_status(".env"), 0)

    def test_workflow_actions_use_full_commit_shas(self):
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows, "The bootstrap workflow is missing")
        for workflow in workflows:
            jobs = _workflow(workflow)["jobs"].values()
            callers = [*jobs, *(step for job in jobs for step in job.get("steps", []))]
            uses = [caller["uses"] for caller in callers if "uses" in caller]
            self.assertTrue(uses, f"No action uses found in {workflow.name}")
            for action in uses:
                with self.subTest(action=action):
                    self.assertRegex(action, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")

    def test_required_job_uses_a_disposable_postgres_service(self):
        job = _workflow(ROOT / ".github/workflows/ci.yml")["jobs"]["tests"]
        self.assertIn("postgres", job.get("services", {}))
        service = job["services"]["postgres"]
        self.assertRegex(service["image"], r"-pg17@sha256:[0-9a-f]{64}$")
        self.assertIn("pg_isready", service["options"])
        self.assertIn("CHA_FINAL_TEST_DATABASE_URL", job.get("env", {}))
        dsn = urlsplit(job["env"]["CHA_FINAL_TEST_DATABASE_URL"])
        self.assertEqual(dsn.hostname, "127.0.0.1")
        self.assertIn(f"{dsn.port}:5432", service["ports"])
        self.assertEqual(dsn.path, "/cha_final_test")
        self.assertEqual(dsn.path[1:], service["env"]["POSTGRES_DB"])
        self.assertEqual(dsn.username, service["env"]["POSTGRES_USER"])
        self.assertEqual(dsn.password, service["env"]["POSTGRES_PASSWORD"])

    def test_required_job_checks_postgres_and_lints_before_unittest(self):
        job = _workflow(ROOT / ".github/workflows/ci.yml")["jobs"]["tests"]
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        ordered = ("psycopg[binary]==3.3.9", "ruff==0.17.4", "SELECT 1", "ruff check .", "unittest discover -s tests")
        for command in ordered:
            self.assertIn(command, commands)
        offsets = [commands.index(command) for command in ordered]
        self.assertEqual(offsets, sorted(offsets))
