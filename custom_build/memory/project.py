"""Derive an opaque project identifier from a Git network origin."""

import hashlib
import os
import re
import subprocess
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443, "ssh": 22, "git": 9418}
_PUBLIC_FORGES = frozenset({"github.com", "gitlab.com", "bitbucket.org"})
_SCP_ORIGIN = re.compile(r"((?:[^@:/\\]+@)?(?:\[[^\]]+\]|[^@:/\\]+)):(?!:)(.+)")


def key(cwd: Path) -> str | None:
    """Return an opaque project key for the Git repository containing cwd."""
    # Read the configured origin directly so Git's URL rewrites cannot change memory scope.
    args = ["config", "--local", "--get", "remote.origin.url"]
    env = {**os.environ, "LC_ALL": "C"}
    # Hermes's session directory owns the lookup, even when launched from another Git repository.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        env.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, env=env,
    )
    if result.returncode == 0:
        origin = _canonical_origin(result.stdout.strip())
        return hashlib.sha256(origin.encode()).hexdigest() if origin else None
    if result.returncode == 1 and not result.stderr.strip():
        return None
    if result.returncode == 128 and result.stderr.strip() == "fatal: --local can only be used inside a git repository":
        return None
    raise subprocess.CalledProcessError(result.returncode, ["git", *args])


def _canonical_origin(origin: str) -> str | None:
    relative_scp = False
    if "://" not in origin:
        # Git treats a colon before the first slash as SCP syntax. A Windows drive path stays local.
        scp = _SCP_ORIGIN.fullmatch(origin)
        if scp is None or PureWindowsPath(origin).is_absolute():
            return None
        authority, path = scp.groups()
        if "?" in path or "#" in path:
            raise ValueError("Invalid Git SCP origin path")
        relative_scp = not path.startswith("/")
        if relative_scp:
            path = f"/{path}"
        origin = f"ssh://{authority}{path}"

    scheme = origin.partition("://")[0].lower()
    if scheme == "file":
        return None
    if scheme not in _DEFAULT_PORTS:
        raise ValueError("Unsupported Git network transport")
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        raise ValueError("Invalid Git network origin") from None

    if not parts.hostname or not parts.path:
        raise ValueError("Invalid Git network origin")
    host = parts.hostname.lower()
    default_port = port in (None, _DEFAULT_PORTS[scheme])
    public_forge = host in _PUBLIC_FORGES and default_port
    if scheme == "ssh" and (unquote(parts.path).lstrip("/").startswith("~") or (relative_scp and not public_forge)):
        raise ValueError("Ambiguous Git SSH origin path")

    if public_forge:
        path = parts.path.strip("/").removesuffix(".git")
        if not path:
            raise ValueError("Invalid Git network origin")
        return f"{host}/{path}"

    authority = f"[{host}]" if ":" in host else host
    if not default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}{parts.path}"
