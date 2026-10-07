"""Assistant seam tests. The SDK is assumed present — run under the plugin venv:

    cd plugin && ~/.claude/plugins/data/engram-*/venv/bin/python -m unittest tests.test_assistants
"""

import importlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from assistants.claude_code import ClaudeCode  # noqa: E402
from assistants.codex import Codex  # noqa: E402
from core.classes import Assistant, InputData  # noqa: E402

CLAUDE_CODE = ClaudeCode()
CODEX = Codex()


def _every_assistant():
    """Found on disk, so a new assistant module has to satisfy the wiring checks below."""
    found = []
    for path in sorted(pathlib.Path(__file__).parent.parent.glob("assistants/[!_]*.py")):
        module = importlib.import_module(f"assistants.{path.stem}")
        found += [
            value()
            for value in vars(module).values()
            if isinstance(value, type) and issubclass(value, Assistant) and value is not Assistant
        ]
    return found


EVERY_ASSISTANT = _every_assistant()


def read(assistant, payload):
    with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
        return assistant.read_input()


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
        return InputData(turn_id=prompt_id, transcript_path=path or self.path)

    def test_turn_id_is_the_prompt_id(self):
        self.assertEqual(read(CLAUDE_CODE, {"prompt_id": "a"}).turn_id, "a")
        self.assertEqual(read(CLAUDE_CODE, {"turn_id": "t1"}).turn_id, "")

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
            InputData(transcript_path=self.path),
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
        self.assertEqual(read(CODEX, {"turn_id": "t1"}).turn_id, "t1")
        self.assertEqual(read(CODEX, {"prompt_id": "a"}).turn_id, "")

    def test_every_turn_counts_as_human(self):
        """Codex documents no provenance field, so nothing is skipped."""
        self.assertFalse(CODEX.is_automated(InputData(turn_id="t1")))

    def test_reads_its_own_rollout_shape(self):
        """Codex wraps messages in `payload` and uses input_text blocks, so the Claude Code
        parser returns nothing for it."""
        self.write([rollout_message("user", "fix the scope config")])
        self.assertEqual(
            CODEX.last_user_text(InputData(transcript_path=self.path)), "fix the scope config"
        )
        self.assertEqual(CLAUDE_CODE.last_user_text(InputData(transcript_path=self.path)), "")

    def test_skips_injected_developer_context(self):
        self.write(
            [rollout_message("user", "the real prompt"),
             rollout_message("developer", "injected context nobody typed")]
        )
        self.assertEqual(
            CODEX.last_user_text(InputData(transcript_path=self.path)), "the real prompt"
        )

    def test_missing_transcript(self):
        self.assertEqual(CODEX.last_user_text(InputData()), "")
        self.assertEqual(CODEX.last_user_text(InputData(transcript_path="/nonexistent")), "")

    def test_survives_invalid_utf8(self):
        """A transcript read while it is being written can split a UTF-8 sequence. The bad line
        is lost to the json guard; the rest of the file still reads."""
        good = json.dumps(rollout_message("user", "survived")).encode()
        with open(self.path, "wb") as f:
            f.write(b"\xff\xfe broken\n" + good + b"\n")
        self.assertEqual(CODEX.last_user_text(InputData(transcript_path=self.path)), "survived")

    def test_a_rewritten_transcript_is_re_read(self):
        """The file read is cached because a hook walks it more than once per turn, so the cache
        has to notice the same path holding different content."""
        self.write([rollout_message("user", "first")])
        self.assertEqual(CODEX.last_user_text(InputData(transcript_path=self.path)), "first")
        self.write([rollout_message("user", "second")])
        self.assertEqual(CODEX.last_user_text(InputData(transcript_path=self.path)), "second")


class ReadInputTest(unittest.TestCase):
    def test_takes_the_fields_it_names_and_ignores_the_rest(self):
        """Hosts send more than the hooks use, and each event only a subset."""
        for assistant in EVERY_ASSISTANT:
            data = read(assistant, {"hook_event_name": "Stop", "cwd": "/repo", "prompt": None})
            self.assertEqual(data.cwd, "/repo", assistant.NAME)
            self.assertEqual(data.prompt, "", assistant.NAME)
            self.assertFalse(data.stop_hook_active, assistant.NAME)


class WiringTest(unittest.TestCase):
    """Each assistant module names itself when run, so nothing resolves an assistant at runtime
    and there is no selection to get wrong."""

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

    def test_running_an_assistant_does_not_double_import_it(self):
        """assistants/__init__.py must stay free of imports. Adding `from .claude_code import
        ClaudeCode` there is the obvious edit, and it makes `python -m assistants.claude_code`
        execute the module twice — two distinct classes, and a RuntimeWarning on stderr for every
        hook invocation."""
        root = pathlib.Path(__file__).parent.parent
        for assistant in EVERY_ASSISTANT:
            result = subprocess.run(
                [sys.executable, "-m", type(assistant).__module__],
                capture_output=True,
                text=True,
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(root), "PYTHONWARNINGS": "always"},
            )
            self.assertNotIn("RuntimeWarning", result.stderr, assistant.NAME)

    def test_every_assistant_is_wired_end_to_end(self):
        """Each registered assistant needs a manifest naming its own hooks file, and commands in
        that file running its own module. No hooks/hooks.json: Claude Code merges the default into
        whatever the manifest names, so a shared default would leak one assistant into the
        other's session."""
        root = os.path.join(os.path.dirname(__file__), "..")
        self.assertFalse(os.path.exists(os.path.join(root, "hooks", "hooks.json")))
        for assistant in EVERY_ASSISTANT:
            module = type(assistant).__module__
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
                self.assertIn(f"-m {module} ", command)

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
