from .base import Assistant, Payload


class Codex(Assistant):
    NAME = "codex"
    MANIFEST_DIR = ".codex-plugin"

    def turn_id(self, payload: Payload) -> str:
        return payload.get("turn_id") or ""

    def last_user_text(self, payload: Payload) -> str:
        """Rollout entries wrap a message in `payload`, and use a `developer` role for injected
        context nobody typed."""
        for entry in self.transcript(payload):
            message = entry.get("payload") or {}
            if message.get("type") != "message" or message.get("role") != "user":
                continue
            blocks = message.get("content") or []
            text = "\n".join(
                b["text"] for b in blocks if isinstance(b, dict) and b.get("text")
            ).strip()
            if text:
                return text
        return ""
