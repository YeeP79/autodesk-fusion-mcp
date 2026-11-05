"""symmetry_report: arguments, the plane, pairing on plain numbers, confirmation,
and whole runs over voxel bodies a fake kernel can really reflect (no Fusion needed)."""

import json
import math
import unittest
from types import SimpleNamespace
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import audit_rules as rules
from fusion_bridge import symmetry_report as sr
from fusion_bridge import tool_surface
from fusion_bridge.common import geometry

YZ = sr.make_plane((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), "yz")
TOL = 0.0254  # 0.01 in, in cm


def options(**overrides):
    return sr.validate_arguments({"plane": "yz", **overrides})


def box_around(center, half):
    return (tuple(c - h for c, h in zip(center, half)), tuple(c + h for c, h in zip(center, half)))


def entry(name="B", path="root", component="Root", visible=True, solid=True, box=((0, 0, 0), (1, 1, 1)), index=0):
    return {"occurrence_path": path, "component": component, "linked": False, "body": name, "index": index,
            "visible": visible, "solid": solid, "bbox": box, "_body": None}


def body(center, half=(1.0, 1.0, 1.0), volume=8.0, name="B", plane=YZ):
    return sr.measured(entry(name, box=box_around(center, half)), volume, center, plane)


def pairs_of(bodies, plane=YZ, tolerance=TOL):
    return sr.pair_bodies(bodies, plane, tolerance)


def names(pair):
    return pair["a"]["body"], pair["b"]["body"]


# -- Voxel fakes -----------------------------------------------------------------


class Coll:
    def __init__(self, members=()):
        self._members = list(members)
        self.count = len(self._members)

    def item(self, index):
        return self._members[index]


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    def transformBy(self, matrix):
        self.x, self.y, self.z = matrix.apply((self.x, self.y, self.z))
        return True


class Matrix:
    def __init__(self):
        self.rows = [[float(i == j) for j in range(4)] for i in range(4)]

    def setCell(self, row, column, value):
        self.rows[row][column] = value

    def apply(self, point):
        return tuple(sum(c * x for c, x in zip(self.rows[i], (*point, 1.0))) for i in range(3))


class Voxels:
    """A solid of 1 cm cubes, each named by its low corner."""

    isSolid = True

    def __init__(self, cells, name="Body", visible=True, context=None, offset=(0, 0, 0)):
        self.cells = set(cells)
        self.name, self.isVisible, self.assemblyContext = name, visible, context
        self.offset = offset

    @property
    def volume(self):
        return float(len(self.cells))

    @property
    def lumps(self):
        return Coll([self] if self.cells else [])

    @property
    def boundingBox(self):
        lows = [min(c[i] for c in self.cells) for i in range(3)]
        highs = [max(c[i] for c in self.cells) + 1 for i in range(3)]
        return SimpleNamespace(minPoint=P(*lows), maxPoint=P(*highs))

    @property
    def physicalProperties(self):
        count = len(self.cells)
        middle = [sum(c[i] + 0.5 for c in self.cells) / count - self.offset[i] for i in range(3)]
        return SimpleNamespace(centerOfMass=P(*middle))


class Manager:
    """A kernel over voxels whose transform really moves cells."""

    def __init__(self, reflects=True, fail_on=None):
        self.reflects, self.fail_on, self.copies = reflects, fail_on, 0

    def copy(self, source):
        self.copies += 1
        return Voxels(source.cells, source.name)

    def transform(self, target, matrix):
        if self.reflects is False:
            return False
        if self.reflects == "raises":
            raise RuntimeError("non-rigid transform")
        if self.reflects == "ignored":
            return True
        moved = set()
        for cell in target.cells:
            middle = matrix.apply(tuple(c + 0.5 for c in cell))
            moved.add(tuple(round(c - 0.5) for c in middle))
        target.cells = moved
        return True

    def booleanOperation(self, target, tool, kind):
        if self.fail_on is not None and self.fail_on in (target.name, tool.name):
            return False
        target.cells = target.cells & tool.cells if kind == "IntersectionBooleanType" else target.cells - tool.cells
        return True


def mirrored(cells):
    return {(-x - 1, y, z) for x, y, z in cells}


def bar(x=2, length=3):
    return {(x + i, 0, 0) for i in range(length)}


CHIRAL = {(1, 0, 0), (2, 1, 0)}
OTHER_HAND = {(-3, 0, 0), (-2, 1, 0)}  # same box, volume and center as CHIRAL's mirror, different solid


class Scene:
    def __init__(self, bodies=(), occurrences=()):
        self.rootComponent = SimpleNamespace(name="Root", bRepBodies=Coll(bodies), allOccurrences=Coll(occurrences))
        self.tokens = {}

    def findEntityByToken(self, token):
        return self.tokens.get(token, [])


def occurrence(path, component, bodies, visible=True):
    return SimpleNamespace(fullPathName=path, component=SimpleNamespace(name=component), bRepBodies=Coll(bodies),
                           isVisible=visible, isReferencedComponent=False)


class ToolCase(unittest.TestCase):
    def call(self, scene, manager=None, **arguments):
        self.manager = manager or Manager()
        fusion = mock.Mock()
        fusion.Design.cast.return_value = scene
        fusion.TemporaryBRepManager.get.return_value = self.manager
        fusion.BooleanTypes = SimpleNamespace(IntersectionBooleanType="IntersectionBooleanType",
                                              DifferenceBooleanType="DifferenceBooleanType")
        core = mock.Mock()
        core.Matrix3D.create.side_effect = Matrix
        core.Point3D.create.side_effect = P
        with mock.patch.object(sr, "get_app"), mock.patch.object(sr.adsk, "fusion", fusion, create=True), \
                mock.patch.object(sr.adsk, "core", core, create=True):
            return sr.symmetry_report({"units": "cm", **arguments})

    def report(self, scene, manager=None, **arguments):
        arguments.setdefault("plane", "yz")
        result = self.call(scene, manager, **arguments)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])


# -- Arguments -------------------------------------------------------------------


