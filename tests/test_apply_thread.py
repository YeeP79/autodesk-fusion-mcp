"""apply_thread: schema, argument validation, size and class logic, and catalog
narrowing against a fake library shaped like Fusion's (no Fusion needed)."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import apply_thread as at
from fusion_bridge import tool_surface
from fusion_bridge.common import bodies
from fusion_bridge.common import entity_refs as er

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


# -- apply -----------------------------------------------------------------------

CM = 2.54
FACE_REF = {"token": "bore", "kind": "face", "point": [0.0, 0.0, 0.0]}


def apply_args(**overrides):
    return {"action": "apply", "family": UNIFIED, "designation": "1/4-20 UNC", "face": FACE_REF, "modeled": False,
            **overrides}


def apply_options(**overrides):
    opts, err = at.validate_args(apply_args(**overrides))
    assert err is None, err
    return opts


def cylinder_record(diameter=0.25, concavity="convex", sweep=360.0, siblings=0, kind="cylinder"):
    rec = {"type": kind, "ref": {"token": "bore", "kind": "face"}, "diameter": diameter, "concavity": concavity,
           "sweep_deg": sweep, "split_siblings": siblings, "axis_direction": [0.0, 0.0, 1.0],
           "length_along_axis": 1.0}
    return rec if kind == "cylinder" else {"type": kind, "ref": rec["ref"]}


class Members(list):
    count = property(len)


class ThreadBody:
    isSolid = True
    isVisible = True
    nativeObject = None
    assemblyContext = None
    boundingBox = None

    def __init__(self, name, volume):
        self.name, self.volume = name, volume
        self.faces, self.lumps, self.shells, self.edges = Members(), Members(), Members(), Members()


class ThreadFace:
    objectType = "adsk::fusion::BRepFace"
    nativeObject = None
    assemblyContext = None

    def __init__(self, body):
        self.body = body


class ThreadFeature:
    objectType = "adsk::fusion::ThreadFeature"
    healthState = 0
    errorOrWarningMessage = ""
    parentComponent = None
    timelineObject = None
    name = "Thread1"

    def deleteMe(self):
        return True


class ThreadRoot:
    allOccurrences = ()

    def __init__(self, members):
        self.bRepBodies = members


class ThreadDesign:
    designType = 1
    activeEditObject = None
    parentDocument = None
    timeline = type("T", (), {"count": 0, "markerPosition": 0, "__iter__": lambda self: iter(())})()

    def __init__(self, members):
        self.rootComponent = ThreadRoot(members)


class ThreadSession:
    """The collaborator ``run_apply`` drives: one rod, one stranger, one face."""

    def __init__(self, record=None, change=0.0):
        self.rod, self.other = ThreadBody("Rod", 10.0), ThreadBody("Other", 5.0)
        self.members = [self.rod, self.other]
        self.design = ThreadDesign(self.members)
        self.face = ThreadFace(self.rod)
        self.record = record or cylinder_record()
        self.change, self.stray = change, 0.0
        self.refuse = None
        self.health = 0
        self.added = []
        self.tokens = {"bore": [self.face]}
        self.bad_values = set()

    def entries(self):
        return bodies.assign_keys(list(bodies.body_entries(self.design.rootComponent)))

    def by_token(self, token):
        return self.tokens.get(token, [])

    def distance(self, entity, point):
        return 0.0

    def at_point(self, point, kind, tolerance_cm=None):
        return []

    def describe(self, face, units, entry):
        return self.record

    def value(self, value):
        if value["text"] in self.bad_values:
            raise ValueError(f"{value['text']!r} is not a valid expression")
        return value["text"]

    def add(self, face, info, opts):
        if self.refuse:
            raise RuntimeError(self.refuse)
        self.added.append((face, info, opts))
        self.rod.volume += self.change
        self.other.volume += self.stray
        feature = ThreadFeature()
        feature.healthState = self.health
        return feature


class ThreadInfoFake:
    def __init__(self, args):
        self.args = args
        self.majorDiameter, self.minorDiameter, self.threadPitch = 0.25 * CM, 0.2 * CM, 0.05 * CM


def info_of(*args):
    return ThreadInfoFake(args)


def apply_run(session, **overrides):
    library = at.ThreadLibrary(Query(), Tapered(), cache={})
    return at.run_apply(session.design, None, library, info_of, apply_options(units="in", **overrides), session)


class IsSplitTests(unittest.TestCase):
    def test_whole(self):
        self.assertFalse(at.is_split(360.0, 0))

    def test_two_halves(self):
        self.assertTrue(at.is_split(180.0, 1))

    def test_two_separate_same_size_bores_are_each_whole(self):
        self.assertFalse(at.is_split(360.0, 0))

    def test_stacked_walls_each_sweep_all_the_way_round(self):
        self.assertTrue(at.is_split(360.0, 1))

    def test_a_half_with_no_neighbor_found(self):
        self.assertTrue(at.is_split(180.0, 0))

    def test_sweep_boundary(self):
        self.assertFalse(at.is_split(355.0, 0))
        self.assertTrue(at.is_split(354.9, 0))

    def test_a_sweep_that_cannot_be_read_proves_nothing(self):
        self.assertTrue(at.is_split(None, 0))


class SplitOnFacesTests(unittest.TestCase):
    """``split_siblings`` as find_entities reports it, feeding ``is_split``."""

    class Surface:
        surfaceType = 1

        def __init__(self, x):
            self.origin = type("O", (), {"x": x, "y": 0.0, "z": 0.0})()
            self.axis = type("A", (), {"x": 0.0, "y": 0.0, "z": 1.0})()
            self.radius = 0.5

    class Wall:
        def __init__(self, x=0.0):
            self.geometry = SplitOnFacesTests.Surface(x)
            self.edges = []

    def link(self, face, *others):
        face.edges = [type("E", (), {"faces": [face, other]})() for other in others]

    def test_two_half_walls(self):
        a, b = self.Wall(), self.Wall()
        self.link(a, b, b)
        self.assertTrue(at.is_split(180.0, er.split_siblings(a)))

    def test_one_wall_with_its_seam(self):
        a = self.Wall()
        self.link(a, a)
        self.assertFalse(at.is_split(360.0, er.split_siblings(a)))

    def test_two_separate_bores_joined_by_a_plate(self):
        a, b, plate = self.Wall(), self.Wall(3.0), mock.Mock(geometry=mock.Mock(surfaceType=0))
        self.link(a, plate)
        self.link(plate, a, b)
        self.assertFalse(at.is_split(360.0, er.split_siblings(a)))


class ApplyCheckTests(unittest.TestCase):
    def test_concavity(self):
        self.assertIsNone(at.concavity_problem(True, "concave"))
        self.assertIsNone(at.concavity_problem(False, "convex"))
        self.assertIn("a shaft", at.concavity_problem(True, "convex"))
        self.assertIn("a bore", at.concavity_problem(False, "concave"))

    def test_unknown_concavity_is_no_contradiction(self):
        self.assertIsNone(at.concavity_problem(True, None))

    def test_not_a_cylinder(self):
        self.assertEqual(at.face_problems(cylinder_record(kind="plane"), False),
                         ["the face is a plane, not a cylinder"])

    def test_a_clean_face(self):
        self.assertEqual(at.face_problems(cylinder_record(), False), [])

    def test_face_problems_are_all_listed(self):
        problems = at.face_problems(cylinder_record(sweep=180.0, siblings=1, concavity="concave"), False)
        self.assertEqual(len(problems), 2)
        self.assertIn("2.57x", problems[0])

    def test_diameter_within_five_percent_either_way(self):
        self.assertIsNone(at.diameter_problem(0.25, 0.25, False, "in"))
        self.assertIsNone(at.diameter_problem(0.2625, 0.25, False, "in"))
        self.assertIsNone(at.diameter_problem(0.2375, 0.25, False, "in"))

    def test_diameter_just_outside(self):
        self.assertIn("major", at.diameter_problem(0.2626, 0.25, False, "in"))
        self.assertIn("major", at.diameter_problem(0.2374, 0.25, False, "in"))

    def test_internal_reads_the_minor(self):
        self.assertIsNone(at.diameter_problem(0.2, 0.2, True, "in"))
        self.assertIn("minor", at.diameter_problem(0.5, 0.2, True, "in"))

    def test_no_reference_is_no_pass(self):
        self.assertIn("no major", at.diameter_problem(0.25, None, False, "in"))
        self.assertIn("no minor", at.diameter_problem(0.25, 0.0, True, "in"))

    def test_modeled_volume(self):
        self.assertIsNone(at.volume_problem(True, 10.0, 9.0, True))
        self.assertIn("changed nothing", at.volume_problem(True, 10.0, 10.0, False))

    def test_cosmetic_volume(self):
        self.assertIsNone(at.volume_problem(False, 10.0, 10.0, False))
        self.assertIn("cosmetic", at.volume_problem(False, 10.0, 9.0, False))

    def test_a_small_thread_on_a_big_body_still_counts(self):
        self.assertIsNone(at.volume_problem(True, 1600.0, 1599.99, False))
        self.assertIsNone(at.volume_problem(True, 1600.0, 1600.0, True))

    def test_kernel_noise_on_a_big_body_is_not_a_cosmetic_cut(self):
        self.assertIsNone(at.volume_problem(False, 1600.0, 1600.0 + 1e-4, False))

    def test_unreadable_volume(self):
        self.assertIn("could not be read", at.volume_problem(True, None, 1.0, True))


class ApplyArgumentTests(unittest.TestCase):
    def refused(self, needle, **overrides):
        arguments = {k: v for k, v in apply_args(**overrides).items() if v is not None}
        opts, err = at.validate_args(arguments)
        self.assertIsNone(opts)
        self.assertIn(needle, err)

    def test_defaults(self):
        opts = apply_options()
        self.assertEqual((opts["right_handed"], opts["full_length"], opts["dry_run"]), (True, True, False))
        self.assertEqual(opts["face"]["kind"], "face")

    def test_modeled_is_required(self):
        self.refused("'modeled'", modeled=None)
        self.refused("'modeled'", modeled="no")

    def test_required(self):
        self.refused("'family'", family=None)
        self.refused("'designation'", designation=None)
        self.refused("'face'", face=None)

    def test_face(self):
        self.refused("'face'", face={"bogus": 1})
        self.refused("must be a face", face={"token": "t", "kind": "edge"})

    def test_a_face_ref_without_a_kind_is_a_face(self):
        self.assertEqual(apply_options(face={"token": "t"})["face"]["kind"], "face")

    def test_a_face_point_is_cm_inside(self):
        self.assertEqual(apply_options(face={"token": "t", "point": [1, 0, 0]})["face"]["point"], (CM, 0.0, 0.0))

    def test_length_needs_full_length_false(self):
        self.refused("need 'full_length'", length=0.5)
        self.refused("need 'full_length'", length=0.5, full_length=True)
        self.refused("need 'full_length'", offset=0.1)
        self.refused("need 'full_length'", measured_from="low")

    def test_a_partial_thread_needs_a_length(self):
        self.refused("'length'", full_length=False)
        self.refused("'length'", full_length=False, length=0)
        self.refused("'length'", full_length=False, length=-1)

    def test_partial_defaults(self):
        opts = apply_options(full_length=False, length=0.5)
        self.assertEqual((opts["length"]["text"], opts["offset"]["text"], opts["measured_from"]),
                         ("0.5 in", "0 in", "high"))

    def test_partial_expressions(self):
        opts = apply_options(full_length=False, length_expression="depth", offset_expression="lead")
        self.assertEqual((opts["length"]["text"], opts["offset"]["text"]), ("depth", "lead"))

    def test_partial_values(self):
        self.refused("'offset'", full_length=False, length=0.5, offset=-0.1)
        self.refused("'measured_from'", full_length=False, length=0.5, measured_from="middle")

    def test_booleans(self):
        self.refused("'right_handed'", right_handed="yes")
        self.refused("'dry_run'", dry_run=1)

    def test_fields_stay_with_apply(self):
        self.refused("diameter", diameter=0.25)
        opts, err = at.validate_args({"action": "catalog", "modeled": True})
        self.assertIn("modeled", err)
        self.assertIsNone(opts)


class ApplyRunTests(unittest.TestCase):
    def refused(self, session, needle, **overrides):
        payload = apply_run(session, **overrides)
        self.assertEqual(payload["status"], "refused", payload)
        self.assertIn(needle, " ".join(payload["refusals"]))
        self.assertEqual(session.added, [])
        return payload

    def test_cosmetic(self):
        session = ThreadSession()
        payload = apply_run(session)
        self.assertEqual((payload["status"], payload["ok"], payload["action"]), ("built", True, "apply"))
        self.assertEqual(payload["volume_delta"], 0.0)
        self.assertEqual(payload["thread"]["designation"], "1/4-20 UNC")
        self.assertEqual((payload["thread"]["thread_class"], payload["thread"]["class_defaulted"]), ("2A", True))
        self.assertEqual((payload["thread"]["major"], payload["thread"]["minor"], payload["thread"]["pitch"]),
                         (0.25, 0.2, 0.05))
        self.assertEqual(payload["face"]["diameter_check"], "ok")
        self.assertEqual((payload["face"]["sweep_deg"], payload["face"]["split_siblings"]), (360.0, 0))
        self.assertEqual(payload["feature"]["type"], "ThreadFeature")
        self.assertTrue(payload["baseline_token"])
        self.assertEqual(session.added[0][0], session.face)

    def test_modeled(self):
        payload = apply_run(ThreadSession(change=-1.0), modeled=True)
        self.assertEqual(payload["status"], "built")
        self.assertAlmostEqual(payload["volume_delta"], -1.0 / CM ** 3, places=5)
        self.assertEqual(payload["bodies"]["touched"][0]["role"], "target")
        self.assertIn("gone", " ".join(payload["warnings"]))

    def test_modeled_that_removed_nothing_is_suspect(self):
        payload = apply_run(ThreadSession(), modeled=True)
        self.assertEqual((payload["status"], payload["ok"]), ("suspect", False))
        self.assertIn("changed nothing", payload["warnings"][0])

    def test_cosmetic_that_cut_is_suspect(self):
        payload = apply_run(ThreadSession(change=-1.0))
        self.assertEqual(payload["status"], "suspect")

    def test_a_split_face_is_refused_either_way(self):
        for modeled in (False, True):
            session = ThreadSession(cylinder_record(sweep=180.0, siblings=1))
            self.refused(session, "2.57x", modeled=modeled)

    def test_a_short_sweep_alone_is_refused(self):
        self.refused(ThreadSession(cylinder_record(sweep=200.0)), "not a whole cylinder")

    def test_a_sibling_alone_is_refused(self):
        self.refused(ThreadSession(cylinder_record(siblings=1)), "not a whole cylinder")

    def test_a_plane_is_refused(self):
        payload = self.refused(ThreadSession(cylinder_record(kind="plane")), "not a cylinder")
        self.assertEqual(payload["face"]["diameter_check"], "not_checked")

    def test_contradicting_concavity(self):
        self.refused(ThreadSession(cylinder_record(concavity="concave")), "a bore")
        self.refused(ThreadSession(cylinder_record(diameter=0.2)), "a shaft", internal=True)

    def test_internal_checks_against_the_minor(self):
        session = ThreadSession(cylinder_record(diameter=0.2, concavity="concave"))
        payload = apply_run(session, internal=True)
        self.assertEqual(payload["status"], "built", payload)
        self.assertEqual(payload["thread"]["thread_class"], "2B")
        self.assertEqual(payload["face"]["diameter_check"], "ok")

    def test_a_half_inch_hole_is_refused(self):
        session = ThreadSession(cylinder_record(diameter=0.5, concavity="concave"))
        payload = self.refused(session, "minor diameter", internal=True)
        self.assertEqual(payload["face"]["diameter_check"], "out_of_range")

    def test_diameter_check_uses_the_callers_units(self):
        session = ThreadSession(cylinder_record(diameter=6.35))
        library = at.ThreadLibrary(Query(), Tapered(), cache={})
        opts = apply_options(units="mm", face={"token": "bore"})
        payload = at.run_apply(session.design, None, library, info_of, opts, session)
        self.assertEqual(payload["status"], "built", payload)

    def test_a_named_class(self):
        payload = apply_run(ThreadSession(), thread_class="3A")
        self.assertEqual((payload["thread"]["thread_class"], payload["thread"]["class_defaulted"]), ("3A", False))

    def test_handedness_reaches_the_info(self):
        session = ThreadSession()
        apply_run(session, right_handed=False)
        self.assertEqual(session.added[0][1].args, (False, UNIFIED, "1/4-20 UNC", "2A", False))

    def test_size_narrows_and_takes_the_dialog_spelling(self):
        self.assertEqual(apply_run(ThreadSession(), size="1/4")["status"], "built")

    def test_unknown_family_has_near_matches(self):
        payload = self.refused(ThreadSession(), "unknown thread family", family="ANSI Unified")
        self.assertEqual(payload["near_matches"][0], UNIFIED)

    def test_unknown_designation_has_near_matches(self):
        payload = self.refused(ThreadSession(), "unknown designation", designation="1/4-20 UNX")
        self.assertIn("1/4-20 UNC", payload["near_matches"])

    def test_tapered_family(self):
        self.refused(ThreadSession(), "tapered", family=NPT)

    def test_a_class_fusion_will_not_build(self):
        library = at.ThreadLibrary(Query(), Tapered(), cache={})

        def broken(*args):
            raise RuntimeError("no data")

        session = ThreadSession()
        payload = at.run_apply(session.design, None, library, broken, apply_options(), session)
        self.assertIn("could not build", payload["refusals"][0])

    def test_every_refusal_is_listed_together(self):
        session = ThreadSession(cylinder_record(sweep=180.0, siblings=1))
        session.design.activeEditObject = type("B", (), {"objectType": "adsk::fusion::BaseFeature", "name": "B"})()
        payload = self.refused(session, "2.57x", family="ANSI Unified")
        self.assertEqual(len(payload["refusals"]), 3)

    def test_dry_run_adds_nothing(self):
        session = ThreadSession()
        payload = apply_run(session, dry_run=True)
        self.assertEqual((payload["status"], payload["ok"], payload["dry_run"]), ("planned", True, True))
        self.assertEqual(session.added, [])
        self.assertEqual(payload["thread"]["designation"], "1/4-20 UNC")
        self.assertEqual(payload["face"]["diameter_check"], "ok")
        self.assertEqual(payload["plan"]["target"], "root/Rod")

    def test_dry_run_still_refuses(self):
        payload = self.refused(ThreadSession(cylinder_record(sweep=180.0)), "2.57x", dry_run=True)
        self.assertTrue(payload["dry_run"])

    def test_partial_thread_reports_where_it_is_measured_from(self):
        session = ThreadSession()
        payload = apply_run(session, full_length=False, length=0.5, offset=0.1, measured_from="low")
        self.assertEqual((payload["thread"]["length"], payload["thread"]["offset"], payload["thread"]["measured_from"]),
                         ("0.5 in", "0.1 in", "low"))
        self.assertEqual(payload["face"]["axis_world"], [0.0, 0.0, 1.0])

    def test_a_bad_length_expression_is_refused_before_the_add(self):
        session = ThreadSession()
        session.bad_values.add("depth")
        self.refused(session, "not a valid expression", full_length=False, length_expression="depth")

    def test_fusion_refusing_keeps_its_words(self):
        session = ThreadSession()
        session.refuse = "The thread could not be created"
        payload = apply_run(session)
        self.assertEqual(payload["refusals"], ["The thread could not be created"])
        self.assertTrue(payload["baseline_token"])

    def test_a_feature_that_came_back_in_error(self):
        session = ThreadSession()
        session.health = 2
        self.assertEqual(apply_run(session)["status"], "refused")

    def test_another_body_losing_volume_is_suspect(self):
        session = ThreadSession()
        session.stray = -1.0
        payload = apply_run(session)
        self.assertEqual(payload["status"], "suspect")
        self.assertIn("root/Other", payload["warnings"][0])

    def test_a_face_that_will_not_resolve(self):
        session = ThreadSession()
        session.tokens = {}
        self.assertIn("(not_found)", apply_run(session)["refusals"][0])

    def test_a_face_on_no_body(self):
        session = ThreadSession()
        session.face.body = ThreadBody("Stranger", 1.0)
        self.assertIn("not on a body", apply_run(session)["refusals"][0])

    def test_a_surface_body(self):
        session = ThreadSession()
        session.rod.isSolid = False
        self.assertIn("surface body", apply_run(session)["refusals"][0])

    def test_a_hidden_body_is_mentioned(self):
        session = ThreadSession()
        session.rod.isVisible = False
        self.assertIn("hidden", " ".join(apply_run(session)["warnings"]))

    def test_preflight_runs(self):
        session = ThreadSession()
        session.design.activeEditObject = type("B", (), {"objectType": "adsk::fusion::BaseFeature", "name": "B"})()
        self.refused(session, "being edited")

    def test_a_failed_post_check_still_says_the_thread_exists(self):
        with mock.patch.object(at.fo, "body_changes", side_effect=KeyError("boom")):
            payload = apply_run(ThreadSession())
        self.assertEqual(payload["status"], "suspect")
        self.assertIn("was added", payload["warnings"][-1])


class ApplyHandlerTests(unittest.TestCase):
    def call(self, session, arguments):
        library = at.ThreadLibrary(Query(), Tapered(), cache={})
        with mock.patch.object(at, "get_app"), mock.patch.object(at.adsk.fusion, "Design", create=True) as design, \
                mock.patch.object(at, "open_library", return_value=library), \
                mock.patch.object(at, "make_info", info_of), \
                mock.patch.object(at, "FusionThread", return_value=session):
            design.cast.return_value = session.design if session else None
            result = at.apply_thread(arguments)
        return result, json.loads(result["content"][0]["text"])

    def test_built(self):
        result, payload = self.call(ThreadSession(), apply_args())
        self.assertFalse(result["isError"])
        self.assertEqual(payload["status"], "built")

    def test_suspect_is_an_error(self):
        result, payload = self.call(ThreadSession(change=-1.0), apply_args())
        self.assertTrue(result["isError"])
        self.assertEqual(payload["status"], "suspect")

    def test_no_design(self):
        result, payload = self.call(None, apply_args())
        self.assertTrue(result["isError"])
        self.assertEqual(payload["status"], "refused")


class FusionThreadTests(unittest.TestCase):
    def build(self, **overrides):
        design = mock.Mock()
        design.unitsManager.isValidExpression.return_value = True
        with mock.patch.object(at.FusionScene, "__init__", return_value=None):
            session = at.FusionThread(design)
        member = mock.Mock()
        face = mock.Mock(nativeObject=member)
        threads = member.body.parentComponent.features.threadFeatures
        collection = mock.Mock()
        locations = mock.Mock()
        with mock.patch.object(at.adsk.core.ObjectCollection, "create", return_value=collection), \
                mock.patch.object(at.adsk.core, "ValueInput") as values, \
                mock.patch.object(at.adsk.fusion, "ThreadLocations", locations, create=True):
            values.createByString.side_effect = lambda text: f"value:{text}"
            session.add(face, "info", apply_options(**overrides))
        return threads, collection, member, locations

    def test_one_native_face_in_the_faces_component(self):
        threads, collection, member, _ = self.build()
        collection.add.assert_called_once_with(member)
        threads.createInput.assert_called_once_with(collection, "info")
        threads.add.assert_called_once_with(threads.createInput.return_value)

    def test_modeled_and_full_length_are_stated(self):
        entity = self.build(modeled=True)[0].createInput.return_value
        self.assertIs(entity.isModeled, True)
        self.assertIs(entity.isFullLength, True)

    def test_partial(self):
        threads, _, _, locations = self.build(full_length=False, length=0.5, offset=0.1, measured_from="low")
        entity = threads.createInput.return_value
        self.assertIs(entity.isFullLength, False)
        self.assertEqual((entity.threadLength, entity.threadOffset), ("value:0.5 in", "value:0.1 in"))
        self.assertEqual(entity.threadLocation, locations.LowEndThreadLocation)

    def test_high_is_the_default_end(self):
        threads, _, _, locations = self.build(full_length=False, length=0.5)
        self.assertEqual(threads.createInput.return_value.threadLocation, locations.HighEndThreadLocation)

    def test_handedness_falls_back_on_the_old_route(self):
        threads = mock.Mock()
        with mock.patch.object(at.adsk.fusion, "ThreadInfo", None, create=True), \
                mock.patch.object(at, "_design_threads", return_value=threads):
            info = at.make_info(True, UNIFIED, "1/4-20 UNC", "2B", False)
        self.assertIs(info.isRightHanded, False)


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
