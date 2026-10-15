import json
import sys
from typing import Any

from core.classes import Assistant, InputData, TranscriptEntry


class Codex(Assistant):
    name = "codex"
    manifest_dir = ".codex-plugin"
    data_dir_env = "PLUGIN_DATA"

    def read_input(self) -> InputData:
        payload = json.load(sys.stdin)
        return InputData(
            stop_hook_active=payload.get("stop_hook_active") or False,
            last_assistant_message=payload.get("last_assistant_message") or "",
            cwd=payload.get("cwd") or "",
            session_id=payload.get("session_id") or "",
            prompt=payload.get("prompt") or "",
            transcript_path=payload.get("transcript_path") or "",
            turn_id=payload.get("turn_id") or "",
        )

    def read_entry(self, raw: dict[str, Any]) -> TranscriptEntry | None:
        """Rollout entries wrap a message in `payload`, and use a `developer` role for injected
        context nobody typed."""
        message = raw.get("payload") or {}
        if message.get("type") != "message" or not message.get("role"):
            return None
        
        blocks = message.get("content") or []
        text="\n".join(
            b["text"] for b in blocks if isinstance(b, dict) and b.get("text")
        ).strip()
        if not text:
            return None

        return TranscriptEntry(
            role=message.get("role", ""),
            text=text,
        )


if __name__ == "__main__":
    from core.hooks import dispatch

    sys.exit(dispatch(Codex(), sys.argv))
