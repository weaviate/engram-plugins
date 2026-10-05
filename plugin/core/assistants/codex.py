from ..transcript import entries_newest_first

NAME = "codex"
MANIFEST_DIR = ".codex-plugin"

# Codex reads exit 2 on Stop as "continue the turn, using stderr as the prompt", so a store
# failure must not use it. The search hook reports the same failure through its own output.
STORE_FAILURE_EXIT = 0


def turn_id(payload):
    return payload.get("turn_id") or ""


def last_user_text(payload):
    """Codex rollout entries wrap a message in `payload`, and carry a `developer` role for
    injected context that no person typed."""
    for entry in entries_newest_first(payload.get("transcript_path")):
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


def is_automated(payload):
    """Codex documents no field recording who submitted a turn, so every turn is treated as a
    person's — the same way an unrecognised Claude Code origin is."""
    return False
