"""Unit tests for the Engram Hermes memory provider.

Everything runs against the fakes in conftest.py — no network, no real SDK.
"""

import json
from types import SimpleNamespace

from conftest import FakeEngramClient, drain


# --- Registration / availability -------------------------------------------

def test_register_and_name(provider, mod):
    assert provider.name == "engram"
    assert isinstance(provider, mod.MemoryProvider)


def test_is_available_requires_api_key(provider, monkeypatch):
    assert provider.is_available() is False
    monkeypatch.setenv("ENGRAM_API_KEY", "k")
    assert provider.is_available() is True


def test_config_file_overrides_env(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_API_KEY", "env-key")
    monkeypatch.setenv("ENGRAM_USER_ID", "env-user")
    (tmp_path / "engram.json").write_text(
        json.dumps({"api_key": "file-key", "user_id": "", "base_url": "http://x"})
    )
    from conftest import load_provider_module

    cfg = load_provider_module()._load_config()
    assert cfg["api_key"] == "file-key"          # file wins over env
    assert cfg["user_id"] == "env-user"          # empty file value ignored → env kept
    assert cfg["base_url"] == "http://x"


def test_get_config_schema(provider):
    schema = {f["key"]: f for f in provider.get_config_schema()}
    assert schema["api_key"]["secret"] is True
    assert schema["api_key"]["required"] is True
    assert schema["api_key"]["env_var"] == "ENGRAM_API_KEY"
    assert "user_id" in schema


def test_save_config_merges(provider, tmp_path):
    (tmp_path / "engram.json").write_text(json.dumps({"base_url": "http://keep"}))
    provider.save_config({"user_id": "u1"}, str(tmp_path))
    cfg = json.loads((tmp_path / "engram.json").read_text())
    assert cfg == {"base_url": "http://keep", "user_id": "u1"}


# --- initialize: guards and identity ----------------------------------------

def _no_git(monkeypatch, mod):
    """Simulate git being unusable / unset."""
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=""))


def test_initialize_disabled_without_api_key(provider):
    provider.initialize("s1", user_id="u")
    assert provider._enabled is False
    assert provider.system_prompt_block() == ""
    assert provider.prefetch("q") == ""


def test_initialize_disabled_without_identity(provider, mod, monkeypatch):
    monkeypatch.setenv("ENGRAM_API_KEY", "k")
    _no_git(monkeypatch, mod)
    provider.initialize("s1")  # no config user_id, no gateway id, no git email
    assert provider._enabled is False
    assert provider._client is None
    # hooks are no-ops, never raise
    provider.sync_turn("hi", "hello")
    assert provider.prefetch("q") == ""
    assert "not active" in provider.handle_tool_call("engram_search", {"query": "q"})


def test_identity_resolution_order(provider, mod, monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_API_KEY", "k")
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="git@example.com\n"))

    # 1. configured user_id beats the gateway id
    (tmp_path / "engram.json").write_text(json.dumps({"user_id": "configured"}))
    provider.initialize("s1", user_id="gateway")
    assert provider._user_id == "configured"

    # 2. gateway id beats git email
    (tmp_path / "engram.json").unlink()
    provider.initialize("s1", user_id="gateway")
    assert provider._user_id == "gateway"

    # 3. git email is the last resort
    provider.initialize("s1")
    assert provider._user_id == "git@example.com"


def test_initialize_creates_client(provider, monkeypatch):
    monkeypatch.setenv("ENGRAM_API_KEY", "k")
    provider.initialize("sess-1", user_id="u1")
    assert provider._enabled is True
    client = FakeEngramClient.instances[-1]
    assert client.api_key == "k"
    assert client.base_url == "https://api.engram.weaviate.io"


# --- sync_turn ---------------------------------------------------------------

def _initialized(provider, monkeypatch, **kwargs):
    monkeypatch.setenv("ENGRAM_API_KEY", "k")
    kwargs.setdefault("user_id", "u1")
    provider.initialize("sess-1", **kwargs)
    return FakeEngramClient.instances[-1]


