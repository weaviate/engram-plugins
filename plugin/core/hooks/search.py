#!/usr/bin/env python3
"""UserPromptSubmit hook: search Engram for memories relevant to the prompt and inject them as
additionalContext for the assistant to use. Recalled memories are injected silently as context;
warnings/errors ride in the model's reply (additionalContext directive), not a hook
`systemMessage`, so they render in every host.

Skips memories already in context. Success is silent. Warnings/errors are scoped to the current
reply and persist by being re-injected each turn, so a fixed problem stops showing on its own."""

import json

from engram import HybridRetrieval, Memory

from core import (
    debug,
    engram_warning,
    get_client,
    get_user_id,
    read_input,
    search_filters,
    session_state,
)
from core.assistant import Assistant

# Fetch well past what is injected: memories this session wrote or has already been shown are
# dropped after the search, and what remains should still fill the injection.
SEARCH_LIMIT = 30
INJECT_LIMIT = 5


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
            "ignore if irrelevant. If a memory informs your answer, say so in your first "
            "sentence and credit Engram:\n" + memories
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


def run(assistant: Assistant) -> int:
    data = read_input()
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return 0
    session_id = data.get("session_id", "")
    turn = assistant.turn_id(data)

    warning = engram_warning()
    if warning:
        debug("search unavailable", turn=turn, reason=warning)
        emit(status=[_tag(warning)])
        return 0

    client = get_client(assistant.NAME)
    if client is None:
        emit(status=[_tag("client unavailable.")])
        return 0

    user_id = get_user_id()
    # Resolving filters reads the scope schema from Engram; that and the search run under one
    # try, so a failure anywhere fails the whole operation with a single error.
    try:
        topics, properties, warnings = search_filters(data.get("cwd", ""), session_id)
        kwargs = {"properties": properties} if properties else {}
        results = client.memories.search(
            query=prompt,
            user_id=user_id,
            topics=topics,
            retrieval_config=HybridRetrieval(limit=SEARCH_LIMIT),
            **kwargs,
        )
    except Exception as e:
        debug("search failed", turn=turn, error=e)
        emit(status=[_tag(f"search failed: {e}")])
        return 0

    own = session_state.load(session_id, "own")
    shown = session_state.load(session_id, "shown") | session_state.load(session_id, "preloaded")
    memories, injected_ids, skipped_own, skipped_shown = [], [], 0, 0
    for m in results:
        if len(memories) == INJECT_LIMIT:
            break
        if _written_this_session(m, session_id, own):
            skipped_own += 1
            continue
        if _already_shown(m, shown):
            skipped_shown += 1
            continue
        if m.content:
            memories.append(m.content.strip())
            injected_ids.append(m.id)

    bullets = "\n".join(f"- {m}" for m in memories)

    debug(
        "search",
        turn=turn,
        injected=len(memories),
        own=skipped_own,
        shown=skipped_shown,
        chars=len(bullets),
    )

    # Success is silent: warnings (if any) show this reply; memories are injected as context.
    emit(status=[_tag(w) for w in warnings] or None, memories=bullets or None)
    session_state.add(session_id, "shown", injected_ids)
    return 0


def _tag(message):
    return f"Engram · {message}"


def _written_this_session(memory: Memory, session_id: str, own: set[str]) -> bool:
    """The ids the store hook recorded cover every topic. The session_id property covers
    session-scoped topics when the store hook missed a run."""
    if memory.id in own:
        return True
    properties = memory.properties or {}
    return bool(session_id) and properties.get("session_id") == session_id


def _already_shown(memory: Memory, shown: set[str]) -> bool:
    """Injected context stays in the conversation, so the assistant still has it."""
    return memory.id in shown
