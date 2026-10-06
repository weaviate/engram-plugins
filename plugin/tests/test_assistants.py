"""Assistant seam tests. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_assistants
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import assistants  # noqa: E402
from assistants import ClaudeCode, Codex  # noqa: E402
from core.assistant import Assistant  # noqa: E402

CLAUDE_CODE = ClaudeCode()
CODEX = Codex()
# Derived from the package so a newly registered assistant has to satisfy the checks below.
EVERY_ASSISTANT = [getattr(assistants, name)() for name in assistants.__all__]


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


class TranscriptFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "transcript.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, entries):
        with open(self.path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return self.path


class ClaudeCodeTest(TranscriptFixture):
    def payload(self, prompt_id, path=None):
        return {"prompt_id": prompt_id, "transcript_path": path or self.path}

    def test_turn_id(self):
        self.assertEqual(CLAUDE_CODE.turn_id({"prompt_id": "a"}), "a")
        self.assertEqual(CLAUDE_CODE.turn_id({}), "")

    def test_reads_the_assistants_label(self):
        self.write([prompt("fix the scope config", "a"), prompt("…", "b", "task-notification")])
        self.assertFalse(CLAUDE_CODE.is_automated(self.payload("a")))
        self.assertTrue(CLAUDE_CODE.is_automated(self.payload("b")))

    def test_skips_tool_results_sharing_the_prompt_id(self):
        self.write([prompt("fix the scope config", "a"), tool_result("a")])
        self.assertFalse(CLAUDE_CODE.is_automated(self.payload("a")))

    def test_unresolvable_origin_is_treated_as_human(self):
        self.write([prompt("hi", "a")])
        for payload in (
            self.payload("missing"),
            self.payload("a", "/nonexistent"),
            self.payload("a", os.path.dirname(__file__)),
            {"transcript_path": self.path},
        ):
            self.assertFalse(CLAUDE_CODE.is_automated(payload), payload)

    def test_survives_a_malformed_line(self):
        self.write([prompt("…", "a", "task-notification")])
        with open(self.path, "a") as f:
            f.write("{not json\n")
        self.assertTrue(CLAUDE_CODE.is_automated(self.payload("a")))

    def test_an_assistant_can_move_the_transcript_without_reimplementing_the_walk(self):
        """The payload key is the assistant's to name, so changing it must not force a copy of
        the JSONL reader."""

        class Elsewhere(ClaudeCode):
            def transcript_path(self, payload):
                return payload.get("rollout")

        self.write([prompt("fix the scope config", "a")])
        self.assertEqual(
            Elsewhere().last_user_text({"rollout": self.path}), "fix the scope config"
        )

    def test_last_user_text(self):
        self.write([prompt("fix the scope config", "a")])
        self.assertEqual(CLAUDE_CODE.last_user_text(self.payload("a")), "fix the scope config")


def rollout_message(role, text, kind="input_text"):
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
    }


class CodexTest(TranscriptFixture):
    def test_turn_id_uses_its_own_field(self):
        self.assertEqual(CODEX.turn_id({"turn_id": "t1"}), "t1")
        self.assertEqual(CODEX.turn_id({"prompt_id": "a"}), "")

    def test_every_turn_counts_as_human(self):
        """Codex documents no provenance field, so nothing is skipped."""
        self.assertFalse(CODEX.is_automated({"turn_id": "t1"}))

    def test_reads_its_own_rollout_shape(self):
        """Codex wraps messages in `payload` and uses input_text blocks, so the Claude Code
        parser returns nothing for it."""
        self.write([rollout_message("user", "fix the scope config")])
        self.assertEqual(
            CODEX.last_user_text({"transcript_path": self.path}), "fix the scope config"
        )
        self.assertEqual(CLAUDE_CODE.last_user_text({"transcript_path": self.path}), "")

    def test_skips_injected_developer_context(self):
        self.write(
            [rollout_message("user", "the real prompt"),
             rollout_message("developer", "injected context nobody typed")]
        )
        self.assertEqual(CODEX.last_user_text({"transcript_path": self.path}), "the real prompt")

    def test_missing_transcript(self):
        self.assertEqual(CODEX.last_user_text({}), "")
        self.assertEqual(CODEX.last_user_text({"transcript_path": "/nonexistent"}), "")

    def test_a_rewritten_transcript_is_re_read(self):
        """The file read is cached because a hook walks it more than once per turn, so the cache
        has to notice the same path holding different content."""
        self.write([rollout_message("user", "first")])
        self.assertEqual(CODEX.last_user_text({"transcript_path": self.path}), "first")
        self.write([rollout_message("user", "second")])
        self.assertEqual(CODEX.last_user_text({"transcript_path": self.path}), "second")


class EntryPointTest(unittest.TestCase):
    """Each assistant gets its own entry module naming itself, so nothing resolves an assistant
    at runtime and there is no selection to get wrong."""

    def test_store_failure_exit_differs_per_assistant(self):
        """Claude Code turns exit 2 into a wake carrying the reason; Codex reads it as
        "continue the turn", which would feed the error back as an instruction."""
        self.assertEqual(CLAUDE_CODE.STORE_FAILURE_EXIT, 2)
        self.assertEqual(CODEX.STORE_FAILURE_EXIT, 0)

    def test_the_contract_refuses_an_incomplete_assistant(self):
        """The point of the base class: a missing method fails at instantiation rather than at
        the first turn that needs it."""

        class Incomplete(Assistant):
            NAME = "incomplete"

        with self.assertRaises(TypeError):
            Incomplete()

    def test_every_assistant_is_wired_end_to_end(self):
        """Each registered assistant needs an entry module, a manifest naming its own hooks file,
        and commands in that file invoking its entry module. No hooks/hooks.json: Claude Code
        merges the default into whatever the manifest names, so a shared default would leak one
        assistant's entry point into the other's session."""
        root = os.path.join(os.path.dirname(__file__), "..")
        self.assertFalse(os.path.exists(os.path.join(root, "hooks", "hooks.json")))
        for assistant in EVERY_ASSISTANT:
            entry = assistant.NAME.replace("-", "_")
            self.assertTrue(os.path.isfile(os.path.join(root, "entry", f"{entry}.py")), entry)

            with open(os.path.join(root, assistant.MANIFEST_DIR, "plugin.json")) as f:
                manifest = json.load(f)
            hooks_path = manifest["hooks"]
            self.assertTrue(hooks_path.startswith("./"), hooks_path)
            with open(os.path.join(root, hooks_path[2:])) as f:
                hooks = json.load(f)
            commands = [
                hook["command"]
                for event in hooks["hooks"].values()
                for group in event
                for hook in group["hooks"]
            ]
            self.assertTrue(commands, assistant.NAME)
            for command in commands:
                self.assertIn(f"-m entry.{entry} ", command)

    def test_the_manifests_agree_on_the_version(self):
        """One plugin shipped to several assistants. Nothing else keeps these in step, and the
        client header reports whichever manifest its own assistant names."""
        root = os.path.join(os.path.dirname(__file__), "..")
        versions = set()
        for assistant in EVERY_ASSISTANT:
            with open(os.path.join(root, assistant.MANIFEST_DIR, "plugin.json")) as f:
                versions.add(json.load(f)["version"])
        self.assertEqual(len(versions), 1, versions)


if __name__ == "__main__":
    unittest.main()
