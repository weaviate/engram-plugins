from core.classes import Assistant, InputData


class Codex(Assistant):
    NAME = "codex"
    MANIFEST_DIR = ".codex-plugin"

    def last_user_text(self, payload: InputData) -> str:
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


if __name__ == "__main__":
    import sys

    from core.hooks import dispatch

    sys.exit(dispatch(Codex(), sys.argv))
