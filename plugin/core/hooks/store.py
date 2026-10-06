"""Stop hook: store the completed turn — user message and the assistant's answer — in Engram as
an OpenAI-format conversation.

Success is silent. A store failure writes to stderr and exits with the assistant's own
STORE_FAILURE_EXIT, because the same code means opposite things: Claude Code wakes and reports
it, Codex would take it as instruction to keep working. Where it does wake, a persistent failure
wakes every turn and fixing the cause silences it; the stop_hook_active guard keeps one wake from
looping."""

import sys

from core import (
    debug,
    get_client,
    get_user_id,
    read_input,
    resolve_scope,
)
from core.assistant import Assistant


def run(assistant: Assistant) -> int:
    data = read_input()
    if data.get("stop_hook_active"):
        return 0

    session_id = data.get("session_id", "")
    turn = assistant.turn_id(data)

    # Recall still runs for these turns, but the user half is machine markup and the work they
    # describe is stored with the human turn that follows.
    if assistant.is_automated(data):
        debug("store skipped", turn=turn, assistant=assistant.NAME)
        return 0

    answer = (data.get("last_assistant_message") or "").strip()
    user = assistant.last_user_text(data)

    messages = []
    if user:
        messages.append({"role": "user", "content": user})
    if answer:
        messages.append({"role": "assistant", "content": answer})
    if not messages:
        return 0

    client = get_client(assistant)
    if client is None:
        debug("store unavailable", turn=turn, reason="no api key")
        return 0  # search surfaces a missing key/SDK immediately; nothing to wake about here

    user_id = get_user_id()
    if not user_id:
        debug("store unavailable", turn=turn, reason="no identity")
        return 0  # search surfaces missing identity immediately

    try:
        properties, _user_required, _unmapped = resolve_scope(data.get("cwd", ""), session_id)
        added = client.memories.add(messages, user_id=user_id, properties=properties or None)
    except Exception as e:
        # Claude Code turns this exit code into a wake showing the stderr below; Codex would
        # read the same code as "keep working", so each assistant names its own.
        sys.stderr.write(
            f"Engram · saving memory failed — {e}. Storing is broken until fixed "
            "(check ENGRAM_API_KEY and .engram.json scope).\n"
        )
        return assistant.STORE_FAILURE_EXIT

    debug("store", turn=turn, run_id=getattr(added, "run_id", None))
    return 0
