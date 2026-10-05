"""One module per assistant, each naming itself. The hooks themselves take an assistant and do
not look one up, so there is no selection to get wrong."""

import sys

from ..hooks import search, store

HOOKS = {"search": search.run, "store": store.run}


def main(assistant, argv):
    hook = HOOKS.get(argv[1]) if len(argv) > 1 else None
    if hook is None:
        sys.stderr.write(f"Engram · expected one of {sorted(HOOKS)}, got {argv[1:]}\n")
        return 0
    return hook(assistant)
