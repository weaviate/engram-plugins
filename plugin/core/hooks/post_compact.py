"""PostCompact hook: compaction replaces the conversation with a summary, which loses both the
memories injected so far and the turns this session's own memories came from. Forget both, so
search can bring them back."""

from core import debug, session_state
from core.classes import Assistant


def run(assistant: Assistant) -> int:
    data = assistant.read_input()
    session_state.reset(data.session_id, "shown", assistant.data_dir())
    session_state.reset(data.session_id, "own", assistant.data_dir())
    debug("session state reset", turn=data.turn_id)
    return 0
