"""Shared, assistant-agnostic core for the Weaviate Engram memory plugins.

Hosts differ only in their plugin manifest; the hook entrypoints in core.hooks speak the
host's stdin/stdout protocol."""

from importlib import import_module

# Lazy re-exports (PEP 562): importing the package must not drag in the Engram SDK, so that
# SDK-free entry points (core.migrate dry-run, unit tests) work outside the plugin venv.
# `from core import X` in the hooks resolves through __getattr__ at their import time.
_EXPORTS = {
    "engram_api_key": ".client",
    "engram_base_url": ".client",
    "engram_get": ".client",
    "engram_warning": ".client",
    "get_client": ".client",
    "get_user_id": ".client",
    "load_config": ".config",
    "user_config_path": ".config",
    "resolve_scope": ".scope",
    "scope_schema": ".scope",
    "search_filters": ".search",
    "last_user_text": ".transcript",
    "data_dir": ".util",
    "read_input": ".util",
}


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)


__all__ = list(_EXPORTS)
