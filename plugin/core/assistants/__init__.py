"""Per-assistant differences live here; everything else in core is assistant-agnostic.

Supporting another one means a subclass here and an entry point in core.entry that names it."""

from .base import Assistant, Payload
from .claude_code import ClaudeCode
from .codex import Codex

__all__ = ["Assistant", "Payload", "ClaudeCode", "Codex"]
