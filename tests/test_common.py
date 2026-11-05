"""Tests for the shared helpers in ``fusion_bridge/common``."""

import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge.common import api, app_log, args, geometry, results


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


class PredicateTests(unittest.TestCase):
    def test_is_int(self):
        self.assertTrue(args.is_int(0))
        for value in (True, 1.0, "1", None):
            self.assertFalse(args.is_int(value))

    def test_int_or_none(self):
        self.assertEqual(args.int_or_none(3), 3)
        self.assertIsNone(args.int_or_none(True))
        self.assertIsNone(args.int_or_none("3"))

    def test_is_number_allows_non_finite(self):
        self.assertTrue(args.is_number(1.5))
        self.assertTrue(args.is_number(float("nan")))
        self.assertFalse(args.is_number(False))

    def test_is_finite_number(self):
        self.assertTrue(args.is_finite_number(2))
        for value in (float("nan"), float("inf"), True, "1", None, 10 ** 400):
            self.assertFalse(args.is_finite_number(value))


class ArgumentCheckTests(unittest.TestCase):
    def test_require_object(self):
        args.require_object({})
        with self.assertRaisesRegex(ValueError, "^arguments must be an object$"):
            args.require_object([])

    def test_unknown_message_sorts_and_keeps_the_label(self):
        self.assertIsNone(args.unknown_message({"a": 1}, {"a"}))
        self.assertEqual(args.unknown_message({"b": 1, "a": 2}, ()), "unknown argument(s): a, b")
        self.assertEqual(args.unknown_message({"a": 1}, (), args.UNKNOWN_CAPITALIZED), "Unknown argument(s): a")

    def test_reject_unknown(self):
        args.reject_unknown({"a": 1}, ("a", "b"))
        with self.assertRaisesRegex(ValueError, "^unknown argument"):
            args.reject_unknown({"c": 1}, ("a",))

    def test_bool_arg(self):
        self.assertTrue(args.bool_arg({}, "k", True))
        self.assertFalse(args.bool_arg({"k": False}, "k", True))
        with self.assertRaisesRegex(ValueError, "^'k' must be a boolean$"):
            args.bool_arg({"k": 1}, "k")

    def test_int_arg_bounds_are_inclusive(self):
        self.assertEqual(args.int_arg({"k": 5}, "k", 1, 1, 5), 5)
        self.assertEqual(args.int_arg({}, "k", 1, 1, 5), 1)
        for bad in (0, 6, True, 1.0, "2", None):
            with self.assertRaisesRegex(ValueError, "^'k' must be an integer from 1 to 5$"):
                args.int_arg({"k": bad}, "k", 1, 1, 5)

    def test_text_arg_absent(self):
        self.assertIsNone(args.text_arg({}, "k"))
        with self.assertRaisesRegex(ValueError, "^'k' is required$"):
            args.text_arg({}, "k", required=True)

    def test_text_arg_blank(self):
        for bad in ("", "  ", 3):
            with self.assertRaisesRegex(ValueError, "^'k' must be a non-empty string$"):
                args.text_arg({"k": bad}, "k")
        self.assertEqual(args.text_arg({"k": ""}, "k", blank_ok=True), "")
        with self.assertRaises(ValueError):
            args.text_arg({"k": 3}, "k", blank_ok=True)

    def test_text_arg_strip_and_suffix(self):
        self.assertEqual(args.text_arg({"k": " a "}, "k"), " a ")
        self.assertEqual(args.text_arg({"k": " a "}, "k", strip=True), "a")
        with self.assertRaisesRegex(ValueError, r"string \(id\)$"):
            args.text_arg({"k": ""}, "k", suffix=" (id)")

    def test_error_pair(self):
        @args.error_pair
        def check(value):
            if value < 0:
                raise ValueError("negative")
            return value

        self.assertEqual(check(1), (1, None))
        self.assertEqual(check(-1), (None, "Error: negative"))

    def test_error_pair_lets_other_exceptions_through(self):
        @args.error_pair
        def check():
            raise KeyError("bug")

        with self.assertRaises(KeyError):
            check()


