"""apply_thread: schema, argument validation, size and class logic, and catalog
narrowing against a fake library shaped like Fusion's (no Fusion needed)."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import apply_thread as at
from fusion_bridge import tool_surface

UNIFIED = "ANSI Unified Screw Threads"
ISO = "ISO Metric profile"
ACME = "ACME Screw Threads"
WOOD = "DIN Wood Screw Thread"
TAPPING = "Inch Tapping Threads"
NPT = "ANSI Taper Pipe Threads"

UNIFIED_SIZES = ["0.086(#2)", "0.138(#6)", "0.19(#10)", "0.216(#12)", "0.25", "0.3125", "0.5", "1.0", "1.125"]
ISO_SIZES = ["2.0", "2.5", "6.0", "10.0"]


class Query:
    """A ThreadDataQuery holding a few rows, counting what it is asked."""

    allThreadTypes = [ACME, UNIFIED, WOOD, ISO, TAPPING]
    defaultInchThreadType = UNIFIED
    defaultMetricThreadType = ISO
    SIZES = {UNIFIED: UNIFIED_SIZES, ISO: ISO_SIZES, ACME: ["1/4", "5/16"], WOOD: ["2", "3"],
             TAPPING: ["0.086", "0.112"]}
    UNITS = {UNIFIED: "in", ISO: "mm", ACME: "in", WOOD: "mm", TAPPING: "in"}
    DESIGNATIONS = {(UNIFIED, "0.25"): ["1/4-20 UNC", "1/4-28 UNF"], (UNIFIED, "0.19(#10)"): ["10-24 UNC", "10-32 UNF"],
                    (ISO, "6.0"): ["M6x1", "M6x0.75"], (WOOD, "2"): ["2x0.9"]}
    CLASSES = {("1/4-20 UNC", False): ["1A", "2A", "3A"], ("1/4-20 UNC", True): ["1B", "2B", "3B"],
               ("M6x1", False): ["4g6g", "6g"], ("M6x1", True): ["6H"], ("2x0.9", False): ["h15"]}

    def __init__(self):
        self.calls = []
        self.recommendation = (True, "12-24 UNC", "3A")

    def allSizes(self, family):
        self.calls.append(("sizes", family))
        return self.SIZES.get(family, [])

    def threadTypeUnit(self, family):
        return self.UNITS.get(family, "")

    def allDesignations(self, family, size):
        self.calls.append(("designations", family, size))
        if size not in self.SIZES.get(family, []):
            raise RuntimeError("3 : cannot get all the designations for your input")
        return self.DESIGNATIONS.get((family, size), [])

    def allClasses(self, internal, family, designation):
        return self.CLASSES.get((designation, internal), [])

    def recommendThreadData(self, diameter, internal, family):
        self.calls.append(("recommend", diameter, internal, family))
        return self.recommendation


class Tapered:
    allThreadTypes = [NPT]


class Info:
    def __init__(self, major, minor, pitch):
        self.majorDiameter, self.minorDiameter, self.threadPitch = major, minor, pitch


def make_info(internal, family, designation, cls):
    if cls == "3A":
        raise RuntimeError("no data")
    return Info(0.632, 0.508, 0.127)


def options(**arguments):
    opts, err = at.validate_args({"action": "catalog", **arguments})
    assert err is None, err
    return opts


class SizeTests(unittest.TestCase):
    def test_table(self):
        rows = [
            ("1/4", UNIFIED_SIZES, "in", "0.25"),
            ("5/16", UNIFIED_SIZES, "in", "0.3125"),
            ("1 1/8", UNIFIED_SIZES, "in", "1.125"),
            ("1-1/8", UNIFIED_SIZES, "in", "1.125"),
            ("#10", UNIFIED_SIZES, "in", "0.19(#10)"),
            ("# 6", UNIFIED_SIZES, "in", "0.138(#6)"),
            ("0.19", UNIFIED_SIZES, "in", "0.19(#10)"),
            (".25", UNIFIED_SIZES, "in", "0.25"),
            ("0.250", UNIFIED_SIZES, "in", "0.25"),
            ('1/4"', UNIFIED_SIZES, "in", "0.25"),
            ("0.25 in", UNIFIED_SIZES, "in", "0.25"),
            ("6.35 mm", UNIFIED_SIZES, "in", "0.25"),
            ("1", UNIFIED_SIZES, "in", "1.0"),
            ("6 mm", ISO_SIZES, "mm", "6.0"),
            ("6mm", ISO_SIZES, "mm", "6.0"),
            ("6", ISO_SIZES, "mm", "6.0"),
            ("2.5", ISO_SIZES, "mm", "2.5"),
            ("10 MM", ISO_SIZES, "mm", "10.0"),
        ]
        for value, sizes, unit, expected in rows:
            self.assertEqual(at.normalize_size(value, sizes, unit), expected, value)

    def test_misses(self):
        for value, sizes, unit in (("7/32", UNIFIED_SIZES, "in"), ("6 mm", UNIFIED_SIZES, "in"),
                                   ("#99", UNIFIED_SIZES, "in"), ("M6", ISO_SIZES, "mm"), ("", ISO_SIZES, "mm"),
                                   ("1/0", UNIFIED_SIZES, "in"), ("nan", ISO_SIZES, "mm"), ("1/4", ISO_SIZES, "mm")):
            self.assertIsNone(at.normalize_size(value, sizes, unit), value)

    def test_an_exact_spelling_wins_over_arithmetic(self):
        self.assertEqual(at.normalize_size("1/4", ["1/4", "5/16"], "in"), "1/4")

    def test_a_family_that_keeps_fractions_also_answers_decimals(self):
        self.assertEqual(at.normalize_size("0.25", ["1/4", "5/16"], "in"), "1/4")

    def test_bare_decimal_gauge_family(self):
        self.assertEqual(at.normalize_size("#2", ["0.086", "0.112"], "in"), "0.086")

    def test_two_sizes_with_one_number_are_not_guessed_between(self):
        self.assertIsNone(at.normalize_size("0.5", ["1/2", "0.500"], "in"))

    def test_non_numeric_sizes_are_skipped(self):
        self.assertIsNone(at.normalize_size("0.5", ["G 1/16-28"], "mm"))

    def test_size_number(self):
        for text, expected in (("0.19(#10)", 0.19), ("1 1/8", 1.125), ("6.0", 6.0), ("1/16", 0.0625), ("G 1/16-28", None),
                               ("1/0", None), ("", None)):
            self.assertEqual(at.size_number(text), expected, text)

    def test_near_sizes_are_the_numerically_closest(self):
        self.assertEqual(at.near_sizes("7/32", UNIFIED_SIZES, "in")[:2], ["0.216(#12)", "0.19(#10)"])
        self.assertEqual(at.near_sizes("7 mm", ISO_SIZES, "mm")[0], "6.0")

    def test_near_sizes_of_a_word(self):
        self.assertEqual(at.near_sizes("nope", ISO_SIZES, "mm"), [])


class ClassTests(unittest.TestCase):
    def test_unified(self):
        self.assertEqual(at.pick_class(False, ["1A", "2A", "3A"]), ("2A", True))
        self.assertEqual(at.pick_class(True, ["1B", "2B", "3B"]), ("2B", True))

    def test_iso(self):
        self.assertEqual(at.pick_class(False, ["4g6g", "6g"]), ("6g", True))
        self.assertEqual(at.pick_class(True, ["6H"]), ("6H", True))

    def test_other_families_take_the_first(self):
        self.assertEqual(at.pick_class(False, ["2G", "3G", "4G"]), ("2G", True))
        self.assertEqual(at.pick_class(True, ["A"]), ("A", True))
        self.assertEqual(at.pick_class(False, ["2B"]), ("2B", True))

    def test_a_named_class_is_not_defaulted(self):
        self.assertEqual(at.pick_class(False, ["1A", "2A"], "1A"), ("1A", False))
        self.assertEqual(at.pick_class(False, ["1A", "2A"], "1a"), ("1A", False))

    def test_a_class_not_offered_is_refused_with_the_offered(self):
        with self.assertRaises(at.ThreadError) as caught:
            at.pick_class(False, ["1A", "2A"], "2B")
        self.assertEqual(caught.exception.near, ["1A", "2A"])

    def test_no_classes_is_refused_rather_than_passing_none(self):
        with self.assertRaises(at.ThreadError):
            at.pick_class(True, [])


class FamilyTests(unittest.TestCase):
    def test_exact_and_any_case(self):
        self.assertEqual(at.resolve_family(UNIFIED, [UNIFIED, ISO]), UNIFIED)
        self.assertEqual(at.resolve_family("iso metric PROFILE", [UNIFIED, ISO]), ISO)

    def test_unknown_has_near_matches(self):
        with self.assertRaises(at.ThreadError) as caught:
            at.resolve_family("ISO Metric", [UNIFIED, ISO])
        self.assertEqual(caught.exception.near, [ISO])

    def test_tapered_is_refused_by_name(self):
        with self.assertRaises(at.ThreadError) as caught:
            at.resolve_family(NPT, [UNIFIED, ISO], [NPT])
        self.assertIn("tapered", str(caught.exception))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.query = Query()
        self.library = at.ThreadLibrary(self.query, Tapered(), cache={})

    def run_catalog(self, **arguments):
        return at.run_catalog(self.library, make_info, options(**arguments))

    def refused(self, **arguments):
        with self.assertRaises(at.ThreadError) as caught:
            self.run_catalog(**arguments)
        return caught.exception

    def test_nothing_gives_families(self):
        self.assertEqual(self.run_catalog(), {"families": Query.allThreadTypes})

    def test_family_gives_sizes(self):
        result = self.run_catalog(family=UNIFIED)
        self.assertEqual(result["sizes"], UNIFIED_SIZES)
        self.assertEqual(result["size_unit"], "in")

    def test_size_gives_designations_under_the_apis_spelling(self):
        result = self.run_catalog(family=UNIFIED, size="1/4")
        self.assertEqual(result, {"family": UNIFIED, "size": "0.25", "designations": ["1/4-20 UNC", "1/4-28 UNF"]})

    def test_gauge_size(self):
        self.assertEqual(self.run_catalog(family=UNIFIED, size="#10")["designations"], ["10-24 UNC", "10-32 UNF"])

    def test_designation_gives_classes_in_units(self):
        result = self.run_catalog(family=ISO, size="6 mm", designation="M6x1", units="mm")
        self.assertEqual(result["classes"], [{"class": "4g6g", "major": 6.32, "minor": 5.08, "pitch": 1.27},
                                             {"class": "6g", "major": 6.32, "minor": 5.08, "pitch": 1.27}])
        self.assertEqual(result["default_class"], "6g")
        self.assertEqual(result["units"]["length"], "mm")
        self.assertFalse(result["internal"])

    def test_designation_without_size_walks_the_family(self):
        result = self.run_catalog(family=UNIFIED, designation="1/4-20 UNC")
        self.assertEqual([row["class"] for row in result["classes"]], ["1A", "2A", "3A"])
        self.assertEqual(result["default_class"], "2A")

    def test_internal_side(self):
        result = self.run_catalog(family=UNIFIED, designation="1/4-20 UNC", internal=True)
        self.assertEqual([row["class"] for row in result["classes"]], ["1B", "2B", "3B"])
        self.assertTrue(result["internal"])

    def test_one_class(self):
        result = self.run_catalog(family=UNIFIED, designation="1/4-20 UNC", thread_class="1A")
        self.assertEqual([row["class"] for row in result["classes"]], ["1A"])

    def test_a_class_fusion_cannot_build_is_reported_on_its_row(self):
        result = self.run_catalog(family=UNIFIED, designation="1/4-20 UNC")
        self.assertEqual(result["classes"][2], {"class": "3A", "error": "no data"})

    def test_designation_any_case(self):
        self.assertEqual(self.run_catalog(family=UNIFIED, designation="1/4-20 unc")["designation"], "1/4-20 UNC")

    def test_unknown_family(self):
        self.assertEqual(self.refused(family="ANSI Unified").near[0], UNIFIED)

    def test_tapered_family(self):
        self.assertIn("tapered", str(self.refused(family=NPT)))

    def test_unknown_size_has_near_matches(self):
        error = self.refused(family=UNIFIED, size="7/32")
        self.assertIn("0.25", error.near)

    def test_unknown_designation_has_near_matches(self):
        error = self.refused(family=UNIFIED, designation="1/4-20 UNF")
        self.assertIn("1/4-20 UNC", error.near)

    def test_designation_outside_the_size(self):
        self.assertIn("size 0.19(#10)", str(self.refused(family=UNIFIED, size="#10", designation="1/4-20 UNC")))

    def test_class_not_offered(self):
        self.assertEqual(self.refused(family=UNIFIED, designation="1/4-20 UNC", thread_class="2B").near, ["1A", "2A", "3A"])

    def test_external_only_designation_says_so(self):
        self.assertIn("no internal form", str(self.refused(family=WOOD, designation="2x0.9", internal=True)))

    def test_a_size_fusion_will_not_expand_is_one_missing_row(self):
        self.query.SIZES = {**Query.SIZES, UNIFIED: ["0.25", "0.3"]}
        self.query.allDesignations = lambda family, size: Query.allDesignations(self.query, family, size) \
            if size == "0.25" else (_ for _ in ()).throw(RuntimeError("3"))
        self.assertEqual(self.library.all_designations(UNIFIED), ["1/4-20 UNC", "1/4-28 UNF"])

    def test_the_family_is_read_once(self):
        self.run_catalog(family=UNIFIED)
        self.run_catalog(family=UNIFIED, size="1/4")
        self.run_catalog(family=UNIFIED, size="0.25")
        self.assertEqual(self.query.calls.count(("sizes", UNIFIED)), 1)
        self.assertEqual(self.query.calls.count(("designations", UNIFIED, "0.25")), 1)

    def test_an_empty_read_is_not_cached(self):
        empty = Query()
        empty.allSizes = lambda family: []
        cache = {}
        at.ThreadLibrary(empty, cache=cache).sizes(UNIFIED)
        self.assertEqual(at.ThreadLibrary(self.query, cache=cache).sizes(UNIFIED), UNIFIED_SIZES)

    def test_families_as_a_method(self):
        self.query.allThreadTypes = lambda: [UNIFIED]
        self.assertEqual(self.library.families(), [UNIFIED])


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self.query = Query()
        self.library = at.ThreadLibrary(self.query, Tapered(), cache={})

    def recommend(self, **arguments):
        opts, err = at.validate_args({"action": "recommend", **arguments})
        assert err is None, err
        return at.run_recommend(self.library, opts)

    def test_result_warns_it_is_only_the_nearest(self):
        result = self.recommend(diameter=0.2269)
        self.assertEqual(result["designation"], "12-24 UNC")
        self.assertEqual(result["thread_class"], "3A")
        self.assertTrue(result["nearest_only"])
        self.assertIn("nearest size", result["warnings"][0])

    def test_diameter_reaches_fusion_in_cm(self):
        self.recommend(diameter=1.0)
        self.assertEqual(self.query.calls[-1], ("recommend", 2.54, False, UNIFIED))

    def test_family_defaults_by_units(self):
        self.recommend(diameter=6.0, units="mm", internal=True)
        call = self.query.calls[-1]
        self.assertAlmostEqual(call[1], 0.6)
        self.assertEqual(call[2:], (True, ISO))

    def test_named_family(self):
        self.recommend(diameter=6.0, units="mm", family=UNIFIED)
        self.assertEqual(self.query.calls[-1][3], UNIFIED)

    def test_tapered_family(self):
        with self.assertRaises(at.ThreadError):
            self.recommend(diameter=0.5, family=NPT)

    def test_no_recommendation(self):
        self.query.recommendation = (False, "", "")
        with self.assertRaises(at.ThreadError):
            self.recommend(diameter=90.0)


class ArgumentTests(unittest.TestCase):
    def refused(self, arguments, text):
        opts, err = at.validate_args(arguments)
        self.assertIsNone(opts)
        self.assertIn(text, err)

    def test_defaults(self):
        self.assertEqual(options(), {"action": "catalog", "units": "in", "internal": False, "family": None,
                                     "size": None, "designation": None, "thread_class": None})

    def test_action(self):
        self.refused({}, "'action'")
        self.refused({"action": "cut"}, "'action'")

    def test_apply_says_it_is_coming(self):
        self.refused({"action": "apply", "modeled": False}, "not available yet")

    def test_fields_belong_to_their_action(self):
        self.refused({"action": "catalog", "diameter": 1}, "diameter")
        self.refused({"action": "recommend", "diameter": 1, "size": "1/4"}, "size")
        self.refused({"action": "catalog", "face": {}}, "face")

    def test_values(self):
        self.refused({"action": "catalog", "units": "ft"}, "'units'")
        self.refused({"action": "catalog", "internal": "yes"}, "'internal'")
        self.refused({"action": "catalog", "family": ""}, "'family'")
        self.refused({"action": "catalog", "family": UNIFIED, "size": 0.25}, "'size'")

    def test_diameter(self):
        for value in (None, 0, -1, True, "0.25", float("inf")):
            self.refused({"action": "recommend", "diameter": value}, "'diameter'")

    def test_narrowing_needs_what_it_narrows(self):
        self.refused({"action": "catalog", "size": "0.25"}, "'family'")
        self.refused({"action": "catalog", "designation": "1/4-20 UNC"}, "'family'")
        self.refused({"action": "catalog", "family": UNIFIED, "thread_class": "2A"}, "'designation'")


class HandlerTests(unittest.TestCase):
    def call(self, arguments):
        library = at.ThreadLibrary(Query(), Tapered(), cache={})
        with mock.patch.object(at, "open_library", return_value=library), mock.patch.object(at, "make_info", make_info):
            result = at.apply_thread(arguments)
        return result, result["content"][0]["text"]

    def test_catalog(self):
        result, text = self.call({"action": "catalog"})
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(text)["action"], "catalog")

    def test_recommend(self):
        result, text = self.call({"action": "recommend", "diameter": 0.2269})
        self.assertFalse(result["isError"])
        self.assertTrue(json.loads(text)["nearest_only"])

    def test_refusal_is_an_error_with_near_matches(self):
        result, text = self.call({"action": "catalog", "family": "ANSI Unified"})
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(text)["near_matches"][0], UNIFIED)

    def test_bad_arguments_never_open_the_library(self):
        with mock.patch.object(at, "open_library") as opened:
            result = at.apply_thread({"action": "apply"})
        self.assertTrue(result["isError"])
        opened.assert_not_called()

    def test_a_fusion_failure_is_an_error(self):
        with mock.patch.object(at, "open_library", side_effect=RuntimeError("boom")):
            result = at.apply_thread({"action": "catalog"})
        self.assertTrue(result["isError"])
        self.assertIn("boom", result["content"][0]["text"])


class FusionLayerTests(unittest.TestCase):
    def test_library_prefers_the_static_query(self):
        query_class = mock.Mock()
        query_class.create.side_effect = lambda tapered: "tapered" if tapered else "straight"
        with mock.patch.object(at.adsk.fusion, "ThreadDataQuery", query_class, create=True):
            library = at.open_library()
        self.assertEqual(library.query, "straight")

    def test_library_falls_back_to_the_design(self):
        threads = mock.Mock(threadDataQuery="from design")
        with mock.patch.object(at.adsk.fusion, "ThreadDataQuery", None, create=True), \
                mock.patch.object(at, "_design_threads", return_value=threads):
            self.assertEqual(at.open_library().query, "from design")

    def test_info_argument_order(self):
        info_class = mock.Mock()
        with mock.patch.object(at.adsk.fusion, "ThreadInfo", info_class, create=True):
            at.make_info(True, UNIFIED, "1/4-20 UNC", "2B")
        info_class.create.assert_called_once_with(False, True, UNIFIED, "1/4-20 UNC", "2B", True)

    def test_info_falls_back_to_the_design(self):
        threads = mock.Mock()
        with mock.patch.object(at.adsk.fusion, "ThreadInfo", None, create=True), \
                mock.patch.object(at, "_design_threads", return_value=threads):
            at.make_info(True, UNIFIED, "1/4-20 UNC", "2B")
        threads.createThreadInfo.assert_called_once_with(True, UNIFIED, "1/4-20 UNC", "2B")


class SchemaTests(unittest.TestCase):
    def _tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == tool_surface.APPLY_THREAD)

    def test_schema_matches_the_handler(self):
        schema = self._tool()["inputSchema"]
        fields = at._COMMON_FIELDS.union(*at._ACTION_FIELDS.values())
        self.assertEqual(set(schema["properties"]), fields)
        self.assertEqual(schema["properties"]["action"]["enum"], list(at.ACTIONS))
        self.assertEqual(schema["required"], ["action"])
        self.assertFalse(schema["additionalProperties"])

    def test_no_keywords_clients_reject(self):
        text = json.dumps(self._tool()["inputSchema"])
        for keyword in ("minItems", "minLength", "uniqueItems", "anyOf", "oneOf"):
            self.assertNotIn(keyword, text)


if __name__ == "__main__":
    unittest.main()