class ArgumentTests(unittest.TestCase):
    def test_defaults(self):
        opts = options()
        self.assertEqual(opts["plane"], {"root": "yz"})
        self.assertEqual(opts["units"], "in")
        self.assertAlmostEqual(opts["tolerance"], TOL)
        self.assertEqual((opts["include_hidden"], opts["component_contains"]), (False, None))
        self.assertEqual((opts["max_results"], opts["time_budget_s"]), (100, 90.0))

    def test_tolerance_is_in_the_callers_units(self):
        self.assertAlmostEqual(options(units="mm", tolerance=1)["tolerance"], 0.1)

    def test_the_default_tolerance_stays_0_01_in_whatever_the_units(self):
        self.assertAlmostEqual(options(units="mm")["tolerance"], TOL)

    def test_point_and_normal(self):
        opts = sr.validate_arguments({"point": [1, 2, 3], "normal": [0, 0, 5], "units": "in"})
        self.assertEqual(opts["plane"]["normal"], (0.0, 0.0, 1.0))
        self.assertEqual(opts["plane"]["point"], (2.54, 5.08, 7.62))

    def test_refusals(self):
        cases = [
            ({}, "exactly one"),
            ({"plane": "yz", "plane_token": "t"}, "exactly one"),
            ({"plane": "yz", "point": [0, 0, 0], "normal": [1, 0, 0]}, "exactly one"),
            ({"plane": "top"}, "'plane'"),
            ({"plane_token": ""}, "plane_token"),
            ({"point": [0, 0, 0]}, "both"),
            ({"normal": [1, 0, 0]}, "both"),
            ({"point": [0, 0], "normal": [1, 0, 0]}, "'point'"),
            ({"point": [0, 0, 0], "normal": [0, 0, 0]}, "zero vector"),
            ({"point": [0, 0, float("nan")], "normal": [1, 0, 0]}, "'point'"),
            ({"plane": "yz", "units": "ft"}, "'units'"),
            ({"plane": "yz", "tolerance": 0}, "'tolerance'"),
            ({"plane": "yz", "tolerance": -1}, "'tolerance'"),
            ({"plane": "yz", "tolerance": True}, "'tolerance'"),
            ({"plane": "yz", "include_hidden": "no"}, "include_hidden"),
            ({"plane": "yz", "component_contains": " "}, "component_contains"),
            ({"plane": "yz", "max_results": 0}, "max_results"),
            ({"plane": "yz", "time_budget_s": 111}, "time_budget_s"),
            ({"plane": "yz", "time_budget_s": 0.5}, "time_budget_s"),
            ({"plane": "yz", "bogus": 1}, "bogus"),
        ]
        for arguments, text in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError) as caught:
                sr.validate_arguments(arguments)
            self.assertIn(text, str(caught.exception))

    def test_arguments_must_be_an_object(self):
        with self.assertRaises(ValueError):
            sr.validate_arguments([])

    def test_the_budget_ceiling_stays_under_the_main_thread_limit(self):
        import settings
        self.assertLess(sr.MAX_TIME_BUDGET_S + sr.PAIRING_GRACE_S, settings.MCP_MAIN_THREAD_TIMEOUT)


class SchemaTests(unittest.TestCase):
    def tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == tool_surface.SYMMETRY_REPORT)

    def test_schema_matches_the_handler(self):
        schema = self.tool()["inputSchema"]
        self.assertEqual(set(schema["properties"]), sr._KNOWN)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["time_budget_s"]["maximum"], sr.MAX_TIME_BUDGET_S)
        self.assertEqual(schema["properties"]["max_results"]["maximum"], sr.MAX_RESULTS_LIMIT)
        self.assertEqual(set(schema["properties"]["plane"]["enum"]), set(sr.ROOT_PLANE_NORMALS))


# -- The plane -------------------------------------------------------------------


class PlaneTests(unittest.TestCase):
    def test_axis_aligned(self):
        self.assertTrue(YZ["axis_aligned"])
        self.assertTrue(sr.make_plane((5, 0, 0), (0, -1, 0), "p")["axis_aligned"])
        self.assertFalse(sr.make_plane((0, 0, 0), (1, 1, 0), "p")["axis_aligned"])

    def test_a_zero_normal_is_refused(self):
        with self.assertRaises(ValueError):
            sr.make_plane((0, 0, 0), (0, 0, 0), "p")

    def test_mirror_point(self):
        self.assertEqual(geometry.mirror_point((3.0, 1.0, 2.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)), (-1.0, 1.0, 2.0))

    def test_a_mirrored_point_keeps_its_distance_on_an_oblique_plane(self):
        normal = geometry.unit((1.0, 2.0, 2.0))
        plane_point = (1.0, 1.0, 1.0)
        point = (4.0, -2.0, 7.0)
        image = geometry.mirror_point(point, plane_point, normal)
        self.assertAlmostEqual(geometry.dot(geometry.sub(point, plane_point), normal),
                               -geometry.dot(geometry.sub(image, plane_point), normal))

    def test_mirror_box_swaps_its_corners_across_the_plane(self):
        self.assertEqual(sr.mirror_box(((1, 2, 3), (4, 5, 6)), YZ), ((-4, 2, 3), (-1, 5, 6)))

    def test_reflection_rows_agree_with_mirror_point(self):
        plane_point, normal = (1.0, 2.0, 3.0), geometry.unit((1.0, -2.0, 0.5))
        rows = sr.reflection_rows(plane_point, normal)
        point = (5.0, -1.0, 2.0)
        moved = tuple(sum(c * x for c, x in zip(rows[i], (*point, 1.0))) for i in range(3))
        for got, want in zip(moved, geometry.mirror_point(point, plane_point, normal)):
            self.assertAlmostEqual(got, want)
        self.assertEqual(rows[3], [0.0, 0.0, 0.0, 1.0])

    def test_reflection_turns_handedness_over(self):
        rows = sr.reflection_rows((0, 0, 0), geometry.unit((1.0, 1.0, 1.0)))
        a, b, c = (row[:3] for row in rows[:3])
        self.assertAlmostEqual(geometry.dot(a, geometry.cross(b, c)), -1.0)


