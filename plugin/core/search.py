"""Search-side scoping: build the Engram `topics` and cross-topic `properties` filters from
the config `search` block."""

from engram import Topic

from .config import load_config
from .scope import config_warnings, resolve_scope

# Cross-topic search filter used when config doesn't set search.properties — scopes recall to the
# current repo (only applied if the key resolves). Override via config, or clear with
# "search": {"properties": []} to search broadly.
DEFAULT_SEARCH_PROPERTIES = ["repo_name"]


def _build_topics(spec, resolved):
    """Build the SDK `topics` arg from the config `search.topics` list (already parsed from
    the JSON config file — no env quoting). Each item mirrors the API:

      "topic name"  (or {"name": "topic"})       restrict to this topic; the cross-topic
                                                  `properties` filter still applies to it
      {"name": "topic", "properties": ["key"]}    override: filter that topic by the key's
                                                  resolved value
      {"name": "topic", "clear_properties": ["key"]}   clear the cross-topic filter for that
                                                        key (→ None: search across all its values)

    'properties' values come from the unified resolution; 'clear_properties' maps to the API's
    per-topic `None` override. Returns (topics, error_message)."""
    if not spec:
        return None, None
    if not isinstance(spec, list):
        return None, "config search.topics must be a list (of names / {name, properties, clear_properties})"
    out = []
    for item in spec:
        if isinstance(item, str):
            out.append(item)  # the cross-topic filter still applies to a bare-name topic
        elif isinstance(item, dict) and item.get("name"):
            filt = {
                p: resolved[p]
                for p in (item.get("properties") or [])
                if resolved.get(p)
            }
            for k in item.get("clear_properties") or []:
                filt[k] = None  # clear the inherited cross-topic filter for this key
            # only need a Topic() when there's a per-topic override; otherwise the name
            # alone already inherits the cross-topic filter
            out.append(Topic(name=item["name"], properties=filt) if filt else item["name"])
    return (out or None), None


def search_filters(cwd, session_id):
    """Mirror the Engram search API's two independent, composable filters, read from the
    config `search` block. Both name only property KEYS; values come from the same
    resolution used when adding memories (resolve_scope).

    The cross-topic `properties` filter defaults to DEFAULT_SEARCH_PROPERTIES (repo_name) — applied
    only if it resolves — so recall is scoped to the current repo out of the box. A config
    search.properties overrides it; an explicit [] clears it (search broad). The presence of the
    key, not its truthiness, decides: absent → default, [] → cleared. `topics` has no default.

      search.properties: ["key", "key2"]   cross-topic filter — narrows EVERY topic ([] clears).
      search.topics: [ "name", {"name": ..., "properties": ["key"]}, ... ]
          the `topics` filter — RESTRICTS the search to these topics, with optional
          per-topic property filters.

    Use either or both. Returns (topics, properties, warnings, resolved) — `resolved` is the
    resolved scope (config + inferred defaults) used for the session banner; warnings flags any
    configured/inferred scope property that didn't resolve to a value, so it won't be attached."""
    cfg = load_config(cwd)
    search = cfg.get("search") or {}
    resolved, _user_required, unmapped = resolve_scope(cwd, session_id)
    prop_keys = (
        search["properties"] if "properties" in search else DEFAULT_SEARCH_PROPERTIES
    )
    cross = {k: resolved[k] for k in (prop_keys or []) if resolved.get(k)}
    topics, topics_err = _build_topics(search.get("topics"), resolved)
    warnings = [topics_err] if topics_err else []
    warnings += cfg.get("warnings", [])  # e.g. a dynamic source dropped from a local config
    warnings += config_warnings(cwd)
    if unmapped:
        warnings.append(
            f"scope [{', '.join(unmapped)}] did not resolve and won't be attached — "
            'check the source/env in your .engram.json "properties".'
        )
    return topics, (cross or None), warnings, resolved
