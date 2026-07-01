"""Low-level helpers with no intra-package dependencies: stdin parsing, the data dir, and git
lookups. Kept dependency-free so config/scope can import it without import cycles."""

import json
import os
import subprocess
import sys


def read_input():
    """Parse the hook payload (JSON on stdin). A malformed payload raises — a hook should
    never run on garbage input."""
    return json.load(sys.stdin)


def data_dir():
    """Plugin data dir (the venv, the schema cache, and state flags), provided by the host via
    CLAUDE_PLUGIN_DATA. No fallback on purpose: a guessed path diverges between entry points
    (hooks vs the setup command) and makes bugs untraceable, so fail loudly if it's not set."""
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        raise RuntimeError("CLAUDE_PLUGIN_DATA not set — the host provides the plugin data dir")
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
    git@github.com:organization/repository.git -> organization-repository."""
    url = git_out(cwd, ["remote", "get-url", "origin"])
    if not url:
        return None
    slug = url[:-4] if url.endswith(".git") else url
    for p in ("https://", "http://", "ssh://", "git://", "git@"):
        if slug.startswith(p):
            slug = slug[len(p) :]
    slug = slug.replace(":", "/")
    parts = [p for p in slug.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}-{parts[-1]}"
    return parts[-1] if parts else None
