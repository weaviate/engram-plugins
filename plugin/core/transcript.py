"""Transcript parsing: pull the most recent real user message from the session transcript.
The host writes a JSONL transcript of {type, message:{content}} entries."""

import json
import os
from collections.abc import Iterator
from typing import Any


def entries_newest_first(transcript_path: str | None) -> Iterator[dict[str, Any]]:
    """Parsed JSONL entries, newest first. Unreadable file or line yields nothing rather than
    raising: a hook must not break a session over a transcript it cannot parse."""
    if not transcript_path:
        return
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in reversed(lines):
        try:
            yield json.loads(line)
        except Exception:
            continue


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


def last_user_text(transcript_path: str | None) -> str:
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
