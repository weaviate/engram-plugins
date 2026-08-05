"""One-shot migration of memories from other local memory systems into Engram.

A source adapter turns a foreign store into a stream of Records speaking the tiny KINDS
vocabulary below; the engine (core.migrate.engine) maps kinds to the Engram group's topics,
batches per repo scope, and submits through the pre-extracted pipeline — the content was
already LLM-summarized by the source system, so no re-extraction. Adding a new source is one
adapter module plus a sources() entry; the engine never changes.

An adapter is a class with:
    name                     registry key (the CLI --source value)
    __init__(path=None)      path overrides the source's default store location
    available()              path/description of the detected install, or None
    records(include_all)     iterator of Record
    describe_selection(include_all)   optional: human lines for the dry-run report
"""

from __future__ import annotations

from dataclasses import dataclass

# What adapters classify into. Deliberately small and Engram-agnostic: the engine maps these
# to the group's actual topics (engine.KIND_TO_TOPIC, overridable via --topic-map), so
# adapters stay valid for users with custom topic sets.
KINDS = ("architecture", "process", "task", "preference")


@dataclass
class Record:
    uid: str  # stable per-source id — the checkpoint key, so re-runs skip migrated rows
    kind: str  # one of KINDS
    content: str  # composed memory text (the engine prepends the [date] prefix)
    created_at: str | None  # ISO timestamp from the source, or None
    project: str | None  # source's project hint; the engine resolves it to a repo_name


def sources():
    """Registry of source adapters, keyed by --source name."""
    from . import claude_mem

    return {claude_mem.ClaudeMemSource.name: claude_mem.ClaudeMemSource}
