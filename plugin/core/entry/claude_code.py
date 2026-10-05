import sys

from ..assistants import ClaudeCode
from . import main

if __name__ == "__main__":
    sys.exit(main(ClaudeCode(), sys.argv))
