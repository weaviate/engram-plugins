"""One module per assistant, each naming itself. The hooks themselves take an assistant and do
not look one up, so there is no selection to get wrong."""

import sys
from collections.abc import Callable, Sequence

from core.assistant import Assistant
from core.hooks import search, store

HOOKS: dict[str, Callable[[Assistant], int]] = {"search": search.run, "store": store.run}


def main(assistant: Assistant, argv: Sequence[str]) -> int:
    hook = HOOKS.get(argv[1]) if len(argv) > 1 else None
    if hook is None:
        sys.stderr.write(f"Engram · expected one of {sorted(HOOKS)}, got {list(argv[1:])}\n")
        return 0
    return hook(assistant)
