"""Session state tests. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_session_state
"""

import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import session_state  # noqa: E402

SESSION = "0c9f6d1e-5a7b-4c2d-9e8f-1a2b3c4d5e6f"


class SessionStateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = tmp.name
        patch = unittest.mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.data})
        patch.start()
        self.addCleanup(patch.stop)

    def test_ids_accumulate(self):
        self.assertEqual(session_state.load(SESSION, "own"), set())
        session_state.add(SESSION, "own", ["a", "b"])
        session_state.add(SESSION, "own", ["b", "c"])
        self.assertEqual(session_state.load(SESSION, "own"), {"a", "b", "c"})

    def test_kinds_and_sessions_are_separate(self):
        session_state.add(SESSION, "own", ["written"])
        session_state.add(SESSION, "shown", ["injected"])
        self.assertEqual(session_state.load(SESSION, "shown"), {"injected"})
        self.assertEqual(session_state.load("another-session", "own"), set())

    def test_reset_clears_one_kind(self):
        session_state.add(SESSION, "own", ["written"])
        session_state.add(SESSION, "shown", ["injected"])

        session_state.reset(SESSION, "shown")

        self.assertEqual(session_state.load(SESSION, "shown"), set())
        self.assertEqual(session_state.load(SESSION, "own"), {"written"})

    def test_clean_deletes_sessions_untouched_for_a_week(self):
        session_state.add("old", "own", ["a"])
        session_state.add("recent", "own", ["a"])
        stale = time.time() - session_state.MAX_AGE_SECONDS - 60
        old = os.path.join(self.data, "sessions", "old.own")
        os.utime(old, (stale, stale))

        session_state.add(SESSION, "shown", ["b"])
        self.assertTrue(os.path.exists(old), "a write must not clean")

        session_state.clean()

        self.assertFalse(os.path.exists(old))
        self.assertEqual(session_state.load("recent", "own"), {"a"})
        self.assertEqual(session_state.load(SESSION, "shown"), {"b"})

    def test_clean_carries_on_past_a_file_it_cannot_remove(self):
        """Another session's cleanup can delete a file first, and a stray entry must not stop
        every cleanup after it."""
        stale = time.time() - session_state.MAX_AGE_SECONDS - 60
        for name in ("a", "b", "c"):
            session_state.add(name, "own", ["x"])
            os.utime(os.path.join(self.data, "sessions", f"{name}.own"), (stale, stale))
        remove, calls = os.remove, []

        def remove_all_but_the_first(path):
            calls.append(path)
            if len(calls) == 1:
                raise FileNotFoundError(path)
            remove(path)

        with unittest.mock.patch.object(session_state.os, "remove", remove_all_but_the_first):
            session_state.clean()

        self.assertEqual(len(os.listdir(os.path.join(self.data, "sessions"))), 1)

    def test_clean_with_nothing_to_clean_does_not_raise(self):
        session_state.clean()
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": ""}):
            session_state.clean()

    def test_reset_with_nothing_to_reset_does_not_raise(self):
        session_state.reset(SESSION, "shown")

    def test_without_a_data_dir_nothing_raises(self):
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": ""}):
            session_state.add(SESSION, "own", ["a"])
            session_state.reset(SESSION, "own")
            self.assertEqual(session_state.load(SESSION, "own"), set())

    def test_a_session_id_that_is_not_a_plain_token_writes_nothing(self):
        """The id comes from the hook payload and becomes a file name."""
        for bad in ("", "../escape", "a/b", ".", ".."):
            session_state.add(bad, "own", ["a"])
            self.assertEqual(session_state.load(bad, "own"), set(), bad)
            session_state.reset(bad, "own")
        self.assertEqual(os.listdir(self.data), [])


if __name__ == "__main__":
    unittest.main()
