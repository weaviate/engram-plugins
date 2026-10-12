"""SessionStart hook: delete the state of sessions untouched for a week."""

from core import session_state
from core.classes import Assistant


def run(assistant: Assistant) -> int:
    session_state.clean(assistant.data_dir())
    return 0
