"""Tests for the shared helpers in ``fusion_bridge/common``."""

import json
import unittest

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge.common import results


class ResultsTests(unittest.TestCase):
    def text(self, result):
        return result["content"][0]["text"]

    def test_string_goes_out_unchanged(self):
        self.assertEqual(results.ok_result("plain"), {"content": [{"type": "text", "text": "plain"}], "isError": False})
        self.assertEqual(results.error_result("bad"), {"content": [{"type": "text", "text": "bad"}], "isError": True})

    def test_payload_is_json(self):
        self.assertEqual(self.text(results.ok_result({"a": 1})), json.dumps({"a": 1}, indent=2))
        self.assertEqual(self.text(results.error_result({"a": 1})), json.dumps({"a": 1}, indent=2))

    def test_indent_is_the_callers(self):
        self.assertEqual(self.text(results.ok_result({"a": [1]}, indent=None)), '{"a": [1]}')
        self.assertEqual(self.text(results.tool_result({"a": 1}, indent=1)), '{\n "a": 1\n}')

    def test_tool_result_flag(self):
        self.assertFalse(results.tool_result("x")["isError"])
        self.assertTrue(results.tool_result("x", True)["isError"])

    def test_unserializable_payload_raises(self):
        with self.assertRaises(TypeError):
            results.ok_result({"a": object()})


if __name__ == "__main__":
    unittest.main()
