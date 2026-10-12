import json
import sys
from typing import Any

from core.classes import Assistant, InputData, TranscriptEntry


class ClaudeCode(Assistant):
    NAME = "claude"
    MANIFEST_DIR = ".claude-plugin"
    STORE_FAILURE_EXIT = 2  # asyncRewake turns this into a wake showing our stderr

    def read_input(self) -> InputData:
        # hook payloads use snake case
        payload = json.load(sys.stdin)
        return InputData(
            stop_hook_active=payload.get("stop_hook_active") or False,
            last_assistant_message=payload.get("last_assistant_message") or "",
            cwd=payload.get("cwd") or "",
            session_id=payload.get("session_id") or "",
            prompt=payload.get("prompt") or "",
            transcript_path=payload.get("transcript_path") or "",
            turn_id=payload.get("prompt_id") or "",
        )

    def read_entry(self, raw: dict[str, Any]) -> TranscriptEntry:
        # transcript entries use camel case
        content = raw.get("message", raw).get("content")
        origin = raw.get("origin")
        return TranscriptEntry(
            role=raw.get("type", ""),
            text="" if _is_tool_only(content) else _text(content),
            turn_id=raw.get("promptId", ""),
            origin=origin.get("kind", "") if isinstance(origin, dict) else "",
        )

    def is_automated(self, payload: InputData) -> bool:
        # An unrecognised origin counts as human.
        origin = self._origin(payload)
        return origin is not None and origin != "human"

    def _origin(self, payload: InputData) -> str | None:
        prompt_id = payload.turn_id
        if not prompt_id:
            return None
        for entry in self.transcript(payload):
            # Tool results inherit the promptId of the prompt that spawned them and carry no
            # origin, so keep scanning past them for the entry that has one.
            if entry.turn_id == prompt_id and entry.origin:
                return entry.origin
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


if __name__ == "__main__":
    from core.hooks import dispatch

    sys.exit(dispatch(ClaudeCode(), sys.argv))
