"""Per-assistant differences live here; everything else in core is assistant-agnostic.

An assistant module provides NAME, turn_id(payload), is_automated(payload) and
last_user_text(payload). Supporting another one means adding a module here and an entry point
in core.entry that names it."""

from . import claude_code, codex

__all__ = ["claude_code", "codex"]
