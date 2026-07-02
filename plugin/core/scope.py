"""Scope resolution: turn the active scope's required properties into concrete values via
deterministic sources (no LLM inference). The requirements are read live from /v1/groups so
only the configured properties are ever sent."""

import json
import os
import subprocess

from .client import engram_get
from .config import load_config
from .util import data_dir, git_repo

# Built-in defaults for well-known properties — applied only when the live schema lists them and
# the user didn't configure them. Any other property is the user's to map in .engram.json.
DEFAULT_SOURCES = {
    # cwd fallback when git-repo yields nothing: no git remote, or run from a non-repo (e.g. parent) dir
    "repo_name": [{"from": "git-repo"}, {"from": "cwd"}],
    "session_id": {"from": "session_id"},
}

# The built-in `from` tokens — kept in sync with _resolve_token; used to flag typos in config.
VALID_TOKENS = ("git-repo", "cwd", "session_id")


def _resolve_token(token, cwd, session_id):
    """Resolve a built-in source token (no arguments): "git-repo" (owner-scoped repo name from the
    remote, e.g. owner/repo), "cwd", or "session_id" (the last two from the hook payload). Unknown
    token → None."""
    if token == "git-repo":
        return git_repo(cwd)
    if token == "cwd":
        return cwd
    if token == "session_id":
        return session_id or None
    return None


def _resolve_element(el, cwd, session_id):
    """Resolve one value / cascade element to a value (or None). JSON shape decides, with no
    possible collision: a string is a literal (used verbatim); a {"from": <token>} object is a
    built-in source; a {"cmd": ["prog", "arg", ...]} object runs argv directly (no shell, so no
    quoting/splitting pitfalls)."""
    if isinstance(el, str):
        return el.strip() or None
    if isinstance(el, dict):
        if "cmd" in el:
            cmd = el["cmd"]
            if not isinstance(cmd, list) or not cmd:
                return None
            out = subprocess.run(
                [str(a) for a in cmd],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return out.stdout.strip() or None
        if "from" in el:
            return _resolve_token(el["from"], cwd, session_id)
    return None


def _resolve_value(value, cwd, session_id):
    """Resolve a configured property value. A JSON array is a cascade — its elements (literals,
    {"from": ...} or {"cmd": ...}) are tried in order and the first non-empty wins; any other value
    is resolved as a single element. This array replaces the old `a|b` string: explicit, not
    parsed, and a bare-string element is unambiguously a literal fallback."""
    for el in value if isinstance(value, list) else [value]:
        try:
            val = _resolve_element(el, cwd, session_id)
        except Exception:
            val = None  # this element errored (e.g. a cmd timeout) → fall through to the next
        if val:
            return str(val).strip()
    return None


def scope_schema():
    """The active group's write requirements, fetched from /v1/groups and cached. Only one group
    is supported, so we take the response's default group and pass nothing to the API. The cache
    stores the raw group payload plus the derived `properties` (UNION of every topic's
    scope_properties) and `user` (any topic user-scoped). No expiry — once written it's reused
    (the schema rarely changes); reinstall to refresh it. Raises if the fetch fails and no cache."""
    cache = os.path.join(data_dir(), "scope-schema.json")
    try:
        with open(cache) as f:
            return json.load(f)
    except Exception:
        pass  # missing or corrupt cache → fall through and re-fetch (recovers it)

    data = engram_get("/v1/groups")  # raises on failure → propagates to the hook
    groups = data.get("groups") or []
    group = next(
        (g for g in groups if g.get("name") == "default"), groups[0] if groups else {}
    )
    topics = group.get("topics", [])
    user = any((t.get("scoping") or {}).get("user_scoped") for t in topics)
    props = []
    for t in topics:
        for p in (t.get("scoping") or {}).get("scope_properties") or []:
            if p not in props:
                props.append(p)
    schema = {"user": user, "properties": props, "raw": group}
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w") as f:
            json.dump(schema, f)
    except Exception:
        pass  # best-effort cache; the schema is returned in-memory regardless
    return schema


def resolve_scope(cwd, session_id):
    """Resolve the scope properties to attach to a memory. Config always wins and is always sent;
    the cached schema's ONLY job is to pick which built-in DEFAULT_SOURCES to infer for properties
    the user didn't configure — it does not gate, require, or limit anything. So a stale schema
    (e.g. after switching projects) can be fully overridden from .engram.json without reinstalling
    to refresh the cache. The server, not the local schema, is the authority on what a store needs.

    Returns (props, user_required, unmapped) — `unmapped` lists configured/inferred properties that
    didn't resolve to a value, so the user can fix the source."""
    configured = load_config(cwd).get("properties", {})
    schema = scope_schema()
    # Schema → defaults only: infer a DEFAULT_SOURCE for each schema property the user left unset.
    defaults = {
        name: DEFAULT_SOURCES[name]
        for name in schema.get("properties", [])
        if name in DEFAULT_SOURCES and name not in configured
    }
    to_resolve = {**defaults, **configured}  # config overrides the inferred defaults
    props, unmapped = {}, []
    for name, value in to_resolve.items():
        val = _resolve_value(value, cwd, session_id)  # already a stripped string or None
        if val:
            props[name] = val
        else:
            unmapped.append(name)
    return props, bool(schema.get("user")), unmapped


def _element_warnings(label, el):
    """Structural problems in one property value / cascade element (recurses into cascades).
    A string is a literal (always fine); a dict must be {"from": token} or {"cmd": [args]}."""
    if isinstance(el, str):
        return []
    if isinstance(el, list):
        out = []
        for i, item in enumerate(el):
            out += _element_warnings(f"{label}[{i}]", item)
        return out
    if isinstance(el, dict):
        if "cmd" in el:
            cmd = el["cmd"]
            if not isinstance(cmd, list) or not cmd:
                return [f'{label}: "cmd" must be a non-empty array of arguments']
            return []
        if "from" in el:
            if el["from"] not in VALID_TOKENS:
                return [
                    f'{label}: unknown source {el["from"]!r} '
                    f'(valid: {", ".join(VALID_TOKENS)})'
                ]
            return []
        return [f'{label}: expected {{"from": ...}} or {{"cmd": [...]}} (keys: {list(el)})']
    return [f"{label}: must be a string, a source object, or an array"]


def config_warnings(cwd):
    """Human-readable structural problems in the configured `properties` — a wrong/renamed source
    key, an unknown token, a malformed cmd — so a typo'd config is surfaced instead of silently
    resolving to nothing. (Malformed JSON is caught earlier and fails loud with the file named; a
    dynamic source in a local file is dropped by load_config with its own warning.)"""
    props = load_config(cwd).get("properties", {})
    out = []
    for name, value in props.items():
        out += _element_warnings(f"properties.{name}", value)
    return out