class ResolvePlaneTests(unittest.TestCase):
    def surface(self, surface_type=0):
        return SimpleNamespace(surfaceType=surface_type, origin=P(1, 2, 3), normal=P(0, 0, 2))

    def resolve(self, *found):
        scene = Scene()
        scene.tokens["t"] = list(found)
        return sr.resolve_plane({"token": "t"}, scene), scene

    def test_a_root_plane_and_a_point_normal(self):
        self.assertEqual(sr.resolve_plane({"root": "xz"}, Scene())["normal"], (0.0, 1.0, 0.0))
        plane = sr.resolve_plane({"point": (1.0, 0.0, 0.0), "normal": (1.0, 0.0, 0.0)}, Scene())
        self.assertEqual((plane["point"], plane["source"]), ((1.0, 0.0, 0.0), "point_normal"))

    def test_a_token_gives_the_entitys_own_plane(self):
        plane, _ = self.resolve(SimpleNamespace(geometry=self.surface(), name="Center", assemblyContext=None))
        self.assertEqual((plane["point"], plane["normal"], plane["source"]), ((1, 2, 3), (0.0, 0.0, 1.0), "Center"))

    def test_a_missing_or_ambiguous_token_is_refused(self):
        with self.assertRaises(ValueError):
            self.resolve()
        plane = SimpleNamespace(geometry=self.surface())
        with self.assertRaises(ValueError):
            self.resolve(plane, plane)

    def test_a_curved_face_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.resolve(SimpleNamespace(geometry=self.surface(1)))
        self.assertIn("planar", str(caught.exception))

    def test_a_native_plane_of_a_sub_component_is_refused(self):
        native = SimpleNamespace(geometry=self.surface(), component=object(), assemblyContext=None)
        with self.assertRaises(ValueError) as caught:
            self.resolve(native)
        self.assertIn("occurrence", str(caught.exception))

    def test_a_plane_seen_through_an_occurrence_is_fine(self):
        proxy = SimpleNamespace(geometry=self.surface(), component=object(), assemblyContext=object())
        self.assertEqual(self.resolve(proxy)[0]["point"], (1, 2, 3))

    def test_a_face_of_a_sub_component_is_checked_through_its_body(self):
        face = SimpleNamespace(geometry=self.surface(), body=SimpleNamespace(parentComponent=object()),
                               assemblyContext=None)
        with self.assertRaises(ValueError):
            self.resolve(face)


# -- Reading ---------------------------------------------------------------------


class SelectionTests(unittest.TestCase):
    def test_surfaces_and_hidden_bodies_are_left_out(self):
        opts = options()
        self.assertEqual(sr.wanted(entry(solid=False), opts), "not_solid")
        self.assertEqual(sr.wanted(entry(visible=False), opts), "hidden")
        self.assertIsNone(sr.wanted(entry(), opts))

    def test_include_hidden(self):
        self.assertIsNone(sr.wanted(entry(visible=False), options(include_hidden=True)))

    def test_component_contains_reads_component_and_path_ignoring_case(self):
        opts = options(component_contains="front")
        self.assertIsNone(sr.wanted(entry(component="Front Axle"), opts))
        self.assertIsNone(sr.wanted(entry(path="FRONT:1+Arm:1", component="Arm"), opts))
        self.assertEqual(sr.wanted(entry(path="Rear:1", component="Arm"), opts), "filtered")

    def test_component_contains_never_reads_the_body_name(self):
        self.assertEqual(sr.wanted(entry(name="front"), options(component_contains="front")), "filtered")


class MeasuredTests(unittest.TestCase):
    def test_no_box_or_no_volume_cannot_be_measured(self):
        self.assertIsNone(sr.measured(entry(box=None), 1.0, (0, 0, 0), YZ))
        self.assertIsNone(sr.measured(entry(), None, (0, 0, 0), YZ))
        self.assertIsNone(sr.measured(entry(), 0.0, (0, 0, 0), YZ))

    def test_a_missing_center_stands_in_with_the_middle_of_the_box(self):
        found = sr.measured(entry(box=((0, 0, 0), (2, 4, 6))), 1.0, None, YZ)
        self.assertEqual(found["center"], (1.0, 2.0, 3.0))
        self.assertTrue(found["center_estimated"])

    def test_sides(self):
        self.assertEqual(sr.side_of(body((5, 0, 0)), TOL), sr.POSITIVE)
        self.assertEqual(sr.side_of(body((-5, 0, 0)), TOL), sr.NEGATIVE)
        self.assertEqual(sr.side_of(body((0.01, 0, 0)), TOL), sr.ON_PLANE)


class WorldCenterTests(unittest.TestCase):
    BOX = ((10, 0, 0), (12, 2, 2))

    def test_a_center_inside_its_box_is_taken_as_read(self):
        self.assertEqual(sr.world_center((11, 1, 1), self.BOX, lambda c: self.fail("not needed")), (11, 1, 1))

    def test_one_outside_went_unmapped_so_it_goes_through_the_occurrence(self):
        self.assertEqual(sr.world_center((1, 1, 1), self.BOX, lambda c: (11, 1, 1)), (11, 1, 1))

    def test_one_still_outside_is_not_a_center(self):
        self.assertIsNone(sr.world_center((1, 1, 1), self.BOX, lambda c: (50, 0, 0)))
        self.assertIsNone(sr.world_center((1, 1, 1), self.BOX, lambda c: None))

    def test_none_stays_none(self):
        self.assertIsNone(sr.world_center(None, self.BOX, lambda c: c))


# -- Pairing ---------------------------------------------------------------------


