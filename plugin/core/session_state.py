"""
Session state management. Creates plain text files in <data dir>/sessions/ to 
track memory ids per session. Files are named <session_id>.<kind>, where kind 
is "own" (created during the session) or "shown" (already retrieved that session).
IDs are appended a line at a time.
"""

import os
import re
import time
from collections.abc import Iterable

from .util import data_dir

# clean() deletes files untouched for this long; the SessionStart hook runs it.
MAX_AGE_SECONDS = 7 * 24 * 3600

# The id becomes a file name, so anything but a plain token is refused rather than trusted.
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _path(session_id: str, kind: str) -> str:
    if not _SESSION_ID.fullmatch(session_id or ""):
        raise ValueError(f"unusable session id {session_id!r}")
    return os.path.join(data_dir(), "sessions", f"{session_id}.{kind}")


def load(session_id: str, kind: str) -> set[str]:
    try:
        with open(_path(session_id, kind)) as f:
            return set(f.read().split())
    except Exception:
        return set()


def add(session_id: str, kind: str, ids: Iterable[str]) -> None:
    lines = "".join(f"{i}\n" for i in ids)
    if not lines:
        return
    try:
        path = _path(session_id, kind)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(lines)
    except Exception:
        pass


def clean() -> None:
    try:
        folder = os.path.join(data_dir(), "sessions")
        cutoff = time.time() - MAX_AGE_SECONDS
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass


def reset(session_id: str, kind: str) -> None:
    try:
        os.remove(_path(session_id, kind))
    except Exception:
        pass
