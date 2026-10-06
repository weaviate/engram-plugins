"""Vendor adapters. One module per assistant, each a subclass of core.assistant.Assistant.

A module that outgrows one file becomes a package here, the way sqlalchemy.dialects does."""

from .claude_code import ClaudeCode
from .codex import Codex

__all__ = ["ClaudeCode", "Codex"]
