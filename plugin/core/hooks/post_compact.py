"""PostCompact hook: compaction replaces the conversation with a summary, which loses both the
memories injected so far and the turns this session's own memories came from. Forget both, so
search can bring them back."""

from core import debug, read_input, session_state
from core.assistant import Assistant


def run(assistant: Assistant) -> int:
    data = read_input()
    session_id = data.get("session_id", "")
    session_state.reset(session_id, "shown")
    session_state.reset(session_id, "own")
    debug("session state reset", turn=assistant.turn_id(data), trigger=data.get("trigger"))
    return 0
