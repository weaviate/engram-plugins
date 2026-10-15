import sys

from assistants.claude_code import ClaudeCode
from core.hooks import dispatch

sys.exit(dispatch(ClaudeCode(), sys.argv))
