"""Shared, assistant-agnostic core for the Weaviate Engram memory plugins.

core.hooks holds the work each hook does, written against the core.assistant contract. What
differs between assistants lives in assistants/, and entry/ picks one. Nothing here imports
either, so a new assistant never touches core."""

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
from .util import data_dir, debug, read_input

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
    "data_dir",
    "debug",
    "read_input",
]
