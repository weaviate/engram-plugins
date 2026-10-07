from typing import Any

from core.classes import Assistant, TranscriptEntry


class Codex(Assistant):
    NAME = "codex"
    MANIFEST_DIR = ".codex-plugin"

    def read_entry(self, raw: dict[str, Any]) -> TranscriptEntry | None:
        """Rollout entries wrap a message in `payload`, and use a `developer` role for injected
        context nobody typed."""
        message = raw.get("payload") or {}
        if message.get("type") != "message":
            return None
        blocks = message.get("content") or []
        return TranscriptEntry(
            role=message.get("role", ""),
            text="\n".join(
                b["text"] for b in blocks if isinstance(b, dict) and b.get("text")
            ).strip(),
        )


if __name__ == "__main__":
    import sys

    from core.hooks import dispatch

    sys.exit(dispatch(Codex(), sys.argv))
