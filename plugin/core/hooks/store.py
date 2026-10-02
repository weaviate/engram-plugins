#!/usr/bin/env python3
"""Stop hook (asyncRewake): store the completed turn — user message + assistant's answer — in
Engram as an OpenAI-format conversation.

Success is silent. On a store *failure* we exit 2: with asyncRewake the harness wakes Claude and
shows our stderr as a system reminder, so it can tell the user immediately — no waiting, no
deferred/stale message. A persistent failure wakes every turn (the search hook surfaces the same
root cause every turn anyway); fixing the cause silences it. The stop_hook_active guard below
keeps that wake from looping within a single rewake."""

import sys

from core import (
    STORE_TIMEOUT,
    debug,
    get_client,
    get_user_id,
    is_automated,
    last_user_text,
    prompt_origin,
    read_input,
    resolve_scope,
)


def main():
    data = read_input()
    if data.get("stop_hook_active"):
        return 0

    session_id = data.get("session_id", "")
    prompt_id = data.get("prompt_id", "")

    # Recall still runs for host-generated turns, but their user half is machine markup and the
    # work they describe is stored with the human turn that follows.
    origin = prompt_origin(data.get("transcript_path"), prompt_id)
    if is_automated(origin):
        debug("store skipped", prompt_id=prompt_id, origin=origin)
        return 0

    assistant = (data.get("last_assistant_message") or "").strip()
    user = last_user_text(data.get("transcript_path"))

    messages = []
    if user:
        messages.append({"role": "user", "content": user})
    if assistant:
        messages.append({"role": "assistant", "content": assistant})
    if not messages:
        return 0

    client = get_client(STORE_TIMEOUT)
    if client is None:
        return 0  # search surfaces a missing key/SDK immediately; nothing to wake about here

    user_id = get_user_id()
    if not user_id:
        return 0  # search surfaces missing identity immediately

    try:
        properties, _user_required, _unmapped = resolve_scope(data.get("cwd", ""), session_id)
        run = client.memories.add(messages, user_id=user_id, properties=properties or None)
    except Exception as e:
        # exit 2 → asyncRewake wakes Claude and shows this stderr as a system reminder
        sys.stderr.write(
            f"Engram · saving memory failed — {e}. Storing is broken until fixed "
            "(check ENGRAM_API_KEY and .engram.json scope).\n"
        )
        return 2

    debug("store", prompt_id=prompt_id, run_id=getattr(run, "run_id", None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
