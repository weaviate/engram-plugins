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

from engram import CommittedOperation, CommittedOperations, Run, RunStatus  # noqa: E402

from assistants.claude_code import ClaudeCode  # noqa: E402
from core import session_state  # noqa: E402
from core.hooks import store  # noqa: E402


def prompt(text, prompt_id, kind="human"):
    return {
        "type": "user",
        "promptId": prompt_id,
        "origin": {"kind": kind},
        "message": {"content": [{"type": "text", "text": text}]},
    }


def run_status(status, created=(), updated=()):
    def ops(ids):
        return [CommittedOperation(memory_id=i, committed_at="2026-10-07T12:00:00Z") for i in ids]

    return RunStatus(
        run_id="r1",
        status=status,
        group_id="g",
        starting_step=0,
        input_type="conversation",
        created_at="2026-10-07T12:00:00Z",
        updated_at="2026-10-07T12:00:00Z",
        committed_operations=(
            CommittedOperations(created=ops(created), updated=ops(updated))
            if created or updated
            else None
        ),
    )


class StoreHookTest(unittest.TestCase):
    def setUp(self):
        self.added = []
        self.status = run_status("completed", created=["m1"], updated=["m2"])
        self.polls = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        client = SimpleNamespace(
            memories=SimpleNamespace(add=self.add),
            runs=SimpleNamespace(get=self.get_run),
        )
        data_dir = os.path.join(self.tmp.name, "data")
        for patch in (
            unittest.mock.patch.object(store, "get_client", lambda *_: client),
            unittest.mock.patch.object(store, "get_user_id", lambda: "u@x"),
            unittest.mock.patch.object(store, "resolve_scope", lambda *_: ({}, False, [])),
            unittest.mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": data_dir}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def add(self, messages, user_id=None, properties=None):
        self.added.append(messages)
        return Run(run_id="r1", status="running")

    def get_run(self, run_id):
        self.polls += 1
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

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

    def test_only_the_memories_a_store_created_are_recorded_as_this_sessions(self):
        """A memory this turn merged into is mostly earlier sessions' knowledge, so it stays
        recallable."""
        self.assertEqual(self.run_hook([prompt("fix the scope config", "a")]), 0)
        self.assertEqual(session_state.load("s", "own"), {"m1"})

    def test_a_buffered_run_ends_the_poll_at_once(self):
        """in_buffer waits on a trigger that can take minutes, so there is nothing to wait for."""
        self.status = run_status("in_buffer")
        self.assertEqual(self.run_hook([prompt("fix the scope config", "a")]), 0)
        self.assertEqual(self.polls, 1)
        self.assertEqual(session_state.load("s", "own"), set())

    def test_a_run_still_going_at_the_deadline_is_given_up(self):
        self.status = run_status("running")
        with unittest.mock.patch.object(store, "OWN_POLL_SECONDS", 0):
            self.assertEqual(self.run_hook([prompt("fix the scope config", "a")]), 0)
        self.assertEqual(self.polls, 1)
        self.assertEqual(session_state.load("s", "own"), set())

    def test_a_failed_poll_is_not_a_failed_store(self):
        """Only a failed add wakes Claude. The turn is saved either way."""
        self.status = ConnectionError("unreachable")
        self.assertEqual(self.run_hook([prompt("fix the scope config", "a")]), 0)
        self.assertEqual(len(self.added), 1)


if __name__ == "__main__":
    unittest.main()
