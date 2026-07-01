"""Shared, assistant-agnostic core for the Weaviate Engram memory plugins.

Hosts differ only in their plugin manifest; the hook entrypoints in core.hooks speak the
host's stdin/stdout protocol."""

from .client import (
    engram_api_key,
    engram_base_url,
    engram_get,
    engram_warning,
    get_client,
    get_user_id,
)
from .config import load_config, user_config_path
from .scope import resolve_scope, scope_schema
from .search import search_filters
from .transcript import last_user_text
from .util import data_dir, read_input

__all__ = [
    "engram_api_key",
    "engram_base_url",
    "engram_get",
    "engram_warning",
    "get_client",
    "get_user_id",
    "load_config",
    "user_config_path",
    "resolve_scope",
    "scope_schema",
    "search_filters",
    "last_user_text",
    "data_dir",
    "read_input",
]
