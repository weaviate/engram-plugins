"""SessionStart hook: delete stale session state, then inject the session start topic."""

import json

from engram import FetchRetrieval, Memory, Topic

from core import (
    debug,
    engram_warning,
    get_client,
    get_user_id,
    read_input,
    search_filters,
    session_start_topic,
    session_state,
)
from core.assistant import Assistant

FETCH_LIMIT = 100  # the most the server returns
MAX_CHARS = 8000  # Claude Code moves hook output over 10,000 characters to a file
TRUNCATED = "… (truncated)"


def emit(context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )


def emit_status(message: str) -> None:
    emit(
        "Engram (memory plugin) status for the user. Begin your next reply with the following "
        f"line verbatim, then answer normally:\nEngram · {message}"
    )


def run(assistant: Assistant) -> int:
    session_state.clean()

    data = read_input()
    session_id = data.get("session_id", "")
    session_state.reset(session_id, "preloaded")
    if engram_warning():
        return 0
    client = get_client(assistant.NAME)
    if client is None:
        return 0

    cwd = data.get("cwd", "")
    try:
        topic, error = session_start_topic(cwd)
        if error:
            emit_status(error)
            return 0
        if topic is None:
            return 0
        search_topics, properties, _warnings = search_filters(cwd, session_id)
        results = client.memories.search(
            query=topic,
            user_id=get_user_id(),
            topics=[_with_search_filter(topic, search_topics)],
            retrieval_config=FetchRetrieval(limit=FETCH_LIMIT),
            properties=properties,
        )
        newest_first = sorted(
            (m for m in results if m.content), key=lambda m: m.created_at, reverse=True
        )
    except Exception as e:
        debug("session start failed", source=data.get("source"), error=e)
        emit_status(f"loading memories at session start failed: {e}")
        return 0

    injected, text = _fit(newest_first)

    debug(
        "session start",
        source=data.get("source"),
        topic=topic,
        fetched=len(newest_first),
        injected=len(injected),
        chars=len(text),
    )

    if text:
        emit(
            f"Standing memory from Engram (topic {topic}), loaded once for the whole session. "
            "Apply it throughout unless the user says otherwise:\n" + text
        )
    session_state.add(session_id, "preloaded", [m.id for m in injected])
    return 0


def _with_search_filter(topic: str, search_topics: list[str | Topic] | None) -> str | Topic:
    for search_topic in search_topics or []:
        if isinstance(search_topic, Topic) and search_topic.name == topic:
            return search_topic
    return topic


def _fit(memories: list[Memory]) -> tuple[list[Memory], str]:
    """As many whole memories as fit in MAX_CHARS, or else the first one cut short."""
    bullets = [f"- {m.content.strip()}" for m in memories]
    kept, size = [], 0
    for bullet in bullets:
        size += len(bullet) + 1
        if size > MAX_CHARS:
            break
        kept.append(bullet)
    if bullets and not kept:
        kept = [bullets[0][:MAX_CHARS] + TRUNCATED]
    return memories[: len(kept)], "\n".join(kept)
