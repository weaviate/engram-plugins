import sys

from ..assistants import claude_code
from . import main

if __name__ == "__main__":
    sys.exit(main(claude_code, sys.argv))
