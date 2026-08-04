"""Engram memory plugin — MemoryProvider interface.

Persistent, cross-session memory backed by Weaviate Engram
(https://docs.weaviate.io/engram). Conversation turns are sent to Engram for
server-side extraction; relevant memories are recalled before each turn.

Configuration
-------------
Secret (lives in $HERMES_HOME/.env or the environment):
  ENGRAM_API_KEY       — Engram API key (required). Get one at
                         https://console.weaviate.cloud/engram

Behavioral settings (live in $HERMES_HOME/engram.json, set via `hermes memory
setup`; env vars are read as fallback):
  user_id              — Canonical user identifier (env: ENGRAM_USER_ID). When
                         set, it applies uniformly across every gateway so the
                         same human gets one memory store. When unset, the
                         gateway-native id is used, falling back to
                         `git config user.email`. NEVER falls back to a shared
                         literal — a non-unique id would commingle different
                         people's memories with no way to un-mix them later.
  base_url             — Engram endpoint override (env: ENGRAM_BASE_URL;
                         default https://api.engram.weaviate.io).

Writes attach `session_id` as the scope property (the default Engram project's
only scope property); recall is scoped to the user. Fail-open throughout: a
missing key/identity or a down API disables memory — it never breaks a session.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.engram.weaviate.io"

# Circuit breaker: after this many consecutive failures, pause API calls for
# _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120
# How long prefetch() waits on an in-flight recall before skipping injection.
_PREFETCH_WAIT_SECS = 3


def _is_client_error(exc: Exception) -> bool:
    """True for user-caused errors (bad request, not found) that should NOT trip
    the circuit breaker."""
    err_str = str(exc).lower()
    return "404" in err_str or "not found" in err_str or "400" in err_str


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from env vars, with $HERMES_HOME/engram.json overrides.

    Environment variables provide defaults; engram.json (if present) overrides
    individual keys. Empty values in the file are ignored so a partial file
    never blanks out an env var.
    """
    from hermes_constants import get_hermes_home

    config = {
        "api_key": os.environ.get("ENGRAM_API_KEY", ""),
        "user_id": os.environ.get("ENGRAM_USER_ID", ""),
        "base_url": os.environ.get("ENGRAM_BASE_URL", DEFAULT_BASE_URL),
    }
    config_path = get_hermes_home() / "engram.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass
    return config


def _git_email() -> str:
    """`git config user.email`, or "" — git missing/timed out/not set all degrade
    to no identity."""
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return ""
    return out.stdout.strip()


