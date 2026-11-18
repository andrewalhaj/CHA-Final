"""Verify the documentation baseline and the bootstrap workflow pins."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ("CONTRIBUTING.md", "ARCHITECTURE.md", "OPERATIONS.md")


class BootstrapTests(unittest.TestCase):
    def test_readme_links_to_the_governing_guides(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for filename in GUIDES:
            with self.subTest(filename=filename):
                self.assertTrue((ROOT / filename).is_file(), f"{filename} is missing")
                self.assertIn(f"]({filename})", readme)

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
            uses = re.findall(
                r"^[ \t]*(?:-[ \t]+)?uses:[ \t]+([^#\s]+)",
                workflow.read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
            self.assertTrue(uses, f"No action uses found in {workflow.name}")
            for action in uses:
                with self.subTest(action=action):
                    self.assertRegex(action, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
