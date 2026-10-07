"""Search hook tests. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_search_hook
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

from engram import Memory, SearchResults  # noqa: E402

from assistants.claude_code import ClaudeCode  # noqa: E402
from core import session_state  # noqa: E402
from core.hooks import post_compact, search  # noqa: E402

SESSION = "0c9f6d1e-5a7b-4c2d-9e8f-1a2b3c4d5e6f"


def memory(memory_id, content, **properties):
    return Memory(
        id=memory_id,
        project_id="p",
        content=content,
        topic="Processes",
        group="default",
        created_at="2026-10-07T12:00:00Z",
        updated_at="2026-10-07T12:00:00Z",
        properties={"repo_name": "owner/repo", **properties},
    )


class SearchHookTest(unittest.TestCase):
    def setUp(self):
        self.results = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = SimpleNamespace(
            memories=SimpleNamespace(
                search=lambda **_: SearchResults(self.results, len(self.results))
            )
        )
        for patch in (
            unittest.mock.patch.object(search, "engram_warning", lambda: None),
            unittest.mock.patch.object(search, "get_client", lambda *_: client),
            unittest.mock.patch.object(search, "get_user_id", lambda: "u@x"),
            unittest.mock.patch.object(search, "search_filters", lambda *_: (None, None, [])),
            unittest.mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": tmp.name}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def injected(self):
        payload = {"prompt": "how do we run the tests", "session_id": SESSION, "cwd": "."}
        stdout = io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                unittest.mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(search.run(ClaudeCode()), 0)
        if not stdout.getvalue():
            return ""
        return json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]

    def test_memories_this_session_wrote_are_not_injected(self):
        session_state.add(SESSION, "own", ["written-here"])
        self.results = [
            memory("summary", "this session so far", session_id=SESSION),
            memory("written-here", "a process noted earlier this session"),
            memory("earlier", "what another session did", session_id="another-session"),
            memory("durable", "tests run with unittest discover"),
        ]
        context = self.injected()
        self.assertNotIn("this session so far", context)
        self.assertNotIn("a process noted earlier this session", context)
        self.assertIn("what another session did", context)
        self.assertIn("tests run with unittest discover", context)

    def test_a_memory_is_injected_once_per_session(self):
        self.results = [memory("a", "fact a"), memory("b", "fact b")]
        self.assertIn("fact a", self.injected())
        self.assertEqual(self.injected(), "")

        self.results = [memory("a", "fact a"), memory("c", "fact c")]
        context = self.injected()
        self.assertNotIn("fact a", context)
        self.assertIn("fact c", context)

    def test_after_compaction_shown_memories_return(self):
        """Compaction drops injected context, so dedup must not outlive it."""
        self.results = [memory("a", "fact a")]
        self.injected()
        payload = {"session_id": SESSION, "trigger": "auto"}
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(post_compact.run(ClaudeCode()), 0)
        self.assertIn("fact a", self.injected())


if __name__ == "__main__":
    unittest.main()
