"""Loads the module directly by path — importing the `core` package would pull in the
Engram SDK, which only exists in the plugin venv."""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(__file__)
_MODULE = os.path.join(_HERE, "..", "core", "transcript.py")

spec = importlib.util.spec_from_file_location("transcript", _MODULE)
transcript = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transcript)


def prompt(text, prompt_id, kind="human"):
    return {
        "type": "user",
        "promptId": prompt_id,
        "origin": {"kind": kind},
        "message": {"content": [{"type": "text", "text": text}]},
    }


def tool_result(prompt_id):
    return {
        "type": "user",
        "promptId": prompt_id,
        "message": {"content": [{"type": "tool_result", "content": "…"}]},
    }


class PromptOriginTest(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for path in self.paths:
            os.unlink(path)

    def transcript_of(self, entries):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        self.paths.append(path)
        return path

    def test_reads_the_hosts_label(self):
        path = self.transcript_of(
            [prompt("fix the scope config", "a"), prompt("…", "b", "task-notification")]
        )
        self.assertEqual(transcript.prompt_origin(path, "a"), "human")
        self.assertEqual(transcript.prompt_origin(path, "b"), "task-notification")

    def test_skips_tool_results_sharing_the_prompt_id(self):
        path = self.transcript_of([prompt("fix the scope config", "a"), tool_result("a")])
        self.assertEqual(transcript.prompt_origin(path, "a"), "human")

    def test_unresolvable(self):
        path = self.transcript_of([prompt("hi", "a")])
        self.assertIsNone(transcript.prompt_origin(path, "missing"))
        self.assertIsNone(transcript.prompt_origin(path, ""))
        self.assertIsNone(transcript.prompt_origin(None, "a"))
        self.assertIsNone(transcript.prompt_origin("/nonexistent", "a"))

    def test_unreadable_transcript(self):
        self.assertIsNone(transcript.prompt_origin(os.path.dirname(__file__), "a"))

    def test_survives_a_malformed_line(self):
        path = self.transcript_of([prompt("hi", "a")])
        with open(path, "a") as f:
            f.write("{not json\n")
        self.assertEqual(transcript.prompt_origin(path, "a"), "human")


class IsAutomatedTest(unittest.TestCase):
    def test_human(self):
        self.assertFalse(transcript.is_automated("human"))

    def test_any_other_label(self):
        """Any label other than human gates, so a kind the host adds later still works."""
        for kind in ("task-notification", "some-future-kind"):
            self.assertTrue(transcript.is_automated(kind), kind)

    def test_unknown_origin_is_treated_as_human(self):
        self.assertFalse(transcript.is_automated(None))


if __name__ == "__main__":
    unittest.main()
