"""Store hook tests. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_store_hook
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

from assistants import ClaudeCode  # noqa: E402
from core.hooks import store  # noqa: E402


def prompt(text, prompt_id, kind="human"):
    return {
        "type": "user",
        "promptId": prompt_id,
        "origin": {"kind": kind},
        "message": {"content": [{"type": "text", "text": text}]},
    }


class StoreHookTest(unittest.TestCase):
    def setUp(self):
        self.added = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        client = SimpleNamespace(
            memories=SimpleNamespace(
                add=lambda messages, user_id=None, properties=None: self.added.append(messages)
                or SimpleNamespace(run_id="r1")
            )
        )
        for patch in (
            unittest.mock.patch.object(store, "get_client", lambda *_: client),
            unittest.mock.patch.object(store, "get_user_id", lambda: "u@x"),
            unittest.mock.patch.object(store, "resolve_scope", lambda *_: ({}, False, [])),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_hook(self, entries):
        """The turn under test is the last entry, since a turn is stored when it ends."""
        path = os.path.join(self.tmp.name, "transcript.jsonl")
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        payload = {
            "prompt_id": entries[-1]["promptId"],
            "session_id": "s",
            "transcript_path": path,
            "cwd": self.tmp.name,
            "last_assistant_message": "did the thing",
        }
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            return store.run(ClaudeCode())

    def test_human_turn_is_stored_with_both_halves(self):
        self.assertEqual(self.run_hook([prompt("fix the scope config", "a")]), 0)
        self.assertEqual(
            self.added,
            [[{"role": "user", "content": "fix the scope config"},
              {"role": "assistant", "content": "did the thing"}]],
        )

    def test_host_generated_turn_is_not_stored(self):
        entries = [prompt("fix the scope config", "a"), prompt("…", "b", "task-notification")]
        self.assertEqual(self.run_hook(entries), 0)
        self.assertEqual(self.added, [])


if __name__ == "__main__":
    unittest.main()
