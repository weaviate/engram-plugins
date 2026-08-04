#!/usr/bin/env python3
"""UserPromptSubmit hook: search Engram for memories relevant to the prompt and inject them as
additionalContext for the assistant to use. Recalled memories are injected silently as context;
warnings/errors ride in the model's reply (additionalContext directive), not a hook
`systemMessage`, so they render in every host.

Stateless — no per-session files. Success is silent. Warnings/errors are scoped to the current
reply and persist by being re-injected each turn, so a fixed problem stops showing on its own."""

import json
import sys

from core import (
    engram_warning,
    get_client,
    get_user_id,
    read_input,
    search_filters,
)


def tag(message):
    return f"Engram · {message}"


def emit(status=None, memories=None):
    """UserPromptSubmit output. Everything rides in `additionalContext` so it renders in every
    host via the model's reply (not a hook `systemMessage`, which GUI hosts drop):

      - `status` (warnings/errors): shown for THIS reply only; persistence comes from re-injection
        each turn, so a fixed problem stops showing on its own.
      - `memories`: offered as usable context, not echoed."""
    parts = []
    if status:
        seen, unique = set(), []
        for line in status:
            if line not in seen:
                seen.add(line)
                unique.append(line)
        parts.append(
            "Engram (memory plugin) status for the user. For THIS reply only, begin your reply "
            "with the following line(s) verbatim, then answer normally — do not repeat them in "
            "later replies unless they appear here again:\n" + "\n".join(unique)
        )
    if memories:
        parts.append(
            "Relevant long-term memories about this user (from Engram). Use them if helpful; "
            "ignore if irrelevant. Briefly mention when Engram memory informs your answer:\n"
            + memories
        )
    if parts:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": "\n\n".join(parts),
                    }
                }
            )
        )


def main():
    data = read_input()
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return 0
    session_id = data.get("session_id", "")

    warning = engram_warning()
    if warning:
        emit(status=[tag(warning)])
        return 0

    client = get_client()
    if client is None:
        emit(status=[tag("client unavailable.")])
        return 0

    user_id = get_user_id()
    # Resolving filters reads the scope schema from Engram; that and the search run under one
    # try, so a failure anywhere fails the whole operation with a single error.
    try:
        topics, properties, warnings = search_filters(data.get("cwd", ""), session_id)
        kwargs = {"properties": properties} if properties else {}
        results = client.memories.search(
            query=prompt, user_id=user_id, topics=topics, **kwargs
        )
    except Exception as e:
        emit(status=[tag(f"search failed: {e}")])
        return 0

    memories = []
    for m in results:
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        if content:
            memories.append(str(content).strip())

    bullets = "\n".join(f"- {m}" for m in memories)

    # Success is silent: warnings (if any) show this reply; memories are injected as context.
    emit(status=[tag(w) for w in warnings] or None, memories=bullets or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
