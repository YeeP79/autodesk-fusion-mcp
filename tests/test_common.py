"""Tests for the shared helpers in ``fusion_bridge/common``."""

import ast
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge.common import api, app_log, args, bodies, cloud, documents, geometry, results, timeline


class PlaneGeometryTests(unittest.TestCase):
    def assert_close(self, a, b):
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, places=9)

    def test_perpendicular_pair_on_axes_and_oblique(self):
        for normal in ((0, 0, 1), (0, 0, -2), (1, 0, 0), (0, 1, 0), (1, 1, 1), (0, 0.5, 0.866), (-3, 2, 0.1)):
            with self.subTest(normal=normal):
                first, second = geometry.perpendicular_pair(normal)
                axis = geometry.unit(normal)
                self.assertAlmostEqual(geometry.dot(first, first), 1.0)
                self.assertAlmostEqual(geometry.dot(second, second), 1.0)
                self.assertAlmostEqual(geometry.dot(first, second), 0.0)
                self.assertAlmostEqual(geometry.dot(first, axis), 0.0)
                self.assert_close(geometry.cross(first, second), axis)

    def test_perpendicular_pair_zero(self):
        self.assertIsNone(geometry.perpendicular_pair((0, 0, 0)))

    def test_rotate_about(self):
        z = (0.0, 0.0, 1.0)
        self.assert_close(geometry.rotate_about((1.0, 0.0, 0.0), z, 0), (1, 0, 0))
        self.assert_close(geometry.rotate_about((1.0, 0.0, 0.0), z, 90), (0, 1, 0))
        self.assert_close(geometry.rotate_about((1.0, 0.0, 0.0), z, 180), (-1, 0, 0))
        self.assert_close(geometry.rotate_about((1.0, 0.0, 0.0), z, -90), (0, -1, 0))

    def test_rotate_about_leaves_the_axis_alone(self):
        self.assert_close(geometry.rotate_about((0.0, 0.0, 2.0), (0.0, 0.0, 1.0), 37), (0, 0, 2))

    def test_collinear_threshold(self):
        a, b = (0, 0, 0), (10, 0, 0)
        self.assertTrue(geometry.collinear(a, b, (20, 0, 0)))
        self.assertTrue(geometry.collinear(a, b, (20, 1e-6, 0)))
        self.assertFalse(geometry.collinear(a, b, (20, 1e-3, 0)))

    def test_collinear_ignores_size(self):
        self.assertFalse(geometry.collinear((0, 0, 0), (1e-4, 0, 0), (0, 1e-4, 0)))

    def test_coincident_points_are_collinear(self):
        self.assertTrue(geometry.collinear((1, 1, 1), (1, 1, 1), (2, 0, 0)))
        self.assertTrue(geometry.collinear((1, 1, 1), (2, 0, 0), (1, 1, 1)))

    def test_same_plane(self):
        up = (0, 0, 1)
        self.assertTrue(geometry.same_plane((0, 0, 1), up, (5, -3, 1), up))
        self.assertTrue(geometry.same_plane((0, 0, 1), up, (5, -3, 1), (0, 0, -1)))
        self.assertFalse(geometry.same_plane((0, 0, 1), up, (0, 0, 1.01), up))
        self.assertFalse(geometry.same_plane((0, 0, 0), up, (0, 0, 0), (0, 0.01, 1)))
        self.assertFalse(geometry.same_plane((0, 0, 0), (0, 0, 0), (0, 0, 0), up))


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

    def test_from_local_undoes_to_local(self):
        frame = ((1.0, 2.0, 3.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        back = geometry.from_local(geometry.to_local((4.0, -5.0, 6.0), frame), frame)
        self.assertEqual(tuple(round(c, 9) for c in back), (4.0, -5.0, 6.0))

    def test_a_direction_turns_with_a_frame_but_does_not_move(self):
        frame = ((1.0, 2.0, 3.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        self.assertEqual(geometry.direction_from_local((1.0, 0.0, 0.0), frame), (0.0, 1.0, 0.0))

    def test_to_local_undoes_a_moved_and_turned_frame(self):
        frame = ((10, 0, 0), (0, 1, 0), (-1, 0, 0), (0, 0, 1))  # A quarter turn about z, then +10 in x.
        self.assertEqual(geometry.to_local((10, 2, 5), frame), (2, 0, 5))
        self.assertEqual(geometry.to_local((9, 0, 0), frame), (0, 1, 0))
        self.assertEqual(geometry.to_local((10, 0, 0), frame), (0, 0, 0))

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


class FakeItem:
    def __init__(self, name, state):
        self.name = name
        self.healthState = state


class Enum:
    HealthyFeatureHealthState = 10
    WarningFeatureHealthState = 11
    ErrorFeatureHealthState = 12
    UnknownFeatureHealthState = 13


class TimelineTests(unittest.TestCase):
    def enum(self):
        return mock.patch.object(timeline.adsk.fusion, "FeatureHealthStates", Enum, create=True)

    def test_health_states_read_the_enum(self):
        with self.enum():
            self.assertEqual(timeline.health_states(), {"healthy": 10, "warning": 11, "error": 12, "unknown": 13})

    def test_health_states_fall_back_without_the_enum(self):
        self.assertEqual(timeline.health_states(), timeline.DEFAULT_STATES)

    def test_state_name(self):
        self.assertEqual(timeline.state_name(2), "error")
        self.assertEqual(timeline.state_name(12, {"error": 12}), "error")
        self.assertEqual(timeline.state_name(99), "unknown")
        self.assertEqual(timeline.state_name(None), "unknown")

    def test_health_of(self):
        with self.enum():
            self.assertEqual(timeline.health_of(FakeItem("a", 11)), "warning")
            self.assertEqual(timeline.health_of(Raises()), "unknown")

    def test_health_counts(self):
        design = type("D", (), {"timeline": [FakeItem("a", 10), FakeItem("b", 11), FakeItem("c", 12),
                                             FakeItem("d", 13)]})()
        with self.enum():
            self.assertEqual(timeline.health_counts(design),
                             ({"healthy": 1, "warning": 2, "error": 1}, ["c"]))


class Fake:
    """An object with the given attributes."""

    def __init__(self, **attributes):
        self.__dict__.update(attributes)


def fake_body(name, visible=True):
    return Fake(name=name, isVisible=visible)


class BodyEntryTests(unittest.TestCase):
    def tree(self):
        occurrence = Fake(fullPathName="Arm:1", isVisible=False, isReferencedComponent=True,
                          component=Fake(name="Arm"), bRepBodies=Indexed([fake_body("A"), fake_body("B", False)]))
        return Fake(name="Root", bRepBodies=Indexed([fake_body("R")]), allOccurrences=Indexed([occurrence]))

    def test_root_bodies_then_occurrences(self):
        entries = list(bodies.iter_body_entries(self.tree()))
        self.assertEqual([(e["occurrence_path"], e["body"], e["index"]) for e in entries],
                         [("root", "R", 0), ("Arm:1", "A", 0), ("Arm:1", "B", 1)])

    def test_root_entry(self):
        entry = next(bodies.iter_body_entries(self.tree()))
        self.assertEqual((entry["component"], entry["linked"], entry["occurrence_visible"]), ("Root", False, None))

    def test_occurrence_entry(self):
        entry = list(bodies.iter_body_entries(self.tree()))[1]
        self.assertEqual((entry["component"], entry["linked"], entry["occurrence_visible"]), ("Arm", True, False))

    def test_design_with_nothing_readable(self):
        self.assertEqual(list(bodies.iter_body_entries(Raises())), [])

    def test_is_hidden(self):
        self.assertFalse(bodies.is_hidden({"visible": True, "occurrence_visible": None}))
        self.assertFalse(bodies.is_hidden({"visible": None, "occurrence_visible": True}))
        self.assertTrue(bodies.is_hidden({"visible": False, "occurrence_visible": True}))
        self.assertTrue(bodies.is_hidden({"visible": True, "occurrence_visible": False}))

    def test_bbox(self):
        low, high = Fake(x=0, y=1, z=2), Fake(x=3, y=4, z=5)
        self.assertEqual(bodies.bbox(Fake(boundingBox=Fake(minPoint=low, maxPoint=high))), ((0, 1, 2), (3, 4, 5)))
        self.assertIsNone(bodies.bbox(Fake()))

    def test_native(self):
        target = Fake()
        self.assertIs(bodies.native(Fake(nativeObject=target)), target)
        plain = Fake(nativeObject=None)
        self.assertIs(bodies.native(plain), plain)
        unreadable = Raises()
        self.assertIs(bodies.native(unreadable), unreadable)

    def test_native_keeps_a_falsy_object(self):
        class Falsy:
            def __bool__(self):
                return False

        target = Falsy()
        self.assertIs(bodies.native(Fake(nativeObject=target)), target)

    def test_bbox_shift(self):
        box = ((0, 0, 0), (1, 1, 1))
        self.assertEqual(bodies.bbox_shift(box, box), 0)
        self.assertEqual(bodies.bbox_shift(box, ((0, 0, 0), (1, 3, 1))), 2)
        self.assertIsNone(bodies.bbox_shift(None, box))

    def test_parse_body_ref(self):
        self.assertEqual(bodies.parse_body_ref({"occurrence_path": "root", "body_name": "B"}, "'a'"), ("root", "B", None))
        self.assertEqual(bodies.parse_body_ref({"occurrence_path": "root", "body_name": "B", "body_index": 2}, "'a'"),
                         ("root", "B", 2))

    def test_parse_body_ref_refusals(self):
        for value in ("B", {"occurrence_path": "root"}, {"occurrence_path": " ", "body_name": "B"},
                      {"occurrence_path": "root", "body_name": "B", "extra": 1},
                      {"occurrence_path": "root", "body_name": "B", "body_index": -1},
                      {"occurrence_path": "root", "body_name": "B", "body_index": True}):
            with self.assertRaises(ValueError):
                bodies.parse_body_ref(value, "'a'")


def box_of(low, high):
    return Fake(minPoint=Fake(x=low[0], y=low[1], z=low[2]), maxPoint=Fake(x=high[0], y=high[1], z=high[2]))


class TempCopyTests(unittest.TestCase):
    """The copy of a proxy may land in component space (live check CM-2)."""

    class Manager:
        def __init__(self, copy, moved_to=None):
            self.result, self.moved_to, self.transforms = copy, moved_to, []

        def copy(self, body):
            return self.result

        def transform(self, body, matrix):
            self.transforms.append(matrix)
            if self.moved_to is None:
                return False
            body.boundingBox = self.moved_to
            return True

    def world(self, context=None):
        return Fake(name="B", boundingBox=box_of((10, 0, 0), (11, 1, 1)), assemblyContext=context)

    def test_a_copy_already_in_place_is_left_alone(self):
        copy = Fake(boundingBox=box_of((10, 0, 0), (11, 1, 1)))
        manager = self.Manager(copy)
        self.assertIs(bodies.temp_copy(manager, self.world()), copy)
        self.assertEqual(manager.transforms, [])

    def test_a_copy_in_component_space_is_moved_by_the_occurrence(self):
        copy = Fake(boundingBox=box_of((0, 0, 0), (1, 1, 1)))
        manager = self.Manager(copy, moved_to=box_of((10, 0, 0), (11, 1, 1)))
        body = self.world(Fake(transform2="matrix"))
        self.assertIs(bodies.temp_copy(manager, body), copy)
        self.assertEqual(manager.transforms, ["matrix"])

    def test_a_copy_that_cannot_be_placed_raises(self):
        misplaced = box_of((0, 0, 0), (1, 1, 1))
        for manager, body in ((self.Manager(Fake(boundingBox=misplaced)), self.world()),
                              (self.Manager(Fake(boundingBox=misplaced)), self.world(Fake(transform2="m"))),
                              (self.Manager(Fake(boundingBox=misplaced), moved_to=misplaced),
                               self.world(Fake(transform2="m")))):
            with self.assertRaises(RuntimeError):
                bodies.temp_copy(manager, body)

    def test_no_copy_raises(self):
        with self.assertRaises(RuntimeError):
            bodies.temp_copy(self.Manager(None), self.world())


def fake_file(name, file_id, extension="f3d", version=1):
    return Fake(name=name, id=file_id, fileExtension=extension, versionNumber=version, parentProject=None)


def fake_folder(name, folder_id="f", files=(), folders=(), is_root=False):
    folder = Fake(name=name, id=folder_id, dataFiles=Indexed(list(files)), dataFolders=Indexed(list(folders)),
                  isRoot=is_root, parentProject=None, parentFolder=None)
    for child in folders:
        child.parentFolder = folder
    for child in files:
        child.parentFolder = folder
    return folder


def pick_first_named(objects, name, label):
    return [o for o in objects if o.name == name][0]


class CloudTests(unittest.TestCase):
    def test_items_prefers_as_array(self):
        class Listing(Indexed):
            def asArray(self):
                return ["from array"]

        self.assertEqual(cloud.items(Listing(["from item"])), ["from array"])

    def test_items_without_as_array(self):
        self.assertEqual(cloud.items(Indexed([1])), [1])
        self.assertEqual(cloud.items(Vector([1])), [1])
        self.assertEqual(cloud.items(None), [])

    def test_path_segments(self):
        self.assertEqual(cloud.path_segments(" a / b\\c// "), ["a", "b", "c"])
        self.assertEqual(cloud.path_segments("/"), [])

    def test_split_path_names_what_it_read(self):
        self.assertEqual(cloud.split_path(None), [])
        with self.assertRaisesRegex(ValueError, "^folder path may not contain '..'"):
            cloud.split_path("a/..")
        with self.assertRaisesRegex(ValueError, "^path may not contain '.'"):
            cloud.split_path("a/.", "path")
        with self.assertRaisesRegex(ValueError, "^path must be a string"):
            cloud.split_path(3, "path")

    def test_ambiguous_folder_is_a_lookup_error(self):
        root = fake_folder("root", folders=[fake_folder("Tubes", "1"), fake_folder("tubes", "2")])
        with self.assertRaises(cloud.AmbiguousFolder):
            cloud.find_child_folder(root, "TUBES")
        with self.assertRaises(LookupError):
            cloud.find_child_folder(root, "TUBES")

    def test_missing_folder_is_not_ambiguous(self):
        with self.assertRaises(LookupError) as caught:
            cloud.find_child_folder(fake_folder("root"), "Nope")
        self.assertNotIsInstance(caught.exception, cloud.AmbiguousFolder)

    def test_resolve_project_by_id_then_match(self):
        projects = [Fake(id="1", name="A"), Fake(id="2", name="1")]
        self.assertEqual(cloud.resolve_project(projects, "1", pick_first_named).name, "A")
        self.assertEqual(cloud.resolve_project(projects, "A", pick_first_named).id, "1")

    def test_resolve_project_survives_an_unreadable_id(self):
        project = Raises()
        project.name = "A"
        self.assertIs(cloud.resolve_project([project], "A", pick_first_named), project)

    def test_resolve_path_walks_folders_then_the_file(self):
        target = fake_file("Knuckle", "urn:k")
        root = fake_folder("root", folders=[fake_folder("Sub", "1", files=[target])])
        self.assertIs(cloud.resolve_path(root, ["Sub", "Knuckle"], pick_first_named), target)

    def test_project_arg(self):
        self.assertEqual(cloud.project_arg({"project": " Car "}), "Car")
        for bad in ({}, {"project": ""}, {"project": 3}):
            with self.assertRaisesRegex(ValueError, r"^'project' must be a non-empty string \(project name or id\)$"):
                cloud.project_arg(bad)

    def test_problem(self):
        self.assertEqual(cloud.problem("x", "why", file="f"), {"problem": "x", "message": "why", "file": "f"})


class ResolveFileTests(unittest.TestCase):
    def setUp(self):
        self.knuckle = fake_file("Knuckle", "urn:1")
        self.twin = fake_file("Knuckle", "urn:2", "f2d")
        self.root = fake_folder("root", "r", files=[self.knuckle, self.twin, fake_file("Arm", "urn:3")], is_root=True)
        self.root.parentProject = Fake(id="p", name="Car")
        self.project = Fake(id="p", name="Car", rootFolder=self.root)
        self.resolver = cloud.PathResolver()

    def resolve(self, spec, **kwargs):
        return cloud.resolve_file(self.project, spec, lambda file_id: None, self.resolver, **kwargs)

    def refusal(self, spec, **kwargs):
        with self.assertRaises(cloud.Refusal) as caught:
            self.resolve(spec, **kwargs)
        return caught.exception.problem

    def test_by_path_is_case_insensitive(self):
        self.assertEqual(self.resolve("ARM").id, "urn:3")

    def test_by_path_with_extension(self):
        self.assertEqual(self.resolve("Knuckle.f2d").id, "urn:2")

    def test_ambiguous_lists_candidates(self):
        found = self.refusal("Knuckle")
        self.assertEqual(found["problem"], "ambiguous")
        self.assertEqual(found["candidates"][0], {"path": "Car/Knuckle", "version": 1, "id": "urn:1"})

    def test_candidates_use_the_callers_rows(self):
        found = self.refusal("Knuckle", describe=lambda data_file, resolver: data_file.id)
        self.assertEqual(found["candidates"], ["urn:1", "urn:2"])

    def test_missing_file_names_the_folder_path(self):
        found = self.refusal("Nope")
        self.assertEqual(found["problem"], "not_found")
        self.assertIn("in 'Car'", found["message"])

    def test_ambiguous_folder_is_reported_as_ambiguous(self):
        self.root.dataFolders = Indexed([fake_folder("Sub", "1"), fake_folder("sub", "2")])
        self.assertEqual(self.refusal("SUB/x")["problem"], "ambiguous")

    def test_bad_paths(self):
        self.assertEqual(self.refusal("/")["problem"], "bad_path")
        self.assertEqual(self.refusal("a/../b")["problem"], "bad_path")

    def test_id_not_found(self):
        self.assertEqual(self.refusal("urn:none")["problem"], "not_found")

    def test_id_lookup_that_raises(self):
        def boom(file_id):
            raise RuntimeError("offline")

        with self.assertRaises(cloud.Refusal) as caught:
            cloud.resolve_file(self.project, "urn:1", boom, self.resolver)
        self.assertIn("offline", caught.exception.problem["message"])

    def test_id_in_another_project(self):
        other = Fake(parentProject=Fake(id="q", name="Bike"))
        with self.assertRaises(cloud.Refusal) as caught:
            cloud.resolve_file(self.project, "urn:9", lambda file_id: other, self.resolver)
        self.assertEqual(caught.exception.problem["problem"], "wrong_project")


class OpenFileIdsTests(unittest.TestCase):
    def test_saved_unsaved_and_unreadable(self):
        class Broken:
            name = "Broken"

            @property
            def dataFile(self):
                raise RuntimeError("no file")

        saved = Fake(name="Arm", isVisible=False, dataFile=Fake(id="urn:1"))
        opened, unreadable = cloud.open_file_ids([saved, Fake(name="New", dataFile=None), Broken()])
        self.assertEqual(opened, {"urn:1": {"document": "Arm", "visible": False}})
        self.assertEqual(unreadable, ["Broken"])


class DocumentTests(unittest.TestCase):
    def test_open_documents(self):
        self.assertEqual(documents.open_documents(Fake(documents=Indexed(["a", "b"]))), ["a", "b"])

    def test_open_documents_raises_rather_than_drop_one(self):
        with self.assertRaises(RuntimeError):
            documents.open_documents(Fake(documents=Indexed(["a"], bad={0})))

    def test_data_file_id(self):
        self.assertEqual(documents.data_file_id({"data_file": {"id": "x"}}), "x")
        self.assertIsNone(documents.data_file_id({"data_file": None}))
        self.assertIsNone(documents.data_file_id({}))

    def test_instances_of(self):
        infos = [{"data_file": {"id": "a"}}, {"data_file": None}, {"data_file": {"id": "a"}}]
        self.assertEqual(documents.instances_of(infos, "a"), [0, 2])
        self.assertEqual(documents.instances_of(infos, "b"), [])

    def test_unsaved_documents_are_not_instances_of_each_other(self):
        infos = [{"data_file": None}, {"data_file": None}]
        self.assertEqual(documents.instances_of(infos, None), [])
        self.assertEqual(documents.instances_of(infos, ""), [])

    def test_instances_of_with_a_flat_snapshot(self):
        infos = [{"file_id": "a"}, {"file_id": "b"}]
        self.assertEqual(documents.instances_of(infos, "b", key=lambda info: info["file_id"]), [1])

    def test_pump_runs_events_and_times_them(self):
        ticks = iter([10.0, 12.5])
        clock = type("Clock", (), {"monotonic": staticmethod(lambda: next(ticks))})
        calls = []
        with mock.patch.object(documents, "time", clock), \
                mock.patch.object(documents.adsk, "doEvents", lambda: calls.append(1), create=True):
            self.assertEqual(documents.pump(), 2.5)
        self.assertEqual(calls, [1])


class LayerTests(unittest.TestCase):
    """The Fusion-free modules must stay importable without ``adsk``."""

    def imports(self, name):
        path = os.path.join(os.path.dirname(results.__file__), f"{name}.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add("." * node.level + (node.module or ""))
        return found

    def test_fusion_free_modules(self):
        allowed = {".api", ".geometry"}
        for name in ("results", "args", "api", "geometry", "cloud", "app_log"):
            with self.subTest(module=name):
                found = self.imports(name)
                self.assertNotIn("adsk", found)
                self.assertEqual({item for item in found if item.startswith(".")} - allowed, set())


if __name__ == "__main__":
    unittest.main()
