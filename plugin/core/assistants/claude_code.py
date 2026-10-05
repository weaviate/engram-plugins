from ..transcript import entries_newest_first
from ..transcript import last_user_text as _last_user_text

NAME = "claude-code"
MANIFEST_DIR = ".claude-plugin"

# asyncRewake turns exit 2 into a wake carrying our stderr, so a store failure reaches the user.
STORE_FAILURE_EXIT = 2


def turn_id(payload):
    return payload.get("prompt_id") or ""


def last_user_text(payload):
    return _last_user_text(payload.get("transcript_path"))


def is_automated(payload):
    # An unrecognised origin counts as human: losing the skip beats dropping a real turn.
    origin = _prompt_origin(payload.get("transcript_path"), turn_id(payload))
    return origin is not None and origin != "human"


def _prompt_origin(transcript_path, prompt_id):
    if not prompt_id:
        return None
    for entry in entries_newest_first(transcript_path):
        # Tool results inherit the promptId of the prompt that spawned them and carry no origin,
        # so keep scanning past them for the entry that has one.
        if entry.get("promptId") == prompt_id:
            origin = entry.get("origin")
            kind = origin.get("kind") if isinstance(origin, dict) else None
            if kind:
                return kind
    return None
