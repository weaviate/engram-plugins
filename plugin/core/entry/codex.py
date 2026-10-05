import sys

from ..assistants import codex
from . import main

if __name__ == "__main__":
    sys.exit(main(codex, sys.argv))
