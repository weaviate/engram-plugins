#!/usr/bin/env python3
"""Stop hook (asyncRewake): store the completed turn — user message + assistant's answer — in
Engram as an OpenAI-format conversation.

Success is silent. On a store *failure* we exit 2: with asyncRewake the harness wakes Claude and
shows our stderr as a system reminder, so it can tell the user immediately — no waiting, no
deferred/stale message. A persistent failure wakes every turn (the search hook surfaces the same
root cause every turn anyway); fixing the cause silences it. The stop_hook_active guard below
keeps that wake from looping within a single rewake."""

import sys

from core.assistant import Assistant

from core import (  # noqa: I001 — core re-exports, kept after the typed import
    debug,
    get_client,
    get_user_id,
    read_input,
    resolve_scope,
)


def run(assistant: Assistant) -> int:
    data = read_input()
    if data.get("stop_hook_active"):
        return 0

    session_id = data.get("session_id", "")
    turn = assistant.turn_id(data)

    # Recall still runs for host-generated turns, but their user half is machine markup and the
    # work they describe is stored with the human turn that follows.
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
