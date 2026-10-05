"""Transcript parsing: pull the most recent real user message from the session transcript.
The host writes a JSONL transcript of {type, message:{content}} entries."""

import json
import os
from collections.abc import Iterator
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=2)
def _lines(transcript_path: str, mtime: int, size: int) -> tuple[str, ...]:
    """Cached on the file's identity so a rewritten transcript is re-read. A hook walks the
    transcript more than once per turn, and these files reach tens of megabytes."""
    try:
        with open(transcript_path, "r") as f:
            return tuple(f)
    except OSError:
        return ()


def entries_newest_first(transcript_path: str | None) -> Iterator[dict[str, Any]]:
    """Parsed JSONL entries, newest first. An unreadable file or line yields nothing rather than
    raising: a hook must not break a session over a transcript it cannot parse."""
    if not transcript_path:
        return
    try:
        stat = os.stat(transcript_path)
    except OSError:
        return
    for line in reversed(_lines(transcript_path, stat.st_mtime_ns, stat.st_size)):
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
    """The most recent real user message, skipping tool-result turns. Handles both
    {message:{content}} and {content} shapes."""
    for entry in entries_newest_first(transcript_path):
        if entry.get("type") != "user":
            continue
        content = entry.get("message", entry).get("content")
        if _is_tool_only(content):
            continue
        text = _content_to_text(content)
        if text:
            return text
    return ""