def _memory_contents(results) -> List[str]:
    """Extract content strings from a search result page, tolerating both SDK
    objects and plain dicts."""
    lines = []
    for m in results or []:
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        if content:
            lines.append(str(content).strip())
    return lines


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "engram_search",
    "description": (
        "Search the user's long-term memories by meaning. Use this before "
        "answering any question that may depend on what you know about the "
        "user (preferences, facts, history, people, projects, past decisions). "
        "For multi-part questions, call it several times with different "
        "wording — one search is rarely enough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
        },
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "engram_add",
    "description": (
        "Store a durable fact about the user. Call this the moment the user "
        "states a lasting preference, correction, decision, or personal detail "
        "worth recalling on future turns — don't wait to be asked to remember. "
        "Skip transient chit-chat and facts already stored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
        },
        "required": ["content"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class EngramMemoryProvider(MemoryProvider):
    """Weaviate Engram memory with server-side extraction and semantic recall."""

    def __init__(self):
        self._config = None
        self._client = None
        self._enabled = False
        self._user_id = ""
        self._session_id = ""
        self._agent_context = "primary"
        self._sync_thread = None
        self._prefetch_thread = None
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        self._prefetch_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._breaker_lock = threading.Lock()
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "engram"

    def is_available(self) -> bool:
        """No network calls — just check an API key is configured."""
        try:
            return bool(_load_config().get("api_key"))
        except Exception:
            return False

    # -- Config -------------------------------------------------------------

    def get_config_schema(self):
        return [
            {
                "key": "api_key",
                "description": "Engram API key",
                "secret": True,
                "required": True,
                "env_var": "ENGRAM_API_KEY",
                "url": "https://console.weaviate.cloud/engram",
            },
            {
                "key": "user_id",
                "description": "User identifier (blank → gateway id, then git user.email)",
                "required": False,
            },
        ]

    def save_config(self, values, hermes_home):
        """Write non-secret config to $HERMES_HOME/engram.json (merge, so an
        existing base_url survives a re-run of the wizard)."""
        import json as _json
        from pathlib import Path

        config_path = Path(hermes_home) / "engram.json"
        existing = {}
        if config_path.exists():
            try:
                existing = _json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(values)
        config_path.write_text(_json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    # -- Lifecycle ----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        self._session_id = session_id or ""
        self._agent_context = kwargs.get("agent_context") or "primary"
        if not (self._config.get("api_key") or "").strip():
            logger.warning("Engram: ENGRAM_API_KEY not set — memory disabled.")
            self._enabled = False
            return
        # Identity: operator-configured user_id → gateway-native id → git email.
        # No shared literal fallback — memories are tagged with this id
        # permanently, so a non-unique id would commingle different people's
        # memories with no way to un-mix them later.
        self._user_id = (
            (self._config.get("user_id") or "").strip()
            or (kwargs.get("user_id") or "").strip()
            or _git_email()
        )
        if not self._user_id:
            logger.warning(
                "Engram: no stable identity — set user_id in engram.json / "
                "ENGRAM_USER_ID, or git config user.email. Memory disabled "
                "(prevents mixing memories between users)."
            )
            self._enabled = False
            return
        try:
            from engram import EngramClient

            self._client = EngramClient(
                api_key=self._config["api_key"],
                base_url=self._config.get("base_url") or DEFAULT_BASE_URL,
            )
        except Exception as e:
            logger.error("Engram client failed to initialize: %s", e)
            self._client = None
            self._enabled = False
            return
        self._enabled = True

    def system_prompt_block(self) -> str:
        if not self._enabled:
            return ""
        return (
            "# Engram Memory\n"
            f"Active. User: {self._user_id}.\n"
            "You have persistent memory of this user from past conversations. "
            "Call engram_search before answering anything that could depend on "
            "prior context (the user's preferences, facts, history, people, "
            "projects, or earlier decisions) — do not rely on the chat window "
            "alone, and do not assume you have no memory.\n"
            "Call engram_add the moment the user states a lasting preference, "
            "correction, decision, or personal detail worth recalling later.\n"
            "Tools: engram_search to find memories, engram_add to store facts."
        )

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        try:
            if self._client and hasattr(self._client, "close"):
                self._client.close()
        except Exception:
            pass
        self._client = None
        self._enabled = False

    # -- Circuit breaker ------------------------------------------------------

    def _is_breaker_open(self) -> bool:
        with self._breaker_lock:
            if self._consecutive_failures < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._consecutive_failures = 0
                return False
            return True

    def _record_success(self):
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self):
        with self._breaker_lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            else:
                count = 0
        if count >= _BREAKER_THRESHOLD:
            logger.warning(
                "Engram circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds.",
                count, _BREAKER_COOLDOWN_SECS,
            )

    # -- Recall (background prefetch + hot-path consume) ----------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._start_prefetch(message)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._start_prefetch(query)

    def _consume_prefetch_result(self, query: str):
        with self._prefetch_lock:
            if self._prefetch_query != query or not self._prefetch_done:
                return None
            result = self._prefetch_result
            self._prefetch_result = ""
            self._prefetch_done = False
            return result

    def _start_prefetch(self, query: str) -> None:
        if not query or not self._enabled or self._client is None:
            return
        if self._is_breaker_open():
            return
        client = self._client
        user_id = self._user_id
        with self._prefetch_lock:
            if self._prefetch_query == query:
                if self._prefetch_done:
                    return
                if self._prefetch_thread and self._prefetch_thread.is_alive():
                    return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False

        def _run():
            body = ""
            try:
                results = client.memories.search(query=query, user_id=user_id)
                lines = _memory_contents(results)
                if lines:
                    body = "## Engram Memory\n" + "\n".join(f"- {l}" for l in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Engram prefetch failed: %s", e)
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result = body
                    self._prefetch_done = True

        t = threading.Thread(target=_run, daemon=True, name="engram-prefetch")
        with self._prefetch_lock:
            self._prefetch_thread = t
        t.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories for the CURRENT question with a short hot-path wait."""
        if not self._enabled:
            return ""
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        # Slow backend: skip injection; engram_search remains as the backstop.
        return ""

    # -- Store (always background, never blocks a turn) ------------------------

    def _start_add(self, messages: List[Dict[str, str]], session_id: str = "") -> None:
        """Fire a background memories.add. Serializes with any in-flight add:
        joins it briefly and skips on contention rather than duplicating."""
        if not self._enabled or self._client is None or self._is_breaker_open():
            return
        client = self._client
        user_id = self._user_id
        sid = session_id or self._session_id
        properties = {"session_id": sid} if sid else None

        def _add():
            try:
                client.memories.add(messages, user_id=user_id, properties=properties)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("Engram sync failed: %s", e)

        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=5.0)
            # Still alive after the wait → skip to avoid duplicate ingestion.
            if self._sync_thread and self._sync_thread.is_alive():
                return
            self._sync_thread = threading.Thread(target=_add, daemon=True, name="engram-sync")
            self._sync_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Persist a completed turn (non-blocking). Skipped for non-primary
        contexts — subagent/cron turns would corrupt the user's memory."""
        if self._agent_context not in ("primary", ""):
            return
        messages = []
        if (user_content or "").strip():
            messages.append({"role": "user", "content": user_content})
        if (assistant_content or "").strip():
            messages.append({"role": "assistant", "content": assistant_content})
        if not messages:
            return
        self._start_add(messages, session_id=session_id)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in MEMORY.md / USER.md writes into Engram. The backend
        is append-only: a 'replace' is stored as the new/corrected fact, and
        'remove' can't be propagated — logged instead."""
        if action not in ("add", "replace"):
            logger.debug(
                "Engram: not mirroring built-in memory %r on %s — append-only backend",
                action, target,
            )
            return
        text = (content or "").strip()
        if not text:
            return
        self._start_add([{"role": "user", "content": text}])

    # -- Tools -----------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, ADD_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._enabled or self._client is None:
            return json.dumps({"error": "Engram memory is not active (missing API key or identity)."})
        if self._is_breaker_open():
            return json.dumps({"error": "Engram temporarily unavailable (multiple consecutive failures). Will retry automatically."})

        if tool_name == "engram_search":
            query = (args.get("query") or "").strip()
            if not query:
                return json.dumps({"error": "Missing required parameter: query"})
            try:
                results = self._client.memories.search(query=query, user_id=self._user_id)
                self._record_success()
                lines = _memory_contents(results)
                if not lines:
                    return json.dumps({"result": "No relevant memories found."})
                return json.dumps({"results": lines, "count": len(lines)})
            except Exception as e:
                if not _is_client_error(e):
                    self._record_failure()
                return json.dumps({"error": f"Search failed: {e}"})

        if tool_name == "engram_add":
            content = (args.get("content") or "").strip()
            if not content:
                return json.dumps({"error": "Missing required parameter: content"})
            try:
                self._client.memories.add(
                    [{"role": "user", "content": content}],
                    user_id=self._user_id,
                    properties={"session_id": self._session_id} if self._session_id else None,
                )
                self._record_success()
                return json.dumps({"result": "Fact stored."})
            except Exception as e:
                if not _is_client_error(e):
                    self._record_failure()
                return json.dumps({"error": f"Failed to store: {e}"})

        return json.dumps({"error": f"Unknown tool: {tool_name}"})


def register(ctx) -> None:
    """Register Engram as a memory provider plugin."""
    ctx.register_memory_provider(EngramMemoryProvider())
