"""Assistant seam tests. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_assistants
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.assistants import Assistant, ClaudeCode, Codex  # noqa: E402

CLAUDE_CODE = ClaudeCode()
CODEX = Codex()


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

    def test_reads_the_hosts_label(self):
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


class EntryPointTest(unittest.TestCase):
    """Each assistant gets its own entry module naming itself, so nothing resolves an assistant
    at runtime and there is no selection to get wrong."""

    def test_store_failure_exit_differs_per_assistant(self):
        """Claude Code turns exit 2 into a wake carrying the reason; Codex reads it as
        "continue the turn", which would feed the error back as an instruction."""
        self.assertEqual(CLAUDE_CODE.STORE_FAILURE_EXIT, 2)
        self.assertEqual(CODEX.STORE_FAILURE_EXIT, 0)

    def test_every_assistant_has_an_entry_point(self):
        root = os.path.join(os.path.dirname(__file__), "..", "core")
        for assistant in (CLAUDE_CODE, CODEX):
            name = assistant.NAME.replace("-", "_")
            self.assertTrue(os.path.isfile(os.path.join(root, "entry", f"{name}.py")), name)

    def test_each_assistant_ships_a_manifest_and_a_hooks_file(self):
        """No hooks/hooks.json: Claude Code merges the default into whatever the manifest names,
        so a shared default would leak one assistant's entry point into the other's session."""
        root = os.path.join(os.path.dirname(__file__), "..")
        self.assertFalse(os.path.exists(os.path.join(root, "hooks", "hooks.json")))
        for manifest_dir, entry in ((".claude-plugin", "claude_code"), (".codex-plugin", "codex")):
            with open(os.path.join(root, manifest_dir, "plugin.json")) as f:
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
            self.assertTrue(commands, manifest_dir)
            for command in commands:
                self.assertIn(f"-m core.entry.{entry} ", command)


if __name__ == "__main__":
    unittest.main()
