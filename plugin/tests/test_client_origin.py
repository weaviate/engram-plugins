"""Loads the module directly by path — importing the `core` package would pull in the
Engram SDK, which only exists in the plugin venv."""

import importlib.util
import json
import os
import unittest
from types import SimpleNamespace

_HERE = os.path.dirname(__file__)
_MODULE = os.path.join(_HERE, "..", "core", "client_origin.py")

spec = importlib.util.spec_from_file_location("client_origin", _MODULE)
client_origin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_origin)

ASSISTANTS = [
    SimpleNamespace(NAME="claude-code", MANIFEST_DIR=".claude-plugin"),
    SimpleNamespace(NAME="codex", MANIFEST_DIR=".codex-plugin"),
]


class HeaderTest(unittest.TestCase):
    def test_names_the_assistant_and_reads_its_own_manifest(self):
        """Each assistant ships its own manifest, so the version has to follow the assistant
        rather than whichever manifest sorts first."""
        for assistant in ASSISTANTS:
            manifest = os.path.join(_HERE, "..", assistant.MANIFEST_DIR, "plugin.json")
            with open(manifest) as f:
                expected = json.load(f)["version"]
            headers = client_origin.client_origin_header(assistant)
            self.assertEqual(list(headers), ["X-Engram-Client"])
            self.assertEqual(
                headers["X-Engram-Client"], f"{assistant.NAME}-plugin/{expected}"
            )

    def test_falls_back_when_no_assistant_is_named(self):
        """The migration CLI runs under an assistant it cannot identify."""
        self.assertTrue(
            client_origin.client_origin_header()["X-Engram-Client"].startswith("engram-plugin/")
        )

    def test_missing_manifest(self):
        self.assertEqual(client_origin._plugin_version(".nonexistent-plugin"), "unknown")


if __name__ == "__main__":
    unittest.main()