def test_sync_turn_payload(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    provider.sync_turn("hi there", "hello!")
    drain(provider)
    assert len(client.memories.add_calls) == 1
    call = client.memories.add_calls[0]
    assert call["messages"] == [
        {"role": "user", "content": "hi there"},
        {"role": "assistant", "content": "hello!"},
    ]
    assert call["user_id"] == "u1"
    assert call["properties"] == {"session_id": "sess-1"}


def test_sync_turn_session_id_override(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    provider.sync_turn("a", "b", session_id="other-session")
    drain(provider)
    assert client.memories.add_calls[0]["properties"] == {"session_id": "other-session"}


def test_sync_turn_skips_non_primary_context(provider, monkeypatch):
    client = _initialized(provider, monkeypatch, agent_context="subagent")
    provider.sync_turn("hi", "hello")
    drain(provider)
    assert client.memories.add_calls == []


def test_sync_turn_empty_turn_ignored(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    provider.sync_turn("", "   ")
    drain(provider)
    assert client.memories.add_calls == []


# --- prefetch / recall --------------------------------------------------------

def test_prefetch_formats_memories(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    client.memories.search_results = [
        {"content": "likes dark mode"},
        SimpleNamespace(content="works at Weaviate"),
        {"content": ""},  # blank entries dropped
    ]
    body = provider.prefetch("what does the user like?")
    assert body == "## Engram Memory\n- likes dark mode\n- works at Weaviate"
    # search ran user-scoped
    assert client.memories.search_calls[0]["user_id"] == "u1"


def test_prefetch_empty_when_nothing_found(provider, monkeypatch):
    _initialized(provider, monkeypatch)
    assert provider.prefetch("anything") == ""


def test_prefetch_failure_is_silent(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    client.memories.error = RuntimeError("boom")
    assert provider.prefetch("q") == ""
    assert provider._consecutive_failures == 1


def test_on_turn_start_warms_cache(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    client.memories.search_results = [{"content": "cached fact"}]
    provider.on_turn_start(1, "tell me about myself")
    assert provider.prefetch("tell me about myself") == "## Engram Memory\n- cached fact"
    # exactly one search — prefetch consumed the warmed result
    assert len(client.memories.search_calls) == 1


# --- tools --------------------------------------------------------------------

def test_tool_search(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    client.memories.search_results = [{"content": "fact one"}, {"content": "fact two"}]
    out = json.loads(provider.handle_tool_call("engram_search", {"query": "q"}))
    assert out == {"results": ["fact one", "fact two"], "count": 2}


def test_tool_search_no_results(provider, monkeypatch):
    _initialized(provider, monkeypatch)
    out = json.loads(provider.handle_tool_call("engram_search", {"query": "q"}))
    assert out["result"] == "No relevant memories found."


def test_tool_search_missing_query(provider, monkeypatch):
    _initialized(provider, monkeypatch)
    out = json.loads(provider.handle_tool_call("engram_search", {}))
    assert "error" in out


def test_tool_add(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    out = json.loads(provider.handle_tool_call("engram_add", {"content": "I prefer vim"}))
    assert out["result"] == "Fact stored."
    assert client.memories.add_calls[0]["messages"] == [
        {"role": "user", "content": "I prefer vim"}
    ]


def test_tool_add_missing_content(provider, monkeypatch):
    _initialized(provider, monkeypatch)
    out = json.loads(provider.handle_tool_call("engram_add", {}))
    assert "error" in out


def test_tool_error_does_not_trip_breaker_on_client_error(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    client.memories.error = RuntimeError("404 not found")
    for _ in range(6):
        provider.handle_tool_call("engram_search", {"query": "q"})
    assert provider._consecutive_failures == 0  # client errors don't count


def test_unknown_tool(provider, monkeypatch):
    _initialized(provider, monkeypatch)
    out = json.loads(provider.handle_tool_call("engram_nope", {}))
    assert "Unknown tool" in out["error"]


def test_tool_schemas(provider):
    names = [s["name"] for s in provider.get_tool_schemas()]
    assert names == ["engram_search", "engram_add"]
    for schema in provider.get_tool_schemas():
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["required"]


# --- on_memory_write mirroring -------------------------------------------------

def test_on_memory_write_mirrors_add_and_replace(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    provider.on_memory_write("add", "memory", "user likes tea")
    drain(provider)
    provider.on_memory_write("replace", "user", "user likes coffee")
    drain(provider)
    assert len(client.memories.add_calls) == 2
    assert client.memories.add_calls[0]["messages"][0]["content"] == "user likes tea"
    assert client.memories.add_calls[1]["messages"][0]["content"] == "user likes coffee"


def test_on_memory_write_ignores_remove(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    provider.on_memory_write("remove", "memory", "obsolete fact")
    drain(provider)
    assert client.memories.add_calls == []


# --- circuit breaker ------------------------------------------------------------

def test_circuit_breaker_opens_after_repeated_failures(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    client.memories.error = RuntimeError("connection refused")
    for _ in range(5):
        provider.sync_turn("a", "b")
        drain(provider)
    assert provider._is_breaker_open() is True
    # while open, tools short-circuit and background work doesn't start
    out = json.loads(provider.handle_tool_call("engram_search", {"query": "q"}))
    assert "temporarily unavailable" in out["error"]
    thread_before = provider._sync_thread
    provider.sync_turn("a", "b")
    assert provider._sync_thread is thread_before


# --- shutdown -------------------------------------------------------------------

def test_shutdown_closes_client(provider, monkeypatch):
    client = _initialized(provider, monkeypatch)
    provider.sync_turn("a", "b")
    provider.shutdown()
    assert client.closed is True
    assert provider._enabled is False
