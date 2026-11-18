"""Derive an opaque project identifier from a Git network origin."""

import hashlib
import os
import re
import subprocess
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443, "ssh": 22, "git": 9418}
_SCP_ORIGIN = re.compile(r"((?:[^@:/\\]+@)?(?:\[[^\]]+\]|[^@:/\\]+)):(?!:)(.+)")


def key(cwd: Path) -> str | None:
    """Return an opaque project key for the Git repository containing cwd."""
    args = ["remote", "get-url", "origin"]
    env = {**os.environ, "LC_ALL": "C"}
    # Hermes's session directory owns the lookup, even when launched from another Git repository.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, env=env,
    )
    if result.returncode == 0:
        origin = _canonical_origin(result.stdout.strip())
        return hashlib.sha256(origin.encode()).hexdigest() if origin else None
    if result.returncode == 2 and result.stderr.strip() == "error: No such remote 'origin'":
        return None
    if result.returncode == 128 and result.stderr.startswith("fatal: not a git repository"):
        return None
    raise subprocess.CalledProcessError(result.returncode, ["git", *args])


def _canonical_origin(origin: str) -> str | None:
    if "://" not in origin:
        # Git treats a colon before the first slash as SCP syntax. A Windows drive path stays local.
        scp = _SCP_ORIGIN.fullmatch(origin)
        if scp is None or PureWindowsPath(origin).is_absolute():
            return None
        authority, path = scp.groups()
        origin = f"ssh://{authority}/{path.lstrip('/')}"

    scheme = origin.partition("://")[0].lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        raise ValueError("Invalid Git network origin") from None

    host = parts.hostname
    path = parts.path.strip("/").removesuffix(".git")
    if not host or not path:
        raise ValueError("Invalid Git network origin")
    authority = f"[{host.lower()}]" if ":" in host else host.lower()
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        authority = f"{authority}:{port}"
    return f"{authority}/{path}"
