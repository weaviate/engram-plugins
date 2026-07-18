"""Test harness: stub the Hermes internals and the Engram SDK in sys.modules,
then load the provider package by file path — mirroring Hermes' own
plugins/memory discovery (_load_provider_from_dir), so these tests double as a
drop-in compatibility check for an upstream PR.

No network, no real SDK, no Hermes checkout needed.
"""

import importlib.util
import os
import sys
import types
from abc import ABC, abstractmethod
from pathlib import Path

import pytest

ENGRAM_PKG = Path(__file__).resolve().parents[1] / "engram"


# --- Stub: agent.memory_provider (the ABC the provider imports at module top) ---

agent_mod = types.ModuleType("agent")
mp_mod = types.ModuleType("agent.memory_provider")


class MemoryProvider(ABC):
    """Minimal mirror of the real ABC's abstract surface (plus the optional
    hooks our provider overrides)."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None: ...

    @abstractmethod
    def get_tool_schemas(self): ...

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        pass

    def handle_tool_call(self, tool_name: str, args, **kwargs) -> str:
        raise NotImplementedError

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        pass

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def get_config_schema(self):
        return []

    def save_config(self, values, hermes_home) -> None:
        pass


mp_mod.MemoryProvider = MemoryProvider
agent_mod.memory_provider = mp_mod
sys.modules.setdefault("agent", agent_mod)
sys.modules["agent.memory_provider"] = mp_mod


# --- Stub: hermes_constants (imported lazily inside provider functions) ---

hermes_constants = types.ModuleType("hermes_constants")


def get_hermes_home():
    # Read at call time so each test's HERMES_HOME (tmp_path) applies.
    return Path(os.environ.get("HERMES_HOME", "/nonexistent-hermes-home"))


hermes_constants.get_hermes_home = get_hermes_home
sys.modules["hermes_constants"] = hermes_constants


# --- Stub: engram SDK (imported lazily in initialize) ---

class FakeMemories:
    def __init__(self):
        self.add_calls = []
        self.search_calls = []
        self.search_results = []
        self.error = None  # raised by add/search when set

    def add(self, messages, user_id=None, properties=None):
        if self.error:
            raise self.error
        self.add_calls.append(
            {"messages": messages, "user_id": user_id, "properties": properties}
        )
        return {}

    def search(self, query=None, user_id=None, topics=None, properties=None):
        if self.error:
            raise self.error
        self.search_calls.append(
            {"query": query, "user_id": user_id, "topics": topics, "properties": properties}
        )
        return self.search_results


class FakeEngramClient:
    instances = []

    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key
        self.base_url = base_url
        self.memories = FakeMemories()
        self.closed = False
        FakeEngramClient.instances.append(self)

    def close(self):
        self.closed = True


engram_mod = types.ModuleType("engram")
engram_mod.EngramClient = FakeEngramClient
sys.modules["engram"] = engram_mod


# --- Provider loading (register-pattern, like Hermes discovery) ---

def load_provider_module():
    init_file = ENGRAM_PKG / "__init__.py"
    spec = importlib.util.spec_from_file_location("plugins.memory.engram", str(init_file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    def __init__(self):
        self.provider = None

    def register_memory_provider(self, provider):
        self.provider = provider


@pytest.fixture
def mod():
    return load_provider_module()


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for var in ("ENGRAM_API_KEY", "ENGRAM_USER_ID", "ENGRAM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    FakeEngramClient.instances.clear()
    yield


@pytest.fixture
def provider(mod):
    ctx = _Ctx()
    mod.register(ctx)
    p = ctx.provider
    assert p is not None, "register(ctx) must register a provider instance"
    yield p
    p.shutdown()


def drain(p, timeout=5.0):
    """Wait for the provider's background threads (sync/prefetch) to finish."""
    for t in (p._sync_thread, p._prefetch_thread):
        if t and t.is_alive():
            t.join(timeout=timeout)
