"""One-shot migration of memories from other local memory systems into Engram.

A source adapter turns a foreign store into a stream of Records; the engine
(core.migrate.engine) groups them into chronological conversations and submits them
through Engram's extraction pipeline with the records' original dates as context. Engram
classifies each memory into the group's topics itself — the migration never picks a topic,
so it works with any topic setup. Adding a new source is one adapter module plus a
sources() entry; the engine never changes.

An adapter is a class with:
    name                     registry key (the CLI --source value)
    __init__(path=None)      path overrides the source's default store location
    db_path                  resolved store location (used in CLI error messages)
    available()              path/description of the detected install, or None
    records()                iterator of Record — every record; Engram's extraction
                             decides what each memory becomes and what to keep
    describe_selection()     optional: human lines for the dry-run report
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Record:
    uid: str  # stable per-source id — the checkpoint key, so re-runs skip migrated rows
    content: str  # composed memory text; Engram extracts and classifies it
    created_at: str | None  # ISO timestamp from the source, or None
    project: str | None  # source's project hint; the engine resolves it to a repo_name


def sources():
    """Registry of source adapters, keyed by --source name."""
    from . import claude_mem, claude_memory

    return {
        claude_mem.ClaudeMemSource.name: claude_mem.ClaudeMemSource,
        claude_memory.ClaudeMemorySource.name: claude_memory.ClaudeMemorySource,
    }
