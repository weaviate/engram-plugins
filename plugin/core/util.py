"""Low-level helpers with no intra-package dependencies: debug logging and git lookups. Kept
dependency-free so config/scope can import it without import cycles."""

import os
import subprocess
import sys
from typing import Any


def debug(event: str, **fields: Any) -> None:
    if not os.environ.get("ENGRAM_DEBUG"):
        return
    detail = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    sys.stderr.write(f"Engram · {event} {detail}\n")


def git_out(cwd, args):
    # git missing / timeout → None; callers degrade gracefully. A non-zero exit (not a repo)
    # doesn't raise (no check=True) → empty stdout → None. cwd is always provided by the hook.
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    return out.stdout.strip() or None


def git_repo(cwd):
    """owner-scoped repo name from the git remote:
    git@github.com:organization/repository.git -> organization/repository."""
    url = git_out(cwd, ["remote", "get-url", "origin"])
    if not url:
        return None
    slug = url[:-4] if url.endswith(".git") else url
    for p in ("https://", "http://", "ssh://", "git://", "git@"):
        if slug.startswith(p):
            slug = slug[len(p) :]
    parts = [p for p in slug.replace(":", "/").split("/") if p]
    return "/".join(parts[-2:]) or None