class VectorTests(unittest.TestCase):
    def test_xyz(self):
        point = type("P", (), {"x": 1.0, "y": 2.0, "z": 3.0})()
        self.assertEqual(geometry.xyz(point), (1.0, 2.0, 3.0))

    def test_arithmetic(self):
        self.assertEqual(geometry.add((1, 2, 3), (1, 1, 1)), (2, 3, 4))
        self.assertEqual(geometry.sub((1, 2, 3), (1, 1, 1)), (0, 1, 2))
        self.assertEqual(geometry.dot((1, 2, 3), (4, 5, 6)), 32)
        self.assertEqual(geometry.scaled((1, 2, 3), 2), (2, 4, 6))
        self.assertEqual(geometry.along((1, 0, 0), (0, 1, 0), 2), (1, 2, 0))

    def test_cross_is_right_handed(self):
        self.assertEqual(geometry.cross((1, 0, 0), (0, 1, 0)), (0, 0, 1))

    def test_unit(self):
        self.assertEqual(geometry.unit((0.0, 3.0, 4.0)), (0.0, 0.6, 0.8))
        self.assertIsNone(geometry.unit((0.0, 0.0, 0.0)))
        self.assertIsNone(geometry.unit((1e-13, 0.0, 0.0)))


class UnitTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(geometry.unit_labels("mm"), {"length": "mm", "area": "mm^2", "volume": "mm^3"})

    def test_to_units_powers(self):
        self.assertEqual(geometry.to_units(2.54, "in"), 1.0)
        self.assertEqual(geometry.to_units(1.0, "mm", 2), 100.0)
        self.assertEqual(geometry.to_units(1.0, "mm", 3), 1000.0)

    def test_to_units_rounds_and_passes_none(self):
        self.assertEqual(geometry.to_units(1.0, "in"), 0.3937)
        self.assertEqual(geometry.to_units(1.0, "in", digits=6), 0.393701)
        self.assertIsNone(geometry.to_units(None, "in"))

    def test_round_trip(self):
        self.assertAlmostEqual(geometry.to_cm(geometry.to_units(5.0, "mm"), "mm"), 5.0)
        self.assertAlmostEqual(geometry.to_cm(100.0, "mm", 2), 1.0)

    def test_from_cm_has_no_negative_zero(self):
        self.assertEqual(geometry.from_cm(2.54, "in"), 1.0)
        self.assertEqual(str(geometry.from_cm(-0.0, "cm")), "0.0")

    def test_factor_tables_agree(self):
        for units, factor in geometry.UNIT_FACTORS.items():
            self.assertAlmostEqual(factor * geometry.CM_PER_UNIT[units], 1.0)


class LogLocationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.logs = os.path.join(self.root, "user", "logs")
        os.makedirs(self.logs)

    def write(self, name, mtime, text="x"):
        path = os.path.join(self.logs, name)
        with open(path, "w") as handle:
            handle.write(text)
        os.utime(path, (mtime, mtime))
        return path

    def test_newest_by_mtime_not_name(self):
        new = self.write("AppLogFile1.log", 200)
        old = self.write("AppLogFile2.log", 100)
        self.assertEqual(app_log.newest_log([self.root]), new)
        self.assertEqual([path for _, _, path in app_log.find_log_files([self.root])], [new, old])

    def test_equal_mtimes_pick_the_later_session(self):
        self.write("AppLogFile20261104T100000.log", 100)
        later = self.write("AppLogFile20261105T100000.log", 100)
        self.assertEqual(app_log.newest_log([self.root]), later)

    def test_nothing_found(self):
        self.assertIsNone(app_log.newest_log([self.root]))
        self.assertIsNone(app_log.newest_log([os.path.join(self.root, "missing")]))
        self.assertIsNone(app_log.newest_log([]))

    def test_other_files_are_ignored(self):
        self.write("Other.log", 100)
        self.assertEqual(app_log.find_log_files([self.root]), [])

    def test_root_with_glob_characters_is_taken_literally(self):
        root = os.path.join(self.root, "a[1]")
        os.makedirs(os.path.join(root, "u", "logs"))
        path = os.path.join(root, "u", "logs", "AppLogFile1.log")
        open(path, "w").close()
        self.assertEqual(app_log.newest_log([root]), path)

    def test_default_roots_cover_mac_and_windows(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/Local"}):
            roots = app_log.default_log_roots()
        self.assertIn(os.path.join("Library", "Application Support"), roots[0])
        self.assertEqual(roots[1], os.path.join("C:/Local", "Autodesk", "Autodesk Fusion 360"))

    def test_default_roots_without_localappdata(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("LOCALAPPDATA", None)
            self.assertEqual(len(app_log.default_log_roots()), 1)

    def test_tail_drops_the_partial_first_line(self):
        path = self.write("AppLogFile1.log", 100, "aaaa\nbbbb\ncccc\n")
        self.assertEqual(app_log.read_tail_lines(path, 7), ["cccc"])
        self.assertEqual(app_log.read_tail_lines(path), ["aaaa", "bbbb", "cccc"])


FAILED = ('20261105T113355 E .1 POST 423 https://x/entities/urn%3Aa%3Aco.ABC/rename?n=1 requestId:r1 - z\n'
          '  Request Body: [Empty body]\n'
          '  Response Body: {"errorCode":"LOCK_RESOURCE_IS_LOCKED","message":"locked"}\n')


class LogParsingTests(unittest.TestCase):
    def test_parse_header(self):
        self.assertEqual(app_log.parse_header("20261104T170849 I 245 [CFS] Start"),
                         ("20261104T170849", "I", "245", "[CFS] Start"))
        self.assertEqual(app_log.parse_header("20261104T170849 I 245"), ("20261104T170849", "I", "245", ""))
        self.assertIsNone(app_log.parse_header("Response Body: {}"))
        self.assertIsNone(app_log.parse_header(""))

    def test_entry_time(self):
        self.assertEqual(app_log.entry_time("20261104T170849"), datetime(2026, 11, 4, 17, 8, 49))
        self.assertIsNone(app_log.entry_time("20261399T999999"))

    def test_parse_failures(self):
        entry = app_log.parse_failures(FAILED)[0]
        self.assertEqual((entry["status"], entry["request_id"], entry["request_body"]), (423, "r1", "[Empty body]"))
        self.assertIn("LOCK_RESOURCE", entry["response_body"])

    def test_gets_and_successes_are_not_failures(self):
        self.assertEqual(app_log.parse_failures("20261105T113355 E .1 GET 404 https://x\n"), [])
        self.assertEqual(app_log.parse_failures("20261105T113355 I .1 POST 200 https://x\n"), [])

    def test_a_body_under_a_success_is_not_given_to_the_next_failure(self):
        text = "20261105T113355 I .1 POST 200 https://x\nResponse Body: fine\n" + FAILED
        self.assertEqual(len(app_log.parse_failures(text)), 1)
        self.assertIn("LOCK", app_log.parse_failures(text)[0]["response_body"])

    def test_failure_reason_reads_json(self):
        reason = app_log.failure_reason(app_log.parse_failures(FAILED)[0])
        self.assertEqual(reason, {"status": 423, "error_code": "LOCK_RESOURCE_IS_LOCKED",
                                  "message": "locked", "request_id": "r1"})

    def test_failure_reason_with_a_plain_or_missing_body(self):
        entry = {"status": 500, "request_id": None, "response_body": "x" * 900}
        self.assertEqual(len(app_log.failure_reason(entry)["message"]), 500)
        self.assertIsNone(app_log.failure_reason(dict(entry, response_body=None))["message"])
        self.assertIsNone(app_log.failure_reason(dict(entry, response_body="[1]"))["error_code"])

    def test_server_reason_only_names_the_resource(self):
        self.assertEqual(app_log.server_reason(FAILED, "urn:a:co.ABC")["status"], 423)
        self.assertIsNone(app_log.server_reason(FAILED, "urn:a:co.OTHER"))

    def test_resource_token_joins_lineage_and_version_ids(self):
        self.assertEqual(app_log.resource_token("urn:a:vf.ABC?version=2"), "ABC")
        self.assertEqual(app_log.resource_token("urn:a:ABC"), "ABC")

    def test_poll_reason_stops_at_the_first_answer(self):
        answers = iter([None, None, "found", "late"])
        naps = []
        self.assertEqual(app_log.poll_reason(lambda: next(answers), 6, naps.append), "found")
        self.assertEqual(len(naps), 2)

    def test_poll_reason_gives_up(self):
        naps = []
        self.assertIsNone(app_log.poll_reason(lambda: None, 3, naps.append))
        self.assertEqual(len(naps), 2)

    def test_server_log_without_a_path_reads_nothing(self):
        log = app_log.ServerLog()
        self.assertIsNone(log.mark())
        self.assertEqual(log.since(None), "")
        self.assertIsNone(log.reason(None, "urn:a:b"))


if __name__ == "__main__":
    unittest.main()
