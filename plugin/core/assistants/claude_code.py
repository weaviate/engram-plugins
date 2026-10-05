from typing import Any

from .base import Assistant, Payload


class ClaudeCode(Assistant):
    NAME = "claude-code"
    MANIFEST_DIR = ".claude-plugin"
    STORE_FAILURE_EXIT = 2  # asyncRewake turns this into a wake showing our stderr

    def turn_id(self, payload: Payload) -> str:
        return payload.get("prompt_id") or ""

    def last_user_text(self, payload: Payload) -> str:
        for entry in self.transcript(payload):
            if entry.get("type") != "user":
                continue
            content = entry.get("message", entry).get("content")
            if _is_tool_only(content):
                continue
            text = _text(content)
            if text:
                return text
        return ""

    def is_automated(self, payload: Payload) -> bool:
        # An unrecognised origin counts as human.
        origin = self._origin(payload)
        return origin is not None and origin != "human"

    def _origin(self, payload: Payload) -> str | None:
        prompt_id = self.turn_id(payload)
        if not prompt_id:
            return None
        for entry in self.transcript(payload):
            # Tool results inherit the promptId of the prompt that spawned them and carry no
            # origin, so keep scanning past them for the entry that has one.
            if entry.get("promptId") == prompt_id:
                origin = entry.get("origin")
                kind = origin.get("kind") if isinstance(origin, dict) else None
                if kind:
                    return str(kind)
        return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _is_tool_only(content: Any) -> bool:
    """True for entries carrying only tool_use / tool_result blocks."""
    if isinstance(content, list):
        types = {b.get("type") for b in content if isinstance(b, dict)}
        return bool(types) and types.issubset({"tool_result", "tool_use"})
    return False
