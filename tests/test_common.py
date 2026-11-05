"""Tests for the shared helpers in ``fusion_bridge/common``."""

import json
import unittest

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge.common import api, results


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


class Indexed:
    """A count/item collection; ``bad`` indexes raise on read."""

    def __init__(self, members, bad=()):
        self.members = members
        self.bad = bad

    @property
    def count(self):
        return len(self.members)

    def item(self, index):
        if index in self.bad:
            raise RuntimeError("stale proxy")
        return self.members[index]


class Vector:
    """A Fusion vector: iteration and len, no count or item."""

    def __init__(self, members):
        self.members = members

    def __iter__(self):
        return iter(self.members)

    def __len__(self):
        return len(self.members)


class Raises:
    @property
    def value(self):
        raise RuntimeError("cloud read failed")


class SafeTests(unittest.TestCase):
    def test_safe_reads_and_defaults(self):
        self.assertEqual(api.safe(Indexed([]), "members"), [])
        self.assertIsNone(api.safe(Raises(), "value"))
        self.assertEqual(api.safe(Raises(), "value", 3), 3)
        self.assertEqual(api.safe(None, "anything", 3), 3)

    def test_safe_keeps_a_none_value(self):
        self.assertIsNone(api.safe(Indexed(None), "members", 3))

    def test_present_treats_none_as_missing(self):
        self.assertEqual(api.present(Indexed(None), "members", 3), 3)
        self.assertEqual(api.present(Raises(), "value", 3), 3)
        self.assertEqual(api.present(Indexed(0), "members", 3), 0)

    def test_attempt(self):
        self.assertEqual(api.attempt(lambda: 1), 1)
        self.assertEqual(api.attempt(lambda: Raises().value, 2), 2)
        self.assertIsNone(api.attempt(lambda: None.anything))


class ItemsTests(unittest.TestCase):
    def test_collection_vector_and_list(self):
        for source in (Indexed([1, 2]), Vector([1, 2]), [1, 2], (1, 2)):
            self.assertEqual(api.items(source), [1, 2])
            self.assertEqual(api.strict_items(source), [1, 2])
        self.assertEqual(api.items(iter([1, 2])), [1, 2])
        self.assertEqual(api.strict_items(iter([1, 2])), [1, 2])

    def test_none_is_empty(self):
        self.assertEqual(api.items(None), [])
        self.assertEqual(api.strict_items(None), [])

    def test_none_members_are_dropped(self):
        for source in (Indexed([1, None, 2]), Vector([1, None, 2])):
            self.assertEqual(api.items(source), [1, 2])
            self.assertEqual(api.strict_items(source), [1, 2])

    def test_a_list_is_not_a_collection_despite_its_count_method(self):
        self.assertEqual(api.strict_items(["a"]), ["a"])

    def test_items_skips_a_member_that_raises(self):
        self.assertEqual(api.items(Indexed([1, 2, 3], bad={1})), [1, 3])

    def test_strict_items_raises_on_a_member_that_raises(self):
        with self.assertRaises(RuntimeError):
            api.strict_items(Indexed([1, 2, 3], bad={1}))

    def test_items_of_a_non_collection_is_empty(self):
        self.assertEqual(api.items(object()), [])

    def test_strict_items_of_a_non_collection_raises(self):
        with self.assertRaises(TypeError):
            api.strict_items(object())

    def test_items_survives_a_failing_iterator(self):
        def broken():
            yield 1
            raise RuntimeError("vector went stale")

        self.assertEqual(api.items(broken()), [])

    def test_limit(self):
        self.assertEqual(api.items(Indexed([1, 2, 3]), limit=2), [1, 2])
        self.assertEqual(api.items(Vector([1, 2, 3]), limit=2), [1, 2])
        self.assertEqual(api.items(Indexed([1]), limit=5), [1])

    def test_item_without_count_falls_back_to_iteration(self):
        class Odd(Vector):
            def item(self, index):
                raise AssertionError("no count, so item must not be used")

        self.assertEqual(api.items(Odd([1])), [1])


if __name__ == "__main__":
    unittest.main()
