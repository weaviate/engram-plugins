"""Loads the module directly by path — importing the `core` package would pull in the
Engram SDK, which only exists in the plugin venv."""

import importlib.util
import json
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(__file__)
_MODULE = os.path.join(_HERE, "..", "core", "client_origin.py")

spec = importlib.util.spec_from_file_location("client_origin", _MODULE)
client_origin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_origin)


class PlatformTest(unittest.TestCase):
    def test_claude(self):
        self.assertEqual(client_origin._platform(), "claude")


class PluginVersionTest(unittest.TestCase):
    def test_reads_manifest(self):
        with open(client_origin._MANIFEST) as f:
            expected = json.load(f)["version"]
        self.assertEqual(client_origin._plugin_version(), expected)

    def test_missing_manifest(self):
        with mock.patch.object(client_origin, "_MANIFEST", "/nonexistent/plugin.json"):
            self.assertEqual(client_origin._plugin_version(), "unknown")


class HeaderTest(unittest.TestCase):
    def test_format(self):
        headers = client_origin.client_origin_header()
        self.assertEqual(list(headers), ["X-Engram-Client"])
        platform, _, version = headers["X-Engram-Client"].partition("/")
        self.assertEqual(platform, "claude-plugin")
        self.assertEqual(version, client_origin._plugin_version())


if __name__ == "__main__":
    unittest.main()
