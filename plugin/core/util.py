"""Low-level helpers with no intra-package dependencies: stdin parsing, the data dir, and git
lookups. Kept dependency-free so config/scope can import it without import cycles."""

import json
import os
import subprocess
import sys
from typing import Any


def read_input() -> dict[str, Any]:
    """Parse the hook payload (a JSON object on stdin). A malformed payload raises — a hook
    should never run on garbage input."""
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        raise TypeError(f"hook payload must be a JSON object, got {type(data).__name__}")
    return data


def debug(event: str, **fields: Any) -> None:
    if not os.environ.get("ENGRAM_DEBUG"):
        return
    detail = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    sys.stderr.write(f"Engram · {event} {detail}\n")


def data_dir() -> str:
    """Plugin data dir (the venv, the schema cache, and state flags), provided by the assistant.
    No guessed path on purpose: it would diverge between entry points and make bugs untraceable,
    so fail loudly if neither variable is set.

    The specific name is read first. Codex sets PLUGIN_DATA and aliases CLAUDE_PLUGIN_DATA to it,
    so preferring the generic one would let a stray PLUGIN_DATA override the real value."""
    d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("PLUGIN_DATA")
    if not d:
        raise RuntimeError("neither CLAUDE_PLUGIN_DATA nor PLUGIN_DATA is set")
    return d


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
