from ..transcript import entries_newest_first, last_user_text
from .base import Assistant, Payload


class ClaudeCode(Assistant):
    NAME = "claude-code"
    MANIFEST_DIR = ".claude-plugin"
    STORE_FAILURE_EXIT = 2  # asyncRewake turns this into a wake showing our stderr

    def turn_id(self, payload: Payload) -> str:
        return payload.get("prompt_id") or ""

    def last_user_text(self, payload: Payload) -> str:
        return last_user_text(payload.get("transcript_path"))

    def is_automated(self, payload: Payload) -> bool:
        # An unrecognised origin counts as human.
        origin = self._origin(payload.get("transcript_path"), self.turn_id(payload))
        return origin is not None and origin != "human"

    def _origin(self, transcript_path: str | None, prompt_id: str) -> str | None:
        if not prompt_id:
            return None
        for entry in entries_newest_first(transcript_path):
            # Tool results inherit the promptId of the prompt that spawned them and carry no
            # origin, so keep scanning past them for the entry that has one.
            if entry.get("promptId") == prompt_id:
                origin = entry.get("origin")
                kind = origin.get("kind") if isinstance(origin, dict) else None
                if kind:
                    return str(kind)
        return None
