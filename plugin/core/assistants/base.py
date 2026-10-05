"""What every assistant has to answer for the hooks to work."""

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

Payload = dict[str, Any]


@lru_cache(maxsize=2)
def _lines(transcript_path: str, mtime: int, size: int) -> tuple[str, ...]:
    """Cached on the file's identity so a rewritten transcript is re-read. A turn walks the
    transcript more than once, and these files reach tens of megabytes.

    Module-level rather than a method: an lru_cache on a method keeps the instance alive."""
    try:
        with open(transcript_path, "r") as f:
            return tuple(f)
    except OSError:
        return ()


class Assistant(ABC):
    NAME: str
    MANIFEST_DIR: str

    # Exit code for a failed store. Claude Code turns 2 into a wake carrying our stderr, but Codex
    # reads it as "continue the turn, using stderr as the prompt", so an assistant has to opt in.
    STORE_FAILURE_EXIT: int = 0

    @abstractmethod
    def turn_id(self, payload: Payload) -> str:
        """The assistant's own identifier for this turn, used to correlate the debug lines."""

    @abstractmethod
    def last_user_text(self, payload: Payload) -> str:
        """The most recent message a person sent, or "" when none can be read."""

    def is_automated(self, payload: Payload) -> bool:
        """Whether the assistant started this turn rather than a person. An assistant that
        records no provenance keeps this default: losing the skip beats dropping a real turn."""
        return False

    def transcript_path(self, payload: Payload) -> str | None:
        """Where this assistant says its transcript is. Claude Code and Codex both document
        `transcript_path` among the fields every hook receives — unlike the turn id, which each
        spells differently — so this is the agreed name until one disagrees."""
        return payload.get("transcript_path")

    def transcript(self, payload: Payload) -> Iterator[Payload]:
        """Transcript entries, newest first. Both assistants write JSONL; one that doesn't can
        override this. An unreadable file or line yields nothing rather than raising, because a
        hook must not break a session over a transcript it cannot parse."""
        path = self.transcript_path(payload)
        if not path:
            return
        try:
            stat = os.stat(path)
        except OSError:
            return
        for line in reversed(_lines(path, stat.st_mtime_ns, stat.st_size)):
            try:
                yield json.loads(line)
            except Exception:
                continue
