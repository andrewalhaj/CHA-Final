"""Test stable, opaque project keys without accessing Git remotes."""

import hashlib
import importlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ProjectKeyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "custom_build/memory/project.py").is_file(), "the project key helper is missing")
        self.project = importlib.import_module("custom_build.memory.project")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        config.start()
        self.addCleanup(config.stop)

    def git(self, cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    def repo(self, name):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-q")
        return path

    def test_common_network_aliases_share_a_digest_from_a_subdirectory(self):
        repo = self.repo("work")
        subdirectory = repo / "nested"
        subdirectory.mkdir()
        expected = hashlib.sha256(b"git.example/Team/Project").hexdigest()
        for origin in (
            "https://git.example/Team/Project.git",
            "https://fixture-user:fixture-pass@GIT.EXAMPLE:443/Team/Project.git/?x=fixture#piece",
            "http://git.example:80/Team/Project",
            "ssh://fixture-user@GIT.EXAMPLE:22/Team/Project.git",
            "fixture-user@GIT.EXAMPLE:Team/Project.git",
            "git://git.example:9418/Team/Project.git",
        ):
            with self.subTest(origin=origin):
                self.git(repo, "config", "remote.origin.url", origin)
                self.assertEqual(self.project.key(subdirectory), expected)

    def test_nondefault_ports_remain_distinct(self):
        repo = self.repo("work")
        for port in (2200, 2201):
            with self.subTest(port=port):
                self.git(repo, "config", "remote.origin.url", f"ssh://git.example:{port}/Team/Project.git")
                expected = hashlib.sha256(f"git.example:{port}/Team/Project".encode()).hexdigest()
                self.assertEqual(self.project.key(repo), expected)

    def test_local_file_missing_and_non_repository_have_no_key(self):
        repo = self.repo("work")
        self.assertIsNone(self.project.key(repo))
        self.assertIsNone(self.project.key(self.root))
        for origin in ("/path/to/project.git", "../project.git", "./local:project", "file://host/path/project.git"):
            with self.subTest(origin=origin):
                self.git(repo, "config", "remote.origin.url", origin)
                self.assertIsNone(self.project.key(repo))

    def test_git_environment_cannot_override_the_requested_repository(self):
        requested = self.repo("requested")
        other = self.repo("other")
        self.git(requested, "config", "remote.origin.url", "https://git.example/Team/Requested")
        self.git(other, "config", "remote.origin.url", "https://git.example/Team/Other")
        with patch.dict(os.environ, {
            "GIT_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
            "GIT_COMMON_DIR": str(other / ".git"),
        }):
            expected = hashlib.sha256(b"git.example/Team/Requested").hexdigest()
            self.assertEqual(self.project.key(requested), expected)

    def test_another_git_failure_remains_visible_without_its_output(self):
        origin = "https://fixture-user:private-content@git.example/Team/Project"
        failure = subprocess.CompletedProcess([], 2, origin, f"error: failed to examine {origin}")
        with (
            patch("custom_build.memory.project.subprocess.run", return_value=failure),
            self.assertRaises(subprocess.CalledProcessError) as caught,
        ):
            self.project.key(self.root)

        self.assertEqual(caught.exception.returncode, 2)
        self.assertIsNone(caught.exception.stdout)
        self.assertIsNone(caught.exception.stderr)
        self.assertNotIn(origin, str(caught.exception))

    def test_malformed_network_url_raises_a_fixed_message(self):
        repo = self.repo("work")
        self.git(repo, "config", "remote.origin.url", "https://fixture-user:private-content@[private-host]/project")
        with self.assertRaisesRegex(ValueError, "^Invalid Git network origin$") as caught:
            self.project.key(repo)

        self.assertTrue(caught.exception.__suppress_context__)
