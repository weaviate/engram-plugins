"""Engram connection + identity: API key / base URL resolution, the SDK client, and the
REST helper. Fails open — callers exit 0 so a coding session is never broken."""

import json
import os
import subprocess
import urllib.request

from .client_origin import client_origin_header


DEFAULT_BASE = "https://api.engram.weaviate.io"

_PROFILES = (
    "~/.zshenv",
    "~/.zshrc",
    "~/.zprofile",
    "~/.bashrc",
    "~/.bash_profile",
    "~/.profile",
)


def _from_profile(var):
    """Extract `export VAR=value` from shell profiles. A GUI host (e.g. Claude Desktop)
    doesn't inherit exported shell env, so this recovers vars set only in a profile."""
    prefix = var + "="
    for path in _PROFILES:
        try:
            with open(os.path.expanduser(path)) as f:
                lines = f.readlines()
        except Exception:
            continue  # this profile doesn't exist / isn't readable — try the next
        for line in reversed(lines):
            s = line.strip()
            if s.startswith("export "):
                s = s[7:].strip()
            if not s.startswith(prefix):
                continue
            val = s[len(prefix) :].strip()
            if val[:1] in ("'", '"'):
                end = val.find(val[0], 1)
                val = val[1:end] if end > 0 else val[1:]
            else:
                val = val.split("#", 1)[0].strip()
            if val and not val.startswith("$"):
                return val
    return None


def _config(env_var):
    """Resolve config (first non-empty wins): exported env var → shell-profile extraction.
    The profile fallback recovers vars set only in a shell profile when the host (e.g. Claude
    Desktop) doesn't inherit exported shell env."""
    return os.environ.get(env_var) or _from_profile(env_var)


def engram_api_key():
    return _config("ENGRAM_API_KEY")


def engram_base_url():
    # Dev/override only. Env (or profile fallback) → default.
    return (
        os.environ.get("ENGRAM_BASE_URL")
        or _from_profile("ENGRAM_BASE_URL")
        or DEFAULT_BASE
    )


def get_user_id():
    """Stable identity for memory scoping. ENGRAM_USER_ID (env or shell profile), then git email.
    Returns None when neither is set — callers MUST NOT fall back to a shared id like $USER or
    "default": memories are tagged with this id in Engram permanently, so a non-unique id would
    commingle different people's memories with no way to un-mix them later."""
    uid = _config("ENGRAM_USER_ID")  # env or shell-profile fallback (like the API key)
    if uid:
        return uid
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None  # git missing / timed out → no identity; engram_warning reports it
    return out.stdout.strip() or None


def get_client():
    # SDK import stays local: everything else in this module (key/identity resolution, the
    # REST helper) is stdlib-only and must keep working where the SDK isn't installed.
    from engram import EngramClient

    api_key = engram_api_key()
    if not api_key:
        return None
    return EngramClient(
        api_key=api_key, base_url=engram_base_url(), headers=client_origin_header()
    )


def engram_warning():
    """Plain-text reason the plugin can't reach Engram, or None if it looks usable. The search
    hook prefixes it and surfaces it through the UserPromptSubmit directive."""
    if not engram_api_key():
        return "API key not set — memory disabled this session."
    if not get_user_id():
        return "no stable identity — set git user.email or ENGRAM_USER_ID to enable memory (prevents mixing memories between users)."
    return None


def engram_get(path):
    """GET a JSON path from the Engram REST API and return the parsed JSON. Raises on any HTTP
    or network error — the caller resolving scope lets it propagate so the operation fails as a
    whole instead of continuing with a half-resolved scope."""
    base = engram_base_url().rstrip("/")
    req = urllib.request.Request(
        base + path,
        headers={
            "Authorization": f"Bearer {engram_api_key()}",
            **client_origin_header(),
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)
