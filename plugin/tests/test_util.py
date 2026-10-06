"""Loads the module directly by path — importing the `core` package would pull in the
Engram SDK, which only exists in the plugin venv."""

import importlib.util
import io
import os
import unittest
from unittest import mock

_MODULE = os.path.join(os.path.dirname(__file__), "..", "core", "util.py")

spec = importlib.util.spec_from_file_location("util", _MODULE)
util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(util)


class ReadInputTest(unittest.TestCase):
    def read(self, raw):
        with mock.patch.object(util.sys, "stdin", io.StringIO(raw)):
            return util.read_input()

    def test_an_object(self):
        self.assertEqual(self.read('{"prompt": "hi"}'), {"prompt": "hi"})

    def test_valid_json_that_is_not_an_object(self):
        """json.load accepts these, and every hook then calls .get on them."""
        for raw in ("[1, 2, 3]", '"a string"', "42", "null"):
            with self.assertRaises(TypeError, msg=raw):
                self.read(raw)

    def test_malformed_json_still_raises(self):
        with self.assertRaises(ValueError):
            self.read("{{{")


if __name__ == "__main__":
    unittest.main()
