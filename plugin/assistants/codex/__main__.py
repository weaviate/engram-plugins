import sys

from assistants.codex import Codex
from core.hooks import dispatch

sys.exit(dispatch(Codex(), sys.argv))
