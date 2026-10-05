"""What every assistant has to answer for the hooks to work."""

from abc import ABC, abstractmethod
from typing import Any

Payload = dict[str, Any]


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
