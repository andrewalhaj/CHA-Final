"""Enforce the repository's catch-all exception policy."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def broad_handler_lines(path, source):
    """Return line numbers of disallowed handlers."""
    return []


class ExceptionPolicyTests(unittest.TestCase):
    def test_repository_has_no_disallowed_handlers(self):
        files = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        violations = [
            f"{path}:{line}"
            for path in sorted(set(files.decode().split("\0")) - {""})
            for line in broad_handler_lines(path, (ROOT / path).read_bytes())
        ]
        self.assertEqual(violations, [], "\n".join(violations))

    def test_rejects_broad_handlers_even_when_they_log_and_re_raise(self):
        for clause in ("", " Exception", " BaseException", " builtins.Exception", " (ValueError, builtins.BaseException)"):
            with self.subTest(clause=clause):
                source = f"try:\n    work()\nexcept{clause}:\n    logger.exception()\n    raise\n"
                self.assertEqual(broad_handler_lines("scripts/probe.py", source), [3])

    def test_allows_narrow_handlers_and_finally(self):
        source = "try:\n    work()\nexcept (OSError, ValueError):\n    raise\nfinally:\n    close()\n"
        self.assertEqual(broad_handler_lines("scripts/probe.py", source), [])

    def test_exempts_only_named_wrappers_in_failure_module(self):
        source = (
            "def boundary():\n"
            "    def wrapped():\n"
            "        try:\n"
            "            work()\n"
            "        except Exception:\n"
            "            raise\n"
            "    return wrapped\n"
            "def run_job():\n"
            "    try:\n"
            "        work()\n"
            "    except BaseException:\n"
            "        raise\n"
            "def unrelated():\n"
            "    try:\n"
            "        work()\n"
            "    except:\n"
            "        raise\n"
        )
        self.assertEqual(broad_handler_lines("custom_build/failures.py", source), [16])
        self.assertEqual(broad_handler_lines("scripts/probe.py", source), [5, 11, 16])