class PairingTests(unittest.TestCase):
    def test_a_true_mirror(self):
        pairs, unmatched, finished = pairs_of([body((5, 1, 2), name="R"), body((-5, 1, 2), name="L")])
        self.assertEqual((len(pairs), unmatched, finished), (1, [], True))
        self.assertEqual(pairs[0]["status"], sr.TRUE_MIRROR)
        self.assertAlmostEqual(pairs[0]["deviation"]["center"], 0.0)

    def test_nothing_in_is_nothing_out(self):
        self.assertEqual(pairs_of([]), ([], [], True))

    def test_a_lone_body_is_unmatched(self):
        pairs, unmatched, _ = pairs_of([body((5, 0, 0), name="R")])
        self.assertEqual((pairs, [b["body"] for b in unmatched]), ([], ["R"]))

    def test_names_mean_nothing(self):
        bodies = [body((5, 0, 0), name="Left arm"), body((-5, 3, 0), name="Left arm"), body((-5, 0, 0), name="Tab")]
        pairs, unmatched, _ = pairs_of(bodies)
        self.assertEqual(names(pairs[0]), ("Left arm", "Tab"))
        self.assertEqual(unmatched[0]["center"], (-5, 3, 0))

    def test_a_is_the_body_further_along_the_normal_either_way_round(self):
        for order in ((1, -1), (-1, 1)):
            pair = pairs_of([body((5 * order[0], 0, 0), name="first"), body((5 * order[1], 0, 0), name="second")])[0][0]
            self.assertGreater(pair["a"]["distance"], 0)

    def test_the_offset_is_b_minus_the_mirror_of_a(self):
        pair = pairs_of([body((5, 0, 0)), body((-5.5, 0.25, 0))])[0][0]
        for got, want in zip(pair["deviation"]["offset"], (-0.5, 0.25, 0.0)):
            self.assertAlmostEqual(got, want)

    def test_bodies_on_one_side_do_not_pair(self):
        self.assertEqual(pairs_of([body((5, 0, 0)), body((5, 0.1, 0))])[0], [])

    def test_each_body_is_used_once_and_the_closest_wins(self):
        bodies = [body((5, 0, 0), name="R"), body((-5.5, 0, 0), name="far"), body((-5.01, 0, 0), name="close")]
        pairs, unmatched, _ = pairs_of(bodies)
        self.assertEqual(names(pairs[0]), ("R", "close"))
        self.assertEqual([b["body"] for b in unmatched], ["far"])

    def test_the_closest_pair_is_taken_first_across_the_whole_assembly(self):
        # Taken in list order, R1 would claim L2 and strand R2.
        bodies = [body((5, 0, 0), name="R1"), body((5, 0.2, 0), name="R2"), body((-5, 0.2, 0), name="L2"),
                  body((-5, -0.5, 0), name="L1")]
        self.assertEqual({names(p) for p in pairs_of(bodies)[0]}, {("R2", "L2"), ("R1", "L1")})

    def test_volume_tells_nested_bodies_apart(self):
        bodies = [body((5, 0, 0), volume=8.0, name="tube"), body((5, 0, 0), volume=6.0, name="sleeve"),
                  body((-5, 0, 0), volume=6.0, name="sleeve2"), body((-5, 0, 0), volume=8.0, name="tube2")]
        self.assertEqual({names(p) for p in pairs_of(bodies)[0]}, {("tube", "tube2"), ("sleeve", "sleeve2")})

    def test_a_partner_further_off_than_its_size_is_no_partner(self):
        size = math.dist(*box_around((0, 0, 0), (1, 1, 1)))
        self.assertEqual(pairs_of([body((5, 0, 0)), body((-5, size + 0.01, 0))])[0], [])
        self.assertEqual(len(pairs_of([body((5, 0, 0)), body((-5, size - 0.01, 0))])[0]), 1)

    def test_a_body_of_another_volume_altogether_is_no_partner(self):
        self.assertEqual(pairs_of([body((5, 0, 0), volume=8.0), body((-5, 0, 0), volume=3.9)])[0], [])
        self.assertEqual(len(pairs_of([body((5, 0, 0), volume=8.0), body((-5, 0, 0), volume=4.0)])[0]), 1)

    def test_a_body_on_the_plane_pairs_with_itself(self):
        pairs, unmatched, _ = pairs_of([body((0, 1, 2), name="Cross")])
        self.assertEqual((unmatched, pairs[0]["on_plane"], names(pairs[0])), ([], True, ("Cross", "Cross")))
        self.assertEqual(pairs[0]["status"], sr.TRUE_MIRROR)

    def test_a_lopsided_body_through_the_plane_pairs_with_itself_and_shows_it(self):
        pair = pairs_of([body((0.5, 0, 0), half=(2, 1, 1))])[0][0]
        self.assertTrue(pair["on_plane"])
        self.assertAlmostEqual(pair["deviation"]["center"], 1.0)
        self.assertEqual(pair["status"], sr.DIFFERENT)

    def test_half_of_a_split_part_touching_the_plane_is_not_its_own_mirror(self):
        pairs, unmatched, _ = pairs_of([body((1, 0, 0), half=(1, 1, 1), name="half")])
        self.assertEqual((pairs, unmatched[0]["body"]), ([], "half"))

    def test_two_halves_meeting_at_the_plane_pair_with_each_other(self):
        pairs, _, _ = pairs_of([body((1, 0, 0)), body((-1, 0, 0))])
        self.assertEqual((len(pairs), pairs[0]["on_plane"]), (1, False))

    def test_a_real_partner_beats_being_its_own_mirror(self):
        pairs, _, _ = pairs_of([body((0.5, 0, 0), half=(2, 1, 1), name="R"), body((-0.5, 0, 0), half=(2, 1, 1), name="L")])
        self.assertEqual((len(pairs), names(pairs[0])), (1, ("R", "L")))

    def test_an_oblique_plane_compares_no_boxes(self):
        plane = sr.make_plane((0, 0, 0), (1, 1, 0), "p")
        center = (3.0, 1.0, 0.0)
        image = geometry.mirror_point(center, plane["point"], plane["normal"])
        pairs, _, _ = pairs_of([body(center, plane=plane), body(image, plane=plane)], plane)
        self.assertIsNone(pairs[0]["deviation"]["box"])
        self.assertEqual(pairs[0]["status"], sr.TRUE_MIRROR)

    def test_a_stop_ends_pairing_and_says_so(self):
        bodies = [body((5, 0, 0)), body((-5, 0, 0))]
        pairs, unmatched, finished = sr.pair_bodies(bodies, YZ, TOL, lambda: True)
        self.assertEqual((pairs, len(unmatched), finished), ([], 2, False))

    def test_a_plane_off_the_origin(self):
        plane = sr.make_plane((10, 0, 0), (1, 0, 0), "p")
        pairs, _, _ = pairs_of([body((12, 0, 0), plane=plane), body((8, 0, 0), plane=plane)], plane)
        self.assertEqual(pairs[0]["status"], sr.TRUE_MIRROR)


class ClassifyTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(sr.classify(0.0, TOL), sr.TRUE_MIRROR)
        self.assertEqual(sr.classify(TOL, TOL), sr.TRUE_MIRROR)
        self.assertEqual(sr.classify(TOL * 1.01, TOL), sr.NEAR)
        self.assertEqual(sr.classify(TOL * sr.NEAR_FACTOR, TOL), sr.NEAR)
        self.assertEqual(sr.classify(TOL * sr.NEAR_FACTOR * 1.01, TOL), sr.DIFFERENT)

    def status(self, a, b):
        return pairs_of([a, b])[0][0]["status"]

    def test_the_box_alone_can_spoil_a_pair(self):
        self.assertEqual(self.status(body((5, 0, 0)), body((-5, 0, 0), half=(1, 1, 1.05))), sr.NEAR)

    def test_the_center_alone_can_spoil_a_pair(self):
        a = sr.measured(entry(box=((4, -1, -1), (6, 1, 1))), 8.0, (5, 0.5, 0), YZ)
        b = sr.measured(entry(box=((-6, -1, -1), (-4, 1, 1))), 8.0, (-5, 0, 0), YZ)
        self.assertEqual(self.status(a, b), sr.DIFFERENT)

    def test_a_tube_cut_short_reads_as_its_lost_length(self):
        # 100 long and 0.1 shorter, with the box held equal: 0.1 % of the volume over a size of about 100.
        half = (50.0, 1.0, 1.0)
        a, b = body((60, 0, 0), half=half, volume=100.0), body((-60, 0, 0), half=half, volume=99.9)
        pair = pairs_of([a, b])[0][0]
        self.assertAlmostEqual(pair["deviation"]["volume_length"], 0.1, places=2)
        self.assertEqual(pair["status"], sr.NEAR)


# -- Confirming ------------------------------------------------------------------


class CheckReflectedTests(unittest.TestCase):
    SOURCE = {"volume": 10.0, "mirror_box": ((-4, 0, 0), (-1, 1, 1)), "exact_box": True}

    def test_a_good_copy_passes(self):
        sr.check_reflected(self.SOURCE["mirror_box"], 10.0, self.SOURCE)

    def test_a_body_turned_inside_out_fails(self):
        with self.assertRaises(sr.ReflectionFailed):
            sr.check_reflected(self.SOURCE["mirror_box"], -10.0, self.SOURCE)

    def test_a_small_volume_drift_passes(self):
        sr.check_reflected(self.SOURCE["mirror_box"], 10.0 * (1 - 0.00034), self.SOURCE)

    def test_the_volume_limit_is_relative(self):
        sr.check_reflected(self.SOURCE["mirror_box"], 10.0 * (1 - 0.0009), self.SOURCE)
        with self.assertRaises(sr.ReflectionFailed):
            sr.check_reflected(self.SOURCE["mirror_box"], 10.0 * (1 - 0.0011), self.SOURCE)

    def test_a_scaled_copy_fails(self):
        with self.assertRaises(sr.ReflectionFailed):
            sr.check_reflected(self.SOURCE["mirror_box"], 10.0 * 1.01, self.SOURCE)

    def test_no_volume_fails(self):
        with self.assertRaises(sr.ReflectionFailed):
            sr.check_reflected(self.SOURCE["mirror_box"], None, self.SOURCE)

    def test_a_copy_that_did_not_move_fails(self):
        with self.assertRaises(sr.ReflectionFailed):
            sr.check_reflected(((1, 0, 0), (4, 1, 1)), 10.0, self.SOURCE)

    def test_an_oblique_plane_expects_no_exact_box(self):
        sr.check_reflected(((1, 0, 0), (4, 1, 1)), 10.0, {**self.SOURCE, "exact_box": False})


class ApplyBooleansTests(unittest.TestCase):
    def pair(self, status):
        return {"status": status, "basis": sr.BY_NUMBERS, "geometry": None}

    def parts(self, difference, trusted=True):
        return {"overlap": 9.0, "a_only": {"volume": difference}, "b_only": {"volume": 0.0}, "trusted": trusted}

    def test_the_same_solid_is_a_true_mirror_whatever_the_box_said(self):
        pair = self.pair(sr.NEAR)
        self.assertEqual(sr.apply_booleans(pair, self.parts(0.0), 0.01), "same")
        self.assertEqual((pair["status"], pair["basis"]), (sr.TRUE_MIRROR, sr.BY_BOOLEANS))

    def test_a_difference_the_numbers_missed_caps_it_at_near(self):
        pair = self.pair(sr.TRUE_MIRROR)
        self.assertEqual(sr.apply_booleans(pair, self.parts(1.0), 0.01), "different")
        self.assertEqual((pair["status"], pair["basis"]), (sr.NEAR, sr.BY_BOOLEANS))

    def test_a_thin_wall_does_not_turn_near_into_different(self):
        pair = self.pair(sr.NEAR)
        sr.apply_booleans(pair, self.parts(5.0), 0.01)
        self.assertEqual(pair["status"], sr.NEAR)

    def test_booleans_that_disagree_change_nothing(self):
        pair = self.pair(sr.TRUE_MIRROR)
        self.assertEqual(sr.apply_booleans(pair, self.parts(1.0, trusted=False), 0.01), "unknown")
        self.assertEqual((pair["status"], pair["basis"]), (sr.TRUE_MIRROR, sr.BY_NUMBERS))
        self.assertIsNone(pair["geometry"]["difference"])


class DifferenceLimitTests(unittest.TestCase):
    def test_a_tube_sliding_within_tolerance_stays_inside_the_limit(self):
        a = {"volume": 20.0, "size": 30.0}
        limit = sr.difference_limit({"a": a, "b": a}, 0.01)
        self.assertAlmostEqual(limit, 20.0 * 0.01 / 30.0)


