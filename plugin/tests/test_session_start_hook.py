"""SessionStart hook tests. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_session_start_hook
"""

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engram import FetchRetrieval, Memory, SearchResults, Topic  # noqa: E402

from assistants.claude_code import ClaudeCode  # noqa: E402
from core import config, search as core_search, session_state  # noqa: E402
from core.hooks import post_compact, search, session_start  # noqa: E402

SESSION = "0c9f6d1e-5a7b-4c2d-9e8f-1a2b3c4d5e6f"


def memory(memory_id, content, created_at="2026-10-07T12:00:00Z"):
    return Memory(
        id=memory_id,
        project_id="p",
        content=content,
        topic="DeveloperPreferences",
        group="default",
        created_at=created_at,
        updated_at=created_at,
    )


def schema(*topic_names):
    return {"raw": {"topics": [{"topic_name": name} for name in topic_names]}}


class SessionStartTopicTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = os.path.join(tmp.name, "repo")
        os.makedirs(self.cwd)
        self.user_config = os.path.join(tmp.name, "config.json")
        self.schema = schema("DeveloperPreferences", "Processes")
        for patch in (
            unittest.mock.patch.object(config, "user_config_path", lambda: self.user_config),
            unittest.mock.patch.object(core_search, "scope_schema", lambda: self.schema),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def configure(self, path, session_start):
        with open(path, "w") as f:
            json.dump({"session_start": session_start}, f)

    def topic(self):
        return core_search.session_start_topic(self.cwd)

    def test_defaults_to_developer_preferences_when_the_schema_has_it(self):
        self.assertEqual(self.topic(), ("DeveloperPreferences", None))
        self.schema = schema("Processes")
        self.assertEqual(self.topic(), (None, None))

    def test_config_chooses_the_topic(self):
        self.configure(self.user_config, {"topic": "Processes"})
        self.assertEqual(self.topic(), ("Processes", None))

    def test_a_topic_missing_from_the_schema_is_still_used(self):
        self.configure(self.user_config, {"topic": "tooling_preferences"})
        self.assertEqual(self.topic(), ("tooling_preferences", None))

    def test_null_or_empty_turns_it_off(self):
        for off in (None, "", "  "):
            self.configure(self.user_config, {"topic": off})
            self.assertEqual(self.topic(), (None, None), off)

    def test_local_config_overrides_global(self):
        self.configure(self.user_config, {"topic": "Processes"})
        self.configure(os.path.join(self.cwd, ".engram.json"), {"topic": None})
        self.assertEqual(self.topic(), (None, None))

    def test_a_topic_that_is_not_a_name_is_an_error(self):
        self.configure(self.user_config, {"topic": ["DeveloperPreferences"]})
        topic, error = self.topic()
        self.assertIsNone(topic)
        self.assertIn("session_start.topic", error)


class SessionStartHookTest(unittest.TestCase):
    def setUp(self):
        self.results = []
        self.calls = []
        self.topic = ("DeveloperPreferences", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = SimpleNamespace(memories=SimpleNamespace(search=self.search))
        patches = [
            unittest.mock.patch.object(session_start, "session_start_topic", lambda _: self.topic),
            unittest.mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": tmp.name}),
        ]
        for hook in (session_start, search):
            patches += [
                unittest.mock.patch.object(hook, "engram_warning", lambda: None),
                unittest.mock.patch.object(hook, "get_client", lambda *_: client),
                unittest.mock.patch.object(hook, "get_user_id", lambda: "u@x"),
                unittest.mock.patch.object(hook, "search_filters", lambda *_: (None, None, [])),
            ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.results, Exception):
            raise self.results
        return SearchResults(self.results, len(self.results))

    def run_hook(self, hook=session_start, **payload):
        payload = {"session_id": SESSION, "cwd": ".", "source": "startup", **payload}
        stdout = io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                unittest.mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(hook.run(ClaudeCode()), 0)
        if not stdout.getvalue():
            return ""
        return json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]

    def test_injects_the_topic(self):
        self.results = [memory("prefs", "prefers small commits")]
        context = self.run_hook()
        self.assertIn("- prefers small commits", context)
        (call,) = self.calls
        self.assertEqual(call["topics"], ["DeveloperPreferences"])
        self.assertIsInstance(call["retrieval_config"], FetchRetrieval)

    def test_uses_the_topic_filter_configured_for_search(self):
        cross_repo = Topic(name="DeveloperPreferences", properties={"repo_name": None})
        filters = ([cross_repo, "Processes"], {"repo_name": "owner/repo"}, [])
        with unittest.mock.patch.object(session_start, "search_filters", lambda *_: filters):
            self.run_hook()
        (call,) = self.calls
        self.assertEqual(call["topics"], [cross_repo])
        self.assertEqual(call["properties"], {"repo_name": "owner/repo"})

    def test_search_skips_what_session_start_injected(self):
        self.results = [memory("prefs", "prefers small commits")]
        self.run_hook()
        self.results = [memory("prefs", "prefers small commits"), memory("other", "fact")]
        context = self.run_hook(search, prompt="how do we commit")
        self.assertNotIn("prefers small commits", context)
        self.assertIn("fact", context)

    def test_compaction_in_either_order_keeps_search_from_repeating_the_topic(self):
        self.results = [memory("prefs", "prefers small commits"), memory("other", "fact")]
        for hooks in ([session_start, post_compact], [post_compact, session_start]):
            for hook in hooks:
                self.run_hook(hook, source="compact", trigger="auto")
            self.assertNotIn("prefers small commits", self.run_hook(search, prompt="commits?"))

    def test_search_recalls_the_topic_when_session_start_fails(self):
        self.results = [memory("prefs", "prefers small commits")]
        self.run_hook()
        self.results = RuntimeError("connection reset")
        self.run_hook(source="compact")
        self.results = [memory("prefs", "prefers small commits")]
        self.assertIn("prefers small commits", self.run_hook(search, prompt="commits?"))

    def test_no_topic_fetches_nothing(self):
        self.topic = (None, None)
        self.assertEqual(self.run_hook(), "")
        self.assertEqual(self.calls, [])

    def test_an_empty_topic_injects_nothing(self):
        self.assertEqual(self.run_hook(), "")
        self.assertEqual(session_state.load(SESSION, "preloaded"), set())

    def test_a_config_error_is_reported_without_fetching(self):
        self.topic = (None, "config session_start.topic must be a topic name")
        self.assertIn("Engram · config session_start.topic", self.run_hook())
        self.assertEqual(self.calls, [])

    def test_a_failed_fetch_is_reported(self):
        self.results = RuntimeError("connection reset")
        self.assertIn("connection reset", self.run_hook())

    def test_a_malformed_memory_is_reported_not_raised(self):
        self.results = [memory("a", "fact a"), memory("b", "fact b", created_at=None)]
        self.assertIn("Engram · loading memories at session start failed", self.run_hook())

    def test_a_missing_key_is_left_for_search_to_report(self):
        with unittest.mock.patch.object(session_start, "engram_warning", lambda: "no key"):
            self.assertEqual(self.run_hook(), "")
        self.assertEqual(self.calls, [])

    def test_still_cleans_old_session_state(self):
        with unittest.mock.patch.object(session_state, "clean") as clean, \
                unittest.mock.patch.object(session_start, "engram_warning", lambda: "no key"):
            self.run_hook()
        clean.assert_called_once()

    def test_injects_the_newest_memories_that_fit(self):
        half = "x" * (session_start.MAX_CHARS // 2)
        self.results = [
            memory("oldest", f"oldest {half}", "2026-10-01T00:00:00Z"),
            memory("middle", f"middle {half}", "2026-10-02T00:00:00Z"),
            memory("newest", "newest", "2026-10-03T00:00:00Z"),
        ]
        context = self.run_hook()
        self.assertLess(context.index("newest"), context.index("middle"))
        self.assertNotIn("oldest", context)
        self.assertNotIn(session_start.TRUNCATED, context)
        self.assertEqual(session_state.load(SESSION, "preloaded"), {"newest", "middle"})

    def test_a_memory_too_long_to_fit_is_cut_not_dropped(self):
        self.results = [memory("prefs", "p" * (session_start.MAX_CHARS * 2))]
        context = self.run_hook()
        self.assertIn(session_start.TRUNCATED, context)
        self.assertLess(len(context), session_start.MAX_CHARS + 500)
        self.assertEqual(session_state.load(SESSION, "preloaded"), {"prefs"})


if __name__ == "__main__":
    unittest.main()
