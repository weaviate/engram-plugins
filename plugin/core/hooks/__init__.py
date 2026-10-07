"""What each hook does, written against the core.assistant contract rather than any one
assistant. Each assistant module runs these by name."""

import sys
from collections.abc import Callable, Sequence

from core.assistant import Assistant

from . import post_compact, search, session_start, store

HOOKS: dict[str, Callable[[Assistant], int]] = {
    "session_start": session_start.run,
    "search": search.run,
    "store": store.run,
    "post_compact": post_compact.run,
}


def dispatch(assistant: Assistant, argv: Sequence[str]) -> int:
    hook = HOOKS.get(argv[1]) if len(argv) > 1 else None
    if hook is None:
        sys.stderr.write(f"Engram · expected one of {sorted(HOOKS)}, got {list(argv[1:])}\n")
        return 0
    return hook(assistant)