class ConfirmTests(unittest.TestCase):
    class Budget:
        def __init__(self, after=None):
            self.after, self.checks = after, 0

        def elapsed(self):
            return 0.0

        def remaining(self):
            return 100.0

        def expired(self):
            self.checks += 1
            return self.after is not None and self.checks > self.after

    def pairs(self, count=2):
        bodies = []
        for i in range(count):
            bodies += [body((5 + 3 * i, 0, 0), name=f"R{i}"), body((-5 - 3 * i, 0, 0), name=f"L{i}")]
        return pairs_of(bodies)[0]

    def copies(self, *_):
        cube = {(x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)}  # the volume of body()'s default
        return SimpleNamespace(fresh=lambda side: Voxels(cube), combine=Manager().booleanOperation)

    def test_differing_pairs_are_not_worth_booleans(self):
        pair = pairs_of([body((5, 0, 0)), body((-5, 1, 0))])[0][0]
        self.assertEqual(pair["status"], sr.DIFFERENT)
        block = sr.confirm_pairs([pair], lambda p: self.fail("no copies needed"), self.Budget(), TOL)
        self.assertEqual((block["checked"], block["not_reached"]), (0, 0))

    def test_the_closest_go_first_and_the_budget_leaves_the_rest_on_numbers(self):
        close = body((5, 0, 0), name="close")
        pairs = pairs_of([close, body((-5, 0, 0)), body((9, 0, 0), name="off"), body((-9.1, 0, 0))])[0]
        seen = []

        def copies(pair):
            seen.append(pair["a"]["body"])
            return self.copies()

        block = sr.confirm_pairs(list(reversed(pairs)), copies, self.Budget(after=1), TOL)
        self.assertEqual(seen, ["close"])
        self.assertEqual((block["checked"], block["not_reached"]), (1, 1))
        self.assertEqual(pairs[1]["basis"], sr.BY_NUMBERS)

    def test_no_pair_reflecting_leaves_everything_on_numbers_and_says_why(self):
        def refuse(pair):
            raise sr.ReflectionFailed("no")

        pairs = self.pairs()
        block = sr.confirm_pairs(pairs, refuse, self.Budget(), TOL)
        self.assertEqual((block["available"], block["method"], block["reason"]), (False, sr.BY_NUMBERS, "no"))
        self.assertEqual((block["checked"], block["reflection_failed"], block["not_reached"]), (0, 2, 0))

    def test_a_failed_reflection_costs_only_that_pair(self):
        calls = []

        def copies(pair):
            calls.append(pair)
            if len(calls) == 2:
                raise sr.ReflectionFailed("bad copy")
            return self.copies()

        pairs = self.pairs(3)
        block = sr.confirm_pairs(pairs, copies, self.Budget(), TOL)
        self.assertEqual((block["available"], block["method"], block["reason"]), (True, sr.BY_BOOLEANS, "bad copy"))
        self.assertEqual((block["checked"], block["same"], block["reflection_failed"], block["not_reached"]),
                         (2, 2, 1, 0))
        self.assertEqual(calls[1]["geometry"], {"verdict": "unknown", "error": "bad copy"})
        self.assertEqual(calls[1]["basis"], sr.BY_NUMBERS)
        self.assertEqual([p["basis"] for p in (calls[0], calls[2])], [sr.BY_BOOLEANS] * 2)

    def test_a_failed_reflection_first_does_not_end_the_run(self):
        calls = []

        def copies(pair):
            calls.append(pair)
            if len(calls) == 1:
                raise sr.ReflectionFailed("bad copy")
            return self.copies()

        block = sr.confirm_pairs(self.pairs(), copies, self.Budget(), TOL)
        self.assertTrue(block["available"])
        self.assertEqual((block["checked"], block["reflection_failed"]), (1, 1))

    def test_failed_reflections_count_toward_the_budget(self):
        clock = [0.0]

        def refuse(pair):
            clock[0] += 8.0
            raise sr.ReflectionFailed("no")

        budget = rules.Budget(10.0, lambda: clock[0])
        block = sr.confirm_pairs(self.pairs(), refuse, budget, TOL)
        self.assertEqual((block["reflection_failed"], block["not_reached"]), (1, 1))

    def test_a_kernel_failure_costs_only_that_pair(self):
        calls = []

        def copies(pair):
            calls.append(pair)
            if len(calls) == 1:
                raise RuntimeError("boolean blew up")
            return self.copies()

        pairs = self.pairs()
        block = sr.confirm_pairs(pairs, copies, self.Budget(), TOL)
        self.assertEqual((block["unknown"], block["same"], block["checked"]), (1, 1, 2))
        self.assertEqual(calls[0]["geometry"], {"verdict": "unknown", "error": "boolean blew up"})
        self.assertEqual(calls[0]["basis"], sr.BY_NUMBERS)

    def test_a_pair_that_would_not_fit_is_not_started(self):
        clock = [0.0]

        def copies(pair):
            clock[0] += 8.0
            return self.copies()

        budget = rules.Budget(10.0, lambda: clock[0])
        block = sr.confirm_pairs(self.pairs(), copies, budget, TOL)
        self.assertEqual((block["checked"], block["not_reached"]), (1, 1))


# -- Whole runs ------------------------------------------------------------------


