"""The contract core.hooks is written against. Implementations live in assistants/."""

import json
import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any


@dataclass
class InputData:
    """The hook payload under one set of names, whichever assistant sent it."""

    stop_hook_active: bool = False
    last_assistant_message: str = ""
    cwd: str = ""
    session_id: str = ""
    prompt: str = ""
    transcript_path: str = ""
    turn_id: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InputData":
        """Each hook event sends a different subset, so unknown keys are dropped and missing or
        null ones take the default."""
        return cls(
            **{f.name: payload[f.name] for f in fields(cls) if payload.get(f.name) is not None}
        )


@dataclass
class TranscriptEntry:
    """A transcript line under one set of names, whichever assistant wrote it."""

    role: str
    text: str
    turn_id: str = ""
    origin: str = ""


@lru_cache(maxsize=2)
def _lines(transcript_path: str, mtime: int, size: int) -> tuple[str, ...]:
    """Keyed on the file's identity so a rewritten transcript is re-read. A turn walks the
    transcript more than once and these files reach tens of megabytes. Module-level because an
    lru_cache on a method would keep the instance alive."""
    try:
        with open(transcript_path, "r", errors="replace") as f:
            return tuple(f)
    except OSError:
        return ()


class Assistant(ABC):
    NAME: str
    MANIFEST_DIR: str

    # Exit code for a failed store. Claude Code turns 2 into a wake carrying our stderr, but Codex
    # reads it as "continue the turn, using stderr as the prompt", so an assistant has to opt in.
    STORE_FAILURE_EXIT: int = 0

    def read_input(self) -> InputData:
        """The hook payload on stdin, for an assistant whose names already match InputData's.
        Malformed JSON raises — a hook should never run on garbage input."""
        return InputData.from_payload(json.load(sys.stdin))

    @abstractmethod
    def read_entry(self, raw: dict[str, Any]) -> TranscriptEntry | None:
        """One line of this assistant's transcript, or None for a line that holds no message."""

    def last_user_text(self, payload: InputData) -> str:
        """The most recent message a person sent, or "" when none can be read."""
        for entry in self.transcript(payload):
            if entry.role == "user" and entry.text:
                return entry.text
        return ""

    def is_automated(self, payload: InputData) -> bool:
        """Whether the assistant started this turn rather than a person. An assistant that
        records no provenance keeps this default: losing the skip beats dropping a real turn."""
        return False

    def transcript(self, payload: InputData) -> Iterator[TranscriptEntry]:
        """Transcript entries, newest first. Both assistants write JSONL; one that doesn't can
        override this. An unreadable file or line yields nothing rather than raising, because a
        hook must not break a session over a transcript it cannot parse."""
        if not payload.transcript_path:
            return
        try:
            stat = os.stat(payload.transcript_path)
        except OSError:
            return
        for line in reversed(_lines(payload.transcript_path, stat.st_mtime_ns, stat.st_size)):
            try:
                entry = self.read_entry(json.loads(line))
            except Exception:
                continue
            if entry is not None:
                yield entry
