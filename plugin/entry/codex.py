import sys

from assistants import Codex
from . import main

if __name__ == "__main__":
    sys.exit(main(Codex(), sys.argv))
