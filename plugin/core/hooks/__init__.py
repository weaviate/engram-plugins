"""What each hook does, written against the core.classes contract rather than any one
assistant. Each assistant module runs these by name."""

import sys
from collections.abc import Callable, Sequence

from core.classes import Assistant

from . import search, store

HOOKS: dict[str, Callable[[Assistant], int]] = {"search": search.run, "store": store.run}


def dispatch(assistant: Assistant, argv: Sequence[str]) -> int:
    hook = HOOKS.get(argv[1]) if len(argv) > 1 else None
    if hook is None:
        sys.stderr.write(f"Engram · expected one of {sorted(HOOKS)}, got {list(argv[1:])}\n")
        return 0
    return hook(assistant)
