#!/usr/bin/env python3
"""Stop hook: store the completed turn — user message + assistant's answer — in
Engram as an OpenAI-format conversation, then wait briefly for the run to commit and record the
memory ids it wrote, so search can leave this session's own memories out.

Success is silent. A store failure writes to stderr and exits with the assistant's own
STORE_FAILURE_EXIT, because the same code means opposite things: Claude Code wakes and reports
it, Codex would take it as instruction to keep working. Where it does wake, a persistent failure
wakes every turn and fixing the cause silences it; the stop_hook_active guard keeps one wake from
looping."""

import sys
import time

from core import (
    debug,
    get_client,
    get_user_id,
    read_input,
    resolve_scope,
    session_state,
)
from core.assistant import Assistant

# A run commits in ~12s. Search cannot recognise what a run still uncommitted after this wrote.
OWN_POLL_SECONDS = 20


def run(assistant: Assistant) -> int:
    data = read_input()
    if data.get("stop_hook_active"):
        return 0

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

    client = get_client(assistant.NAME)
    if client is None:
        debug("store unavailable", turn=turn, reason="no api key")
        return 0  # search surfaces a missing key/SDK immediately; nothing to wake about here

    user_id = get_user_id()
    if not user_id:
        debug("store unavailable", turn=turn, reason="no identity")
        return 0  # search surfaces missing identity immediately

    try:
        properties, _user_required, _unmapped = resolve_scope(
            data.get("cwd", ""), data.get("session_id", "")
        )
        added = client.memories.add(messages, user_id=user_id, properties=properties or None)
    except Exception as e:
        sys.stderr.write(
            f"Engram · saving memory failed — {e}. Storing is broken until fixed "
            "(check ENGRAM_API_KEY and .engram.json scope).\n"
        )
        return assistant.STORE_FAILURE_EXIT

    debug("store", turn=turn, run_id=added.run_id)

    # Hook runs in background (asyncRewake: True), waiting and polling for the run
    # here does not block future interactions
    try:
        status = _wait_for_run_including_buffer(client, added.run_id)
    except Exception as e:
        debug("store poll failed", turn=turn, run_id=added.run_id, error=e)
        return 0
    ids = [op.memory_id for op in (*status.memories_created, *status.memories_updated)]

    session_state.add(data.get("session_id", ""), "own", ids)
    debug("store settled", turn=turn, status=status.status, own=len(ids))
    return 0


def _wait_for_run_including_buffer(client, run_id):
    """Essentially runs.wait but including `in_buffer` state as settled."""
    deadline = time.monotonic() + OWN_POLL_SECONDS
    while True:
        status = client.runs.get(run_id)
        if (
            status.status in ("completed", "failed", "in_buffer")
            or time.monotonic() >= deadline
        ):
            return status
        time.sleep(1)

