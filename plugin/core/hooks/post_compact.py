"""PostCompact hook: compaction drops the memories injected so far from the assistant's context, so
forget they were shown and let search inject them again. Memories this session wrote stay
excluded."""

from core import debug, read_input, session_state
from core.assistant import Assistant


def run(assistant: Assistant) -> int:
    data = read_input()
    session_state.reset(data.get("session_id", ""), "shown")
    debug("shown reset", turn=assistant.turn_id(data), trigger=data.get("trigger"))
    return 0
