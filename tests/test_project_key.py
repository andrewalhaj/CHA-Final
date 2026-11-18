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

    def test_public_forge_aliases_share_a_digest_from_a_subdirectory(self):
        repo = self.repo("work")
        subdirectory = repo / "nested"
        subdirectory.mkdir()
        for host in ("github.com", "gitlab.com", "bitbucket.org"):
            expected = hashlib.sha256(f"{host}/Team/Project".encode()).hexdigest()
            for origin in (
                f"https://{host}/Team/Project.git",
                f"https://fixture-user:fixture-pass@{host.upper()}:443/Team/Project.git/?x=fixture#piece",
                f"http://{host}:80/Team/Project",
                f"ssh://fixture-user@{host.upper()}:22/Team/Project.git",
                f"fixture-user@{host.upper()}:Team/Project.git",
                f"git://{host}:9418/Team/Project.git",
            ):
                with self.subTest(origin=origin):
                    self.git(repo, "config", "remote.origin.url", origin)
                    self.assertEqual(self.project.key(subdirectory), expected)

    def test_nondefault_ports_remain_distinct(self):
        repo = self.repo("work")
        for port in (2200, 2201):
            with self.subTest(port=port):
                self.git(repo, "config", "remote.origin.url", f"ssh://github.com:{port}/Team/Project.git")
                expected = hashlib.sha256(f"ssh://github.com:{port}/Team/Project.git".encode()).hexdigest()
                self.assertEqual(self.project.key(repo), expected)

    def test_generic_ssh_home_relative_origins_refuse_scope(self):
        repo = self.repo("work")
        for origin in (
            "alice@git.example:Team/Project.git",
            "bob@git.example:Team/Project.git",
            "ssh://alice@git.example/~/Team/Project.git",
            "ssh://bob@git.example/%7E/Team/Project.git",
        ):
            with self.subTest(origin=origin):
                self.git(repo, "config", "remote.origin.url", origin)
                with self.assertRaisesRegex(ValueError, "^Ambiguous Git SSH origin path$"):
                    self.project.key(repo)

    def test_generic_origins_keep_transport_and_path(self):
        repo = self.repo("work")
        for origin, canonical in (
            ("ssh://alice@GIT.EXAMPLE:22/srv/project.git", "ssh://git.example/srv/project.git"),
            ("alice@git.example:/srv/project.git", "ssh://git.example/srv/project.git"),
            ("ssh://alice@git.example/srv/project", "ssh://git.example/srv/project"),
            ("https://git.example/srv/project.git?key=fixture#part", "https://git.example/srv/project.git"),
        ):
            with self.subTest(origin=origin):
                self.git(repo, "config", "remote.origin.url", origin)
                expected = hashlib.sha256(canonical.encode()).hexdigest()
                self.assertEqual(self.project.key(repo), expected)

    def test_scp_percent_escapes_are_literal_paths(self):
        repo = self.repo("work")
        self.git(repo, "config", "remote.origin.url", "git@git.example:/srv/project%20one.git")
        literal_path_key = self.project.key(repo)
        self.git(repo, "config", "remote.origin.url", "ssh://git@git.example/srv/project%20one.git")
        with self.subTest("URL escape addresses a space"):
            self.assertNotEqual(self.project.key(repo), literal_path_key)
        self.git(repo, "config", "remote.origin.url", "ssh://git@git.example/srv/project%2520one.git")
        with self.subTest("URL escape addresses the literal percent"):
            self.assertEqual(self.project.key(repo), literal_path_key)

    def test_unknown_network_transport_raises_instead_of_selecting_unscoped_memory(self):
        repo = self.repo("work")
        self.git(repo, "config", "remote.origin.url", "custom://git.example/Team/Project")
        with self.assertRaisesRegex(ValueError, "^Unsupported Git network transport$"):
            self.project.key(repo)

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
            expected = hashlib.sha256(b"https://git.example/Team/Requested").hexdigest()
            self.assertEqual(self.project.key(requested), expected)

    def test_environment_url_rewrite_does_not_change_the_configured_key(self):
        repo = self.repo("work")
        self.git(repo, "config", "remote.origin.url", "https://github.com/Team/Project.git")
        expected = hashlib.sha256(b"github.com/Team/Project").hexdigest()
        with patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.https://alternate.example/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
        }):
            self.assertEqual(self.project.key(repo), expected)

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
