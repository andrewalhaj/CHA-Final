"""Prove each C2 lint step in the CI workflow against its failing and passing cases (CONTRIBUTING.md C6, 8.2).

Each test takes a step's `run` script from the workflow and runs it the way GitHub runs `shell: bash`
(`bash --noprofile --norc -eo pipefail`) inside a throwaway git repository built for the case.
A failing case passes only when the step fails with the message that names its cause.
Needs `git`, `ruff` and `python` on PATH, as the CI job has them.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from test_bootstrap import ROOT, _workflow

NOQA = "no" + "qa"  # kept apart so the suppression ban does not match this file
RUFF = "ruff" + ":"  # the same, for ruff's ignore/disable/file-ignore comments
CONFIG = '[tool.ruff.lint]\nextend-select = ["BLE001", "S110"]\n'
CODE = "x = 1\n"


def _step(name):
    steps = _workflow(ROOT / ".github/workflows/ci.yml")["jobs"]["tests"]["steps"]
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r} in ci.yml"
    assert matches[0].get("shell") == "bash", f"step {name!r} must set shell: bash, so pipefail applies"
    return matches[0]["run"]


def _run(script, files):
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for name, body in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-f", name], check=True)
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=repo, capture_output=True, text=True, check=False,  # the exit code is the result
        )
    return result.returncode, result.stdout + result.stderr


class LintPolicyTests(unittest.TestCase):
    def _check(self, script, cases):
        for label, files, expect in cases:
            with self.subTest(case=label):
                code, output = _run(script, files)
                if expect is None:
                    self.assertEqual(code, 0, output)
                else:
                    self.assertNotEqual(code, 0, output)
                    self.assertIn(expect, output)

    def test_root_pyproject_switches_the_rules_on(self):
        code, output = _run(_step(PROBE_STEP), {"pyproject.toml": (ROOT / "pyproject.toml").read_text(encoding="utf-8")})
        self.assertEqual(code, 0, output)

    def test_suppression_ban(self):
        self._check(_step(NOQA_STEP), [
            ("clean", {"a.py": CODE}, None),
            ("another rule only", {"a.py": f"import os  # {NOQA}: F401\n"}, None),
            ("names BLE001", {"a.py": f"x = 1  # {NOQA}: BLE001\n"}, "git grep exited 0"),
            ("names S110 in another case", {"a.py": f"x = 1  # {NOQA.upper()}: s110\n"}, "git grep exited 0"),
            ("lists E722 after another rule", {"a.py": f"x = 1  # {NOQA}: F401, E722\n"}, "git grep exited 0"),
            ("bare", {"a.py": f"x = 1  # {NOQA}\n"}, "git grep exited 0"),
            ("bare, no space, trailing text", {"a.py": f"x = 1  #{NOQA}  because\n"}, "git grep exited 0"),
            ("file-level ruff", {"a.py": f"# ruff: {NOQA}\nx = 1\n"}, "git grep exited 0"),
            ("file-level flake8", {"a.py": f"# flake8: {NOQA}\nx = 1\n"}, "git grep exited 0"),
            ("ruff ignore comment", {"a.py": f"x = 1  # {RUFF} ignore[BLE001]\n"}, "git grep exited 0"),
            ("ruff disable comment", {"a.py": f"# {RUFF} disable[S110]\nx = 1\n"}, "git grep exited 0"),
            ("ruff file-ignore comment", {"a.py": f"# {RUFF} file-ignore[E722]\nx = 1\n"}, "git grep exited 0"),
        ])

    def test_suppression_ban_fails_when_git_fails(self):
        script = _step(NOQA_STEP).replace("git grep -n", "git --not-a-real-option grep -n")
        self._check(script, [("git fails", {"a.py": CODE}, "git grep exited 129")])

    def test_rules_probe(self):
        self._check(_step(PROBE_STEP), [
            ("configured", {"pyproject.toml": CONFIG}, None),
            ("ignore BLE001", {"pyproject.toml": CONFIG + 'ignore = ["BLE001"]\n'}, "ruff no longer reports BLE001"),
            ("extend-ignore prefix E", {"pyproject.toml": CONFIG + 'extend-ignore = ["E"]\n'},
             "ruff no longer reports E722"),
            ("select replaces defaults", {"pyproject.toml": '[tool.ruff.lint]\nselect = ["F"]\n'}, "ruff exited 0"),
            ("top-level ignore E722", {"pyproject.toml": '[tool.ruff]\nignore = ["E722"]\n' + CONFIG},
             "ruff no longer reports E722"),
            ("root ruff.toml ignores S110",
             {"pyproject.toml": CONFIG, "ruff.toml": '[lint]\nextend-select = ["BLE001", "S110"]\nignore = ["S110"]\n'},
             "ruff no longer reports S110"),
        ])

    def test_rules_probe_fails_when_ruff_is_missing(self):
        script = _step(PROBE_STEP).replace("| ruff check", "| /nonexistent/ruff check")
        self._check(script, [("ruff missing", {"pyproject.toml": CONFIG}, "ruff exited 127")])

    def test_coverage(self):
        self._check(_step(COVERAGE_STEP), [
            ("every file, nested and tests",
             {"pyproject.toml": CONFIG, "a.py": CODE, "pkg/b.py": CODE, "tests/test_c.py": CODE}, None),
            ("include narrows to src", {"pyproject.toml": '[tool.ruff]\ninclude = ["src/**/*.py"]\n' + CONFIG,
                                        "a.py": CODE, "src/b.py": CODE}, "does not lint:\na.py"),
            ("default exclude dist", {"pyproject.toml": CONFIG, "dist/y.py": CODE}, "does not lint:\ndist/y.py"),
            ("gitignore matches a tracked file",
             {"pyproject.toml": CONFIG, ".gitignore": "ignored.py\n", "a.py": CODE, "ignored.py": CODE},
             "does not lint:\nignored.py"),
            ("extend-exclude", {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["pkg/"]\n' + CONFIG,
                                "a.py": CODE, "pkg/b.py": CODE}, "does not lint:\npkg/b.py"),
        ])

    def test_coverage_fails_when_a_tool_is_missing(self):
        for tool in ("ruff", "git"):
            script = _step(COVERAGE_STEP).replace(f'run("{tool}",', f'run("/nonexistent/{tool}",')
            self._check(script, [(f"{tool} missing", {"pyproject.toml": CONFIG, "a.py": CODE}, f"/nonexistent/{tool}")])

    def test_config_location(self):
        self._check(_step(CONFIG_STEP), [
            ("root pyproject only", {"pyproject.toml": CONFIG, "a.py": CODE}, None),
            ("per-file ignore for another rule",
             {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["F401"]\n'}, None),
            ("nested ruff.toml", {"pyproject.toml": CONFIG, "pkg/ruff.toml": '[lint]\nignore = ["BLE001"]\n'},
             OUTSIDE + "pkg/ruff.toml"),
            ("nested .ruff.toml", {"pyproject.toml": CONFIG, "pkg/.ruff.toml": '[lint]\nignore = ["BLE001"]\n'},
             OUTSIDE + "pkg/.ruff.toml"),
            ("nested pyproject.toml",
             {"pyproject.toml": CONFIG, "pkg/pyproject.toml": '[tool.ruff.lint]\nignore = ["E722"]\n'},
             OUTSIDE + "pkg/pyproject.toml"),
            ("root ruff.toml", {"pyproject.toml": CONFIG, "ruff.toml": "line-length = 100\n"}, OUTSIDE + "ruff.toml"),
            ("tool.ruff extend-exclude", {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["pkg/"]\n' + CONFIG},
             "extend-exclude: pkg/"),
            ("tool.ruff.lint exclude", {"pyproject.toml": CONFIG + 'exclude = ["x.py"]\n'}, "exclude: x.py"),
            ("per-file ignore S110",
             {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["S110"]\n'},
             "per-file-ignores: tests/*: S110"),
            ("per-file ignore ALL",
             {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["ALL"]\n'},
             "per-file-ignores: tests/*: ALL"),
            ("extend-per-file ignore prefix E",
             {"pyproject.toml": CONFIG + '[tool.ruff.lint.extend-per-file-ignores]\n"x.py" = ["E"]\n'},
             "extend-per-file-ignores: x.py: E"),
            ("no pyproject.toml", {"a.py": CODE}, "no pyproject.toml at the repository root"),
        ])

    def test_config_location_fails_when_git_is_missing(self):
        script = _step(CONFIG_STEP).replace('["git", "ls-files"', '["/nonexistent/git", "ls-files"')
        self._check(script, [("git missing", {"pyproject.toml": CONFIG}, "/nonexistent/git")])


NOQA_STEP = "no suppression that can silence the error-handling rules"
PROBE_STEP = "the error-handling rules are switched on"
COVERAGE_STEP = "ruff lints every tracked Python file"
CONFIG_STEP = "ruff configuration lives in the root pyproject.toml and narrows nothing"
OUTSIDE = "ruff configuration outside the root pyproject.toml: "
