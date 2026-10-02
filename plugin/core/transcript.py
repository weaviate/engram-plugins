"""Reading the host's session transcript. Everything host-specific about a turn lives here, so
supporting another assistant means replacing this module rather than touching the hooks."""

import json
import os


def _content_to_text(content):
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


def _is_tool_only(content):
    """True for transcript entries that only carry tool_use / tool_result blocks."""
    if isinstance(content, list):
        types = {b.get("type") for b in content if isinstance(b, dict)}
        return bool(types) and types.issubset({"tool_result", "tool_use"})
    return False


def last_user_text(transcript_path):
    """Walk the transcript JSONL backwards for the most recent real user message,
    skipping tool-result turns. Handles both {message:{content}} and {content} shapes."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    with open(transcript_path, "r") as f:
        lines = f.readlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue  # skip a partial/non-JSON line and keep scanning
        if entry.get("type") != "user":
            continue
        msg = entry.get("message", entry)
        content = msg.get("content")
        if _is_tool_only(content):
            continue
        text = _content_to_text(content)
        if text:
            return text
    return ""


def _origin(entry):
    origin = entry.get("origin")
    return origin.get("kind") if isinstance(origin, dict) else None


def prompt_origin(transcript_path, prompt_id):
    if not transcript_path or not prompt_id:
        return None
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        # Tool results inherit the promptId of the prompt that spawned them and carry no origin,
        # so keep scanning past them for the entry that has one.
        if entry.get("promptId") == prompt_id:
            origin = _origin(entry)
            if origin:
                return origin
    return None


def is_automated(origin):
    # An unrecognised origin counts as human: losing the skip beats dropping a real turn.
    return origin is not None and origin != "human"