class RunTests(ToolCase):
    def test_a_true_mirror_is_confirmed_by_booleans(self):
        scene = Scene([Voxels(bar(), "R"), Voxels(mirrored(bar()), "L")])
        report = self.report(scene)
        self.assertEqual(report["summary"], {"bodies": 2, "pairs": 1, "true_mirror": 1, "near": 0, "different": 0,
                                             "unmatched": 0, "on_plane": 0})
        pair = report["pairs"][0]
        self.assertEqual((pair["status"], pair["basis"]), ("true_mirror", "temporary_brep"))
        self.assertEqual(pair["geometry"]["verdict"], "same")
        self.assertEqual(pair["geometry"]["symmetric_difference"], 0.0)
        self.assertTrue(report["complete"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["confirmation"]["same"], 1)

    def test_a_shape_only_booleans_can_tell_from_its_mirror(self):
        report = self.report(Scene([Voxels(CHIRAL, "R"), Voxels(OTHER_HAND, "L")]))
        pair = report["pairs"][0]
        self.assertEqual(pair["deviation"]["center_of_mass"], 0.0)
        self.assertEqual(pair["deviation"]["bbox"], 0.0)
        self.assertEqual((pair["status"], pair["basis"]), ("near", "temporary_brep"))
        self.assertEqual(pair["geometry"]["verdict"], "different")

    def test_worst_comes_first(self):
        far, near, good = bar(2, 8), bar(20, 8), bar(40, 8)
        shifted = {(x, y + 0, z) for x, y, z in mirrored(far)} | {(-11, 0, 0)}
        scene = Scene([Voxels(far, "far"), Voxels(shifted, "far2"), Voxels(good, "good"),
                       Voxels(mirrored(good), "good2"), Voxels(near, "near"),
                       Voxels(mirrored(near) | {(-29, 0, 0)}, "near2")])
        statuses = [p["status"] for p in self.report(scene, tolerance=0.05)["pairs"]]
        self.assertEqual(statuses[-1], "true_mirror")
        self.assertEqual(statuses, sorted(statuses, key=lambda s: sr.STATUS_RANK[s]))

    def test_a_body_with_no_partner_is_listed_by_side(self):
        scene = Scene([Voxels(bar(), "R"), Voxels(mirrored(bar()), "L"), Voxels(bar(20), "extra"),
                       Voxels(mirrored(bar(40)), "other")])
        report = self.report(scene)
        self.assertEqual(report["summary"]["unmatched"], 2)
        self.assertEqual([b["body_name"] for b in report["unmatched"]["positive"]], ["extra"])
        self.assertEqual([b["body_name"] for b in report["unmatched"]["negative"]], ["other"])
        self.assertEqual(report["unmatched"]["on_plane"], [])

    def test_a_centered_body_pairs_with_itself(self):
        centered = {(x, 0, 0) for x in (-2, -1, 0, 1)}
        report = self.report(Scene([Voxels(centered, "Cross")]))
        pair = report["pairs"][0]
        self.assertTrue(pair["on_plane"])
        self.assertEqual(report["summary"]["on_plane"], 1)
        self.assertEqual(pair["geometry"]["verdict"], "same")

    def test_each_occurrence_of_one_component_counts(self):
        scene = Scene(occurrences=[occurrence("Arm:1", "Arm", [Voxels(bar(), "Tube")]),
                                   occurrence("Arm:2", "Arm", [Voxels(mirrored(bar()), "Tube")]),
                                   occurrence("Arm:3", "Arm", [Voxels(bar(30), "Tube")])])
        report = self.report(scene)
        self.assertEqual(report["summary"]["bodies"], 3)
        self.assertEqual(report["pairs"][0]["a"]["occurrence_path"], "Arm:1")
        self.assertEqual(report["pairs"][0]["b"]["occurrence_path"], "Arm:2")
        self.assertEqual(report["unmatched"]["positive"][0]["occurrence_path"], "Arm:3")

    def test_a_center_read_in_component_space_is_mapped_through_the_occurrence(self):
        matrix = Matrix()
        matrix.setCell(0, 3, 100.0)
        context = SimpleNamespace(transform2=matrix)
        moved = Voxels({(100 + x, y, z) for x, y, z in bar()}, "R", context=context, offset=(100, 0, 0))
        report = self.report(Scene([moved, Voxels(mirrored({(100 + x, y, z) for x, y, z in bar()}), "L")]))
        self.assertEqual(report["pairs"][0]["deviation"]["center_of_mass"], 0.0)

    def test_hidden_bodies_are_counted_and_left_out(self):
        scene = Scene([Voxels(bar(), "R"), Voxels(mirrored(bar()), "L", visible=False)])
        report = self.report(scene)
        self.assertEqual((report["summary"]["bodies"], report["skipped"]["hidden"]), (1, 1))
        self.assertEqual(self.report(scene, include_hidden=True)["summary"]["pairs"], 1)

    def test_a_hidden_occurrence_hides_its_bodies(self):
        scene = Scene([Voxels(bar(), "R")], [occurrence("Old:1", "Old", [Voxels(mirrored(bar()), "L")], visible=False)])
        self.assertEqual(self.report(scene)["skipped"]["hidden"], 1)

    def test_component_contains_narrows_the_run(self):
        scene = Scene([Voxels(bar(), "R")], [occurrence("Arm:1", "Arm", [Voxels(mirrored(bar()), "L")])])
        report = self.report(scene, component_contains="arm")
        self.assertEqual((report["summary"]["bodies"], report["skipped"]["filtered"]), (1, 1))

    def test_nothing_to_compare_is_an_error(self):
        result = self.call(Scene([Voxels(bar(), "R", visible=False)]), plane="yz")
        self.assertTrue(result["isError"])
        self.assertIn("no solid body", result["content"][0]["text"])

    def test_max_results_caps_the_lists_not_the_summary(self):
        bodies = [Voxels(bar(10 * i), f"R{i}") for i in range(1, 4)]
        bodies += [Voxels(mirrored(bar(10 * i)), f"L{i}") for i in range(1, 4)]
        bodies += [Voxels(bar(100), "x"), Voxels(bar(120), "y")]
        report = self.report(Scene(bodies), max_results=1)
        self.assertEqual((len(report["pairs"]), report["summary"]["pairs"]), (1, 3))
        self.assertEqual((len(report["unmatched"]["positive"]), report["summary"]["unmatched"]), (1, 2))
        self.assertTrue(report["pairs_truncated"])
        self.assertTrue(report["unmatched_truncated"])

    def test_units_apply_to_lengths_and_volumes(self):
        report = self.report(Scene([Voxels(bar(), "R"), Voxels(mirrored(bar()), "L")]), units="mm")
        self.assertEqual(report["units"]["volume"], "mm^3")
        self.assertEqual(report["pairs"][0]["a"]["volume"], 3000.0)
        self.assertEqual(report["pairs"][0]["a"]["center"], [35.0, 5.0, 5.0])

    def test_the_default_tolerance_is_reported_in_the_callers_units(self):
        scene = Scene([Voxels(bar(), "R")])
        self.assertEqual(self.report(scene, units="in")["tolerance"]["true_mirror"], 0.01)

    def test_a_plane_off_the_origin_reflects_copies_there_too(self):
        cells = {(4 + i, 0, 0) for i in range(2)}
        other = {(2 - x, y, z) for x, y, z in cells}  # across x = 1.5
        report = self.report(Scene([Voxels(cells, "R"), Voxels(other, "L")]), plane=None, point=[1.5, 0, 0],
                             normal=[1, 0, 0])
        self.assertEqual(report["pairs"][0]["geometry"]["verdict"], "same")
        self.assertEqual(report["plane"]["source"], "point_normal")

    def test_an_unreadable_center_uses_the_box_and_says_so(self):
        class Blind(Voxels):
            physicalProperties = None

        report = self.report(Scene([Blind(bar(), "R"), Blind(mirrored(bar()), "L")]))
        self.assertEqual(report["summary"]["pairs"], 1)
        self.assertIn("no readable center of mass", report["warnings"][0])

    def test_a_body_with_no_volume_makes_the_run_incomplete(self):
        class Empty(Voxels):
            volume = None

        report = self.report(Scene([Voxels(bar(), "R"), Empty(mirrored(bar()), "L")]))
        self.assertEqual(report["skipped"]["unmeasured"], 1)
        self.assertFalse(report["complete"])

    def test_nothing_is_written_to_the_design(self):
        scene = Scene([Voxels(bar(), "R"), Voxels(mirrored(bar()), "L")])
        before = [set(b.cells) for b in scene.rootComponent.bRepBodies._members]
        self.report(scene)
        self.assertEqual([b.cells for b in scene.rootComponent.bRepBodies._members], before)
        self.assertGreater(self.manager.copies, 0)


class FallbackTests(ToolCase):
    def scene(self):
        return Scene([Voxels(CHIRAL, "R"), Voxels(OTHER_HAND, "L")])

    def assert_fell_back(self, report):
        block = report["confirmation"]
        self.assertEqual((block["available"], block["method"], block["checked"]), (False, "numbers", 0))
        self.assertEqual(report["pairs"][0]["basis"], "numbers")
        self.assertEqual(report["pairs"][0]["status"], "true_mirror")
        self.assertIn("rest on numbers", report["warnings"][-1])

    def test_a_refused_transform_falls_back_to_numbers_and_says_so(self):
        report = self.report(self.scene(), Manager(reflects=False))
        self.assert_fell_back(report)
        self.assertIn("refused", report["confirmation"]["reason"])

    def test_a_raising_transform_falls_back(self):
        report = self.report(self.scene(), Manager(reflects="raises"))
        self.assert_fell_back(report)
        self.assertIn("non-rigid", report["confirmation"]["reason"])

    def test_a_transform_that_did_nothing_is_caught(self):
        report = self.report(self.scene(), Manager(reflects="ignored"))
        self.assert_fell_back(report)
        self.assertIn("mirrored box", report["confirmation"]["reason"])

    def test_one_body_that_will_not_reflect_leaves_the_others_confirmed(self):
        class Drifted(Manager):
            def transform(self, body, matrix):
                moved = super().transform(body, matrix)
                if body.name == "R":
                    body.cells = set(sorted(body.cells)[:1])
                return moved

        scene = Scene([Voxels(bar(), "R"), Voxels(mirrored(bar()), "L"),
                       Voxels(bar(x=12), "R2"), Voxels(mirrored(bar(x=12)), "L2")])
        report = self.report(scene, Drifted())
        block = report["confirmation"]
        self.assertEqual((block["available"], block["checked"], block["reflection_failed"]), (True, 1, 1))
        self.assertEqual(sorted(p["basis"] for p in report["pairs"]), ["numbers", "temporary_brep"])
        self.assertIn("1 pair(s) could not be reflected", report["warnings"][-1])

    def test_a_failed_boolean_leaves_that_pair_on_numbers(self):
        report = self.report(Scene([Voxels(bar(), "R"), Voxels(mirrored(bar()), "L")]), Manager(fail_on="R"))
        self.assertTrue(report["confirmation"]["available"])
        self.assertEqual(report["confirmation"]["unknown"], 1)
        self.assertEqual(report["pairs"][0]["basis"], "numbers")
        self.assertEqual(report["pairs"][0]["geometry"]["verdict"], "unknown")


class BudgetTests(unittest.TestCase):
    def test_a_budget_spent_while_reading_says_unmatched_may_be_wrong(self):
        clock = [0.0]

        def entries():
            for x in (5, -5):
                clock[0] += 6.0
                yield entry(box=box_around((x, 0, 0), (1, 1, 1)))

        budget = rules.Budget(5.0, lambda: clock[0])
        report = sr.run(_after_first(entries, clock), YZ, options(), budget, lambda e: (8.0, None), None)
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["bodies"], 1)
        self.assertIn("reading", report["warnings"][0])

    def test_pairing_gets_a_grace_past_the_budget_then_stops(self):
        clock = [0.0]
        budget = rules.Budget(5.0, lambda: clock[0])
        budget.expired = lambda: False  # reading is over; only pairing's stop is under test
        clock[0] = 5.0 + sr.PAIRING_GRACE_S + 1.0
        bodies = [entry(box=box_around((x, 0, 0), (1, 1, 1))) for x in (5, -5)]
        report = sr.run(iter(bodies), YZ, options(), budget, lambda e: (8.0, None), None)
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["pairs"], 0)
        self.assertIn("pairing", report["warnings"][0])


def _after_first(entries, clock):
    """Yield the first entry on a fresh clock; the budget then runs out before the second."""
    source = entries()
    first = next(source)
    clock[0] = 0.0
    yield first
    clock[0] = 6.0
    yield from source


class HandlerTests(ToolCase):
    def test_bad_arguments_never_reach_fusion(self):
        with mock.patch.object(sr, "get_app") as get_app:
            result = sr.symmetry_report({"plane": "top"})
        self.assertTrue(result["isError"])
        get_app.assert_not_called()

    def test_no_design(self):
        result = self.call(None, plane="yz")
        self.assertIn("not a Fusion design", result["content"][0]["text"])

    def test_a_bad_token_is_an_error_with_a_reason(self):
        result = self.call(Scene([Voxels(bar())]), plane_token="nope")
        self.assertTrue(result["isError"])
        self.assertIn("plane_token", result["content"][0]["text"])

    def test_an_unexpected_failure_is_reported(self):
        class Broken(Scene):
            @property
            def rootComponent(self):
                raise RuntimeError("gone")

            @rootComponent.setter
            def rootComponent(self, value):
                pass

        result = self.call(Broken(), plane="yz")
        self.assertTrue(result["isError"])
        self.assertIn("gone", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
