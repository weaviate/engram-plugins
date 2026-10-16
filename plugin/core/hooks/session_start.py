"""SessionStart hook: delete stale session state, then inject the session start topics."""

import json
from collections.abc import Iterable

from engram import FetchRetrieval, Memory, Topic

from core import (
    debug,
    engram_warning,
    get_client,
    get_user_id,
    search_filters,
    session_start_topics,
    session_state,
)
from core.classes import Assistant

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
    session_state.clean(assistant.data_dir())

    data = assistant.read_input()
    session_state.reset(data.session_id, "preloaded", assistant.data_dir())
    if engram_warning():
        return 0
    client = get_client(assistant.name)
    if client is None:
        return 0

    try:
        topics, error = session_start_topics(data.cwd, assistant.data_dir())
        if error:
            emit_status(error)
            return 0
        if not topics:
            return 0
        search_topics, properties, _warnings = search_filters(data.cwd, data.session_id, assistant.data_dir())
        results = client.memories.search(
            query=" ".join(topics),
            user_id=get_user_id(),
            topics=[_with_search_filter(topic, search_topics) for topic in topics],
            retrieval_config=FetchRetrieval(limit=FETCH_LIMIT),
            properties=properties,
        )
        ordered = _by_topic_then_newest((m for m in results if m.content), topics)
    except Exception as e:
        debug("session start failed", error=e)
        emit_status(f"loading memories at session start failed: {e}")
        return 0

    injected, text = _fit(ordered)

    debug(
        "session start",
        topics=topics,
        fetched=len(ordered),
        injected=len(injected),
        chars=len(text),
    )

    if text:
        emit(
            f"Standing memory from Engram ({'topic' if len(topics) == 1 else 'topics'} "
            f"{', '.join(topics)}), loaded once for the whole session. "
            "Apply it throughout unless the user says otherwise:\n" + text
        )
    session_state.add(data.session_id, "preloaded", [m.id for m in injected], assistant.data_dir())
    return 0


def _with_search_filter(topic: str, search_topics: list[str | Topic] | None) -> str | Topic:
    for search_topic in search_topics or []:
        if isinstance(search_topic, Topic) and search_topic.name == topic:
            return search_topic
    return topic


def _by_topic_then_newest(memories: Iterable[Memory], topics: list[str]) -> list[Memory]:
    """Topics in the order configured, so the first ones get the space when not all fit."""
    rank = {topic: i for i, topic in enumerate(topics)}
    newest_first = sorted(memories, key=lambda m: m.created_at, reverse=True)
    return sorted(newest_first, key=lambda m: rank.get(m.topic, len(topics)))


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
