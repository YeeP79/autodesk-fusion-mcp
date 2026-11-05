"""sketch_profile: pure outline geometry, argument validation and loop building with fakes."""

import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import sketch_geometry as geo
from fusion_bridge import sketch_profile, tool_surface, value_builders
import adsk.core

SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]


def problems_of(fn, *args):
    with self_raises() as ctx:
        fn(*args)
    return ctx.exception.problems


class self_raises:
    def __enter__(self):
        self.exception = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError("expected GeometryError")
        if not issubclass(exc_type, geo.GeometryError):
            return False
        self.exception = exc
        return True


class UnitsAndParsingTests(unittest.TestCase):
    def test_unit_factors(self):
        self.assertEqual(geo.unit_factor("in"), 2.54)
        self.assertEqual(geo.unit_factor("cm"), 1.0)
        self.assertEqual(geo.unit_factor("mm"), 0.1)
        for bad in ("inch", None, "MM"):
            with self.subTest(units=bad), self.assertRaises(geo.GeometryError):
                geo.unit_factor(bad)

    def test_points_converted_to_cm(self):
        points, warnings = geo.parse_points([[0, 0], [1, 0], [1, 1]], 2, 2.54)
        self.assertEqual(points[2], (2.54, 2.54))
        self.assertEqual(warnings, [])

    def test_explicit_closing_point_dropped_with_warning(self):
        points, warnings = geo.parse_points([[0, 0], [1, 0], [1, 1], [0, 0]], 2, 1.0)
        self.assertEqual(len(points), 3)
        self.assertEqual(len(warnings), 1)

    def test_bad_points_refused(self):
        cases = ([[0, 0], [1]], [[0, 0], [1, float("nan")], [2, 2]], [[0, 0], [True, 1], [2, 2]],
                 [[0, 0], ["1", 1], [2, 2]], "nope", [[0, 0]], [[0, 0, 0], [1, 0, 0], [1, 1, 0]])
        for bad in cases:
            with self.subTest(points=bad), self.assertRaises(geo.GeometryError):
                geo.parse_points(bad, 2, 1.0)

    def test_point_limit(self):
        with self.assertRaises(geo.GeometryError):
            geo.parse_points([[i, i * i] for i in range(geo.MAX_POINTS + 1)], 2, 1.0)

    def test_arc_specs(self):
        arcs = geo.parse_arcs([{"index": 1, "bulge": 0.5}, {"index": 2, "mid_point": [1, 3]}], 4, 2, 10.0)
        self.assertEqual(arcs, {1: {"bulge": 0.5}, 2: {"mid": (10.0, 30.0)}})
        self.assertEqual(geo.parse_arcs(None, 4, 2, 1.0), {})

    def test_bad_arcs_refused(self):
        cases = ([{"index": 4, "bulge": 1}], [{"index": -1, "bulge": 1}], [{"index": 0}],
                 [{"index": 0, "bulge": 1, "mid_point": [0, 1]}], [{"index": 0, "bulge": 0}],
                 [{"index": 0, "bulge": 1}, {"index": 0, "bulge": 2}], [{"index": True, "bulge": 1}],
                 [{"index": 0, "bulge": float("inf")}], [{"index": 0, "mid_point": [1]}],
                 [{"index": 0, "bulge": 1, "radius": 2}], ["arc"])
        for bad in cases:
            with self.subTest(arcs=bad), self.assertRaises(geo.GeometryError):
                geo.parse_arcs(bad, 4, 2, 1.0)


class ArcMathTests(unittest.TestCase):
    def assertPoint(self, actual, expected):
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e, places=9)

    def test_bulge_one_is_semicircle_on_the_right(self):
        self.assertPoint(geo.bulge_to_mid((0, 0), (2, 0), 1.0), (1, -1))
        self.assertPoint(geo.bulge_to_mid((0, 0), (2, 0), -1.0), (1, 1))

    def test_bulge_quarter_circle(self):
        bulge = math.tan(math.radians(90) / 4)
        mid = geo.bulge_to_mid((1, 0), (0, 1), bulge)
        # Counterclockwise quarter of the unit circle about the origin.
        self.assertPoint(mid, (math.sqrt(0.5), math.sqrt(0.5)))
        self.assertAlmostEqual(geo.arc_sweep((1, 0), mid, (0, 1)), math.radians(90), places=9)

    def test_bulge_sign_matches_sweep_direction(self):
        for bulge in (0.2, 1.0, 3.0, -0.2, -1.0, -3.0):
            mid = geo.bulge_to_mid((0, 0), (4, 0), bulge)
            sweep = geo.arc_sweep((0, 0), mid, (4, 0))
            with self.subTest(bulge=bulge):
                self.assertAlmostEqual(sweep, 4 * math.atan(bulge), places=9)

    def test_zero_chord_refused(self):
        with self.assertRaises(geo.GeometryError):
            geo.bulge_to_mid((1, 1), (1, 1), 1.0)

    def test_circle_through(self):
        (cx, cy), r = geo.circle_through((1, 0), (0, 1), (-1, 0))
        self.assertPoint((cx, cy, r), (0, 0, 1))
        self.assertIsNone(geo.circle_through((0, 0), (1, 1), (2, 2)))

    def test_tessellation_stays_on_circle_and_ends_exactly(self):
        pts = geo.tessellate_arc((1, 0), (0, -1), (-1, 0))  # Clockwise half circle through the bottom.
        self.assertEqual(pts[-1], (-1, 0))
        self.assertGreaterEqual(len(pts), 18)
        for p in pts:
            self.assertAlmostEqual(math.hypot(*p), 1.0, places=9)
            self.assertLessEqual(p[1], 1e-12)


class PredicateTests(unittest.TestCase):
    def test_segments_touch(self):
        self.assertTrue(geo.segments_touch((0, 0), (2, 2), (0, 2), (2, 0)))     # Proper cross.
        self.assertTrue(geo.segments_touch((0, 0), (2, 0), (1, 0), (1, 5)))     # T touch.
        self.assertTrue(geo.segments_touch((0, 0), (2, 0), (1, 0), (3, 0)))     # Collinear overlap.
        self.assertFalse(geo.segments_touch((0, 0), (2, 0), (0, 1), (2, 1)))    # Parallel apart.
        self.assertFalse(geo.segments_touch((0, 0), (1, 0), (2, 0), (3, 0)))    # Collinear apart.
        self.assertFalse(geo.segments_touch((0, 0), (1, 1), (2, 0), (3, -5)))

    def test_polygon_area_sign(self):
        self.assertEqual(geo.polygon_area(SQUARE), 4.0)
        self.assertEqual(geo.polygon_area(SQUARE[::-1]), -4.0)

    def test_point_in_loops_with_hole(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
        self.assertTrue(geo.point_in_loops((1, 1), [outer, hole]))
        self.assertFalse(geo.point_in_loops((5, 5), [outer, hole]))
        self.assertFalse(geo.point_in_loops((11, 5), [outer, hole]))


class ValidateOutlineTests(unittest.TestCase):
    def test_square_is_valid(self):
        mids, warnings = geo.validate_outline(SQUARE, {})
        self.assertEqual((mids, warnings), ({}, []))

    def test_bowtie_refused(self):
        problems = problems_of(geo.validate_outline, [(0, 0), (2, 2), (2, 0), (0, 2)], {})
        self.assertEqual(len(problems), 1)
        self.assertIn("segment 0", problems[0])
        self.assertIn("segment 2", problems[0])
        self.assertIn("crosses", problems[0])

    def test_figure_eight_touching_at_vertex_refused(self):
        # Two triangles sharing the point (2, 1) only as a touch of a vertex and a segment.
        outline = [(0, 0), (2, 1), (4, 0), (4, 2), (2, 1.0), (0, 2)]
        problems = problems_of(geo.validate_outline, outline, {})
        self.assertTrue(any("crosses or touches" in p for p in problems))

    def test_duplicate_consecutive_point(self):
        problems = problems_of(geo.validate_outline, [(0, 0), (2, 0), (2, 0), (0, 2)], {})
        self.assertIn("identical", problems[0])

    def test_zero_length_segment(self):
        problems = problems_of(geo.validate_outline, [(0, 0), (2, 0), (2, 1e-7), (0, 2)], {})
        self.assertIn("zero length", problems[0])

    def test_all_collinear_encloses_no_area(self):
        problems = problems_of(geo.validate_outline, [(0, 0), (1, 0), (3, 0)], {})
        self.assertTrue(any("no area" in p or "doubles back" in p for p in problems))

    def test_spike_doubles_back(self):
        problems = problems_of(geo.validate_outline, [(0, 0), (4, 0), (4, 2), (4, 5), (4, 3), (0, 3)], {})
        self.assertTrue(any("doubles back" in p for p in problems))

    def test_two_lines_need_three_points(self):
        problems = problems_of(geo.validate_outline, [(0, 0), (1, 0)], {})
        self.assertIn("at least 3 points", problems[0])

    def test_redundant_collinear_vertex_warns(self):
        _, warnings = geo.validate_outline([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)], {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("point 1", warnings[0])

    def test_d_shape_from_two_points_and_an_arc(self):
        mids, _ = geo.validate_outline([(0, 0), (2, 0)], {1: {"bulge": 1.0}})
        self.assertIn(1, mids)

    def test_arc_bulging_through_the_outline_refused(self):
        # Segment 0 bows up into a 4 x 1 rectangle as a semicircle and crosses the top side.
        problems = problems_of(geo.validate_outline, [(0, 0), (4, 0), (4, 1), (0, 1)], {0: {"bulge": -1.0}})
        self.assertTrue(any("segment 0" in p and "segment 2" in p for p in problems))

    def test_arc_wrapping_around_the_outline_is_valid(self):
        # A 286 degree arc on the bottom edge encloses the square but touches it only at its ends.
        geo.validate_outline(SQUARE, {0: {"bulge": -3.0}})

    def test_outward_arc_is_valid(self):
        mids, _ = geo.validate_outline(SQUARE, {0: {"bulge": 0.5}})
        self.assertLess(mids[0][1], 0)  # Bottom edge bows downward, away from the square.

    def test_straight_arc_refused(self):
        problems = problems_of(geo.validate_outline, SQUARE, {0: {"mid": (1.0, 0.0)}})
        self.assertIn("effectively straight", problems[0])

    def test_many_points_is_fast_enough(self):
        n = 400
        circle = [(math.cos(2 * math.pi * k / n), math.sin(2 * math.pi * k / n)) for k in range(n)]
        geo.validate_outline(circle, {})


class ModelSpaceTests(unittest.TestCase):
    def test_root_frames_are_right_handed_about_plane_normals(self):
        normals = {"xy": (0, 0, 1), "xz": (0, 1, 0), "yz": (1, 0, 0)}
        for plane, (u, v) in geo.ROOT_PLANE_FRAMES.items():
            with self.subTest(plane=plane):
                self.assertEqual(geo.cross(u, v), normals[plane])

    def test_xz_frame_maps_model_minus_z_to_sketch_y(self):
        flat, off = geo.project_to_frame([(3, 0, -5)], (0, 0, 0), *geo.ROOT_PLANE_FRAMES["xz"])
        self.assertEqual(flat, [(3, 5)])
        self.assertEqual(off, [0])

    def test_off_plane_points_refused_in_user_units(self):
        points = [(0, 0, 0), (2.54, 0, 0), (2.54, 0.254, 2.54)]
        problems = problems_of(geo.flatten_model_input, points, {}, "xz", 2.54, "in")
        self.assertEqual(len(problems), 1)
        self.assertIn("points[2] is 0.1 in off", problems[0])

    def test_mid_point_checked_against_plane(self):
        points = [(0, 0, 0), (2, 0, 0), (2, 0, 2)]
        problems = problems_of(geo.flatten_model_input, points, {0: {"mid": (1, 1, -1)}}, "xz")
        self.assertIn("arc on segment 0 mid_point", problems[0])

    def test_xz_input_flattens_to_valid_outline(self):
        points = [(0, 0, 0), (2, 0, 0), (2, 0, -2), (0, 0, -2)]
        flat, arcs = geo.flatten_model_input(points, {1: {"mid": (3, 0, -1)}}, "xz")
        self.assertEqual(flat, [(0, 0), (2, 0), (2, 2), (0, 2)])
        self.assertEqual(arcs, {1: {"mid": (3, 1)}})
        geo.validate_outline(flat, arcs)

    def test_arbitrary_plane_uses_newell_normal(self):
        # Square tilted 45 degrees about x, offset in z.
        s = math.sqrt(0.5)
        points = [(0, 0, 5), (2, 0, 5), (2, 2 * s, 5 + 2 * s), (0, 2 * s, 5 + 2 * s)]
        normal = geo.newell_normal(points)
        self.assertAlmostEqual(abs(normal[1]), s, places=9)
        self.assertAlmostEqual(abs(normal[2]), s, places=9)
        flat, _ = geo.flatten_model_input(points, {}, None)
        self.assertAlmostEqual(abs(geo.polygon_area(flat)), 4.0, places=9)

    def test_arbitrary_plane_refuses_non_coplanar(self):
        points = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 1)]
        with self.assertRaises(geo.GeometryError):
            geo.flatten_model_input(points, {}, None)

    def test_frame_from_normal_is_orthonormal(self):
        for normal in ((0, 0, 1), (1, 0, 0), (0, -1, 0), (s := math.sqrt(1 / 3), s, s)):
            u, v = geo.frame_from_normal(normal)
            with self.subTest(normal=normal):
                self.assertAlmostEqual(geo.dot(u, v), 0, places=12)
                for a, b in zip(geo.cross(u, v), normal):
                    self.assertAlmostEqual(a, b, places=12)


class ParseRequestTests(unittest.TestCase):
    BASE = {"action": "closed_polyline", "units": "in", "sketch_plane": "xy",
            "points": [[0, 0], [2, 0], [2, 2], [0, 2]]}

    def problems(self, **changes):
        args = {**self.BASE, **changes}
        for key, value in list(args.items()):
            if value is None:
                del args[key]
        return problems_of(sketch_profile.parse_request, args)

    def test_valid_request(self):
        request = sketch_profile.parse_request(self.BASE)
        self.assertEqual(request["points"][2], (5.08, 5.08))
        self.assertEqual(request["mids"], {})

    def test_argument_errors(self):
        for changes in ({"action": "draw"}, {"units": "ft"}, {"units": None}, {"space": "world"},
                        {"sketch_plane": None}, {"plane_entity_token": "abc"}, {"sketch_plane": "zx"},
                        {"name": ""}, {"point": [1, 1]}, {"points": [[0, 0, 0], [1, 0, 0], [1, 1, 0]]}):
            with self.subTest(changes=changes):
                self.assertTrue(self.problems(**changes))

    def test_model_space_needs_three_coordinates_and_on_plane(self):
        self.assertTrue(self.problems(space="model"))
        problems = self.problems(space="model", sketch_plane="xz",
                                 points=[[0, 0, 0], [1, 0, 0], [1, 1, 1], [0, 0, 1]])
        self.assertIn("off the sketch plane", problems[0])

    def test_profiles_request(self):
        request = sketch_profile.parse_request({"action": "profiles", "units": "mm", "name": "S", "point": [10, 0]})
        self.assertEqual(request["point"], (1.0, 0.0))
        for args in ({"action": "profiles", "units": "mm"},
                     {"action": "profiles", "units": "mm", "name": "S", "sketch_entity_token": "t"},
                     {"action": "profiles", "units": "mm", "name": "S", "points": [[0, 0]]}):
            with self.subTest(args=args), self.assertRaises(geo.GeometryError):
                sketch_profile.parse_request(args)

    def test_invalid_outline_never_touches_fusion(self):
        args = {**self.BASE, "points": [[0, 0], [2, 2], [2, 0], [0, 2]]}
        with patch.object(sketch_profile, "get_app", side_effect=AssertionError("Fusion touched")):
            result = sketch_profile.sketch_profile(args)
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("nothing was created", payload["error"])
        self.assertIn("crosses", payload["problems"][0])


class CircleParsingTests(unittest.TestCase):
    def test_circles_converted_to_cm(self):
        circles = geo.parse_circles([{"center": [1, 2], "diameter": 0.5}], 2, 2.54)
        self.assertEqual(circles, [{"center": (2.54, 5.08), "radius": 0.635}])

    def test_bad_circles_refused(self):
        good = {"center": [0, 0], "diameter": 1}
        cases = (None, [], "nope", ["circle"], [{"center": [0, 0]}], [{"diameter": 1}],
                 [{**good, "diameter": 0}], [{**good, "diameter": -1}], [{**good, "diameter": True}],
                 [{**good, "diameter": "1"}], [{**good, "diameter": float("nan")}],
                 [{**good, "diameter": float("inf")}], [{**good, "diameter": 1e-6}],
                 [{**good, "center": [0]}], [{**good, "center": [0, 0, 0]}], [{**good, "center": [0, None]}],
                 [{**good, "radius": 2}], [good] * (geo.MAX_POINTS + 1))
        for bad in cases:
            with self.subTest(circles=bad if not isinstance(bad, list) or len(bad) < 5 else "too many"):
                with self.assertRaises(geo.GeometryError):
                    geo.parse_circles(bad, 2, 1.0)

    def test_every_bad_circle_is_reported(self):
        problems = problems_of(geo.parse_circles, [{"center": [0, 0], "diameter": 0}, {"center": [1], "diameter": 1}],
                               2, 1.0)
        self.assertEqual(len(problems), 2)


class ValidateCirclesTests(unittest.TestCase):
    @staticmethod
    def circle(x, y, r):
        return {"center": (x, y), "radius": r}

    def test_apart_and_nested_are_valid(self):
        geo.validate_circles([self.circle(0, 0, 1), self.circle(5, 0, 1)])
        geo.validate_circles([self.circle(0, 0, 2), self.circle(0, 0, 1)])  # Tube wall.
        geo.validate_circles([self.circle(0, 0, 2), self.circle(0.5, 0, 1)])  # Eccentric, inside.

    def test_duplicate_refused(self):
        problems = problems_of(geo.validate_circles, [self.circle(1, 1, 1), self.circle(1, 1, 1)])
        self.assertEqual(problems, ["circles 0 and 1 are the same circle"])

    def test_crossing_and_touching_refused(self):
        for second in (self.circle(1, 0, 1), self.circle(2, 0, 1), self.circle(1, 0, 2)):
            with self.subTest(second=second):
                problems = problems_of(geo.validate_circles, [self.circle(0, 0, 1), second])
                self.assertEqual(problems, ["circles 0 and 1 cross or touch"])

    def test_gap_just_over_tolerance_is_valid(self):
        geo.validate_circles([self.circle(0, 0, 1), self.circle(2 + 2 * geo.LENGTH_TOL_CM, 0, 1)])

    def test_flatten_refuses_off_plane_center_in_user_units(self):
        circles = [{"center": (0.0, 0.0, 0.0), "radius": 1.0}, {"center": (5.0, 2.54, 0.0), "radius": 1.0}]
        problems = problems_of(geo.flatten_circles, circles, "xz", 2.54, "in")
        self.assertEqual(problems, ["circles[1].center is 1 in off the sketch plane"])

    def test_flatten_uses_the_plane_frame(self):
        flat = geo.flatten_circles([{"center": (3.0, 0.0, -5.0), "radius": 1.0}], "xz")
        self.assertEqual(flat, [{"center": (3.0, 5.0), "radius": 1.0}])


class CircleRequestTests(unittest.TestCase):
    BASE = {"action": "circles", "units": "in", "sketch_plane": "xy",
            "circles": [{"center": [1, 1], "diameter": 0.5}, {"center": [3, 1], "diameter": 0.5}]}

    def test_valid_request(self):
        request = sketch_profile.parse_request(self.BASE)
        self.assertEqual(request["circles"][1], {"center": (7.62, 2.54), "radius": 0.635})

    def test_argument_errors(self):
        for changes in ({"circles": []}, {"points": [[0, 0], [1, 0], [1, 1]]}, {"arcs": []}, {"point": [1, 1]},
                        {"plane_entity_token": "abc"}, {"sketch_plane": "zx"}, {"space": "model"}):
            with self.subTest(changes=changes), self.assertRaises(geo.GeometryError):
                sketch_profile.parse_request({**self.BASE, **changes})
        missing = {k: v for k, v in self.BASE.items() if k != "sketch_plane"}
        with self.assertRaises(geo.GeometryError):
            sketch_profile.parse_request(missing)

    def test_circles_key_belongs_to_the_circles_action(self):
        args = {"action": "closed_polyline", "units": "in", "sketch_plane": "xy", "circles": [],
                "points": [[0, 0], [2, 0], [2, 2]]}
        self.assertIn("only used by circles", problems_of(sketch_profile.parse_request, args)[0])
        args = {"action": "profiles", "units": "in", "name": "S", "circles": []}
        self.assertIn("only used by circles", problems_of(sketch_profile.parse_request, args)[0])

    def test_model_space_checked_on_a_root_plane(self):
        args = {**self.BASE, "space": "model", "sketch_plane": "xz",
                "circles": [{"center": [1, 1, 1], "diameter": 0.5}]}
        self.assertIn("off the sketch plane", problems_of(sketch_profile.parse_request, args)[0])

    def test_crossing_circles_never_touch_fusion(self):
        args = {**self.BASE, "circles": [{"center": [0, 0], "diameter": 2}, {"center": [1, 0], "diameter": 2}]}
        with patch.object(sketch_profile, "get_app", side_effect=AssertionError("Fusion touched")):
            result = sketch_profile.sketch_profile(args)
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("nothing was created", payload["error"])
        self.assertIn("cross or touch", payload["problems"][0])


# -- Fakes for the Fusion-facing builder ---------------------------------------------------


class FakePoint:
    def __init__(self, sketch, x, y):
        self.sketch, self.geometry = sketch, SimpleNamespace(x=x, y=y, z=0.0)
        self.curves = []

    @property
    def connectedEntities(self):
        return SimpleNamespace(count=len(self.curves))

    def merge(self, other):
        for curve in other.curves:
            if curve.startSketchPoint is other:
                curve.startSketchPoint = self
            if curve.endSketchPoint is other:
                curve.endSketchPoint = self
            self.curves.append(curve)
        self.sketch.merges += 1
        return True


class FakeCurve:
    def __init__(self, start, end):
        self.startSketchPoint, self.endSketchPoint = start, end
        start.curves.append(self)
        end.curves.append(self)


class FakeCircle:
    def __init__(self, sketch, center, radius):
        self.centerSketchPoint = FakePoint(sketch, center[0], center[1])
        self.centerSketchPoint.curves.append(self)
        self.center_z, self.radius = center[2], radius
        self.entityToken = f"circle-{len(sketch.circles)}"


class FakeSketch:
    """Arcs mimic Fusion: always counterclockwise, so a clockwise request swaps ends."""

    def __init__(self):
        self.merges = 0
        self.curves = []
        self.circles = []
        sketch = self

        def point(ref):
            return ref if isinstance(ref, FakePoint) else FakePoint(sketch, ref[0], ref[1])

        def add_line(start, end):
            curve = FakeCurve(point(start), point(end))
            sketch.curves.append(curve)
            return curve

        def add_arc(start, mid, end):
            def xy(ref):
                return (ref.geometry.x, ref.geometry.y) if isinstance(ref, FakePoint) else ref[:2]
            ccw = geo.arc_sweep(xy(start), mid[:2], xy(end)) > 0
            # Fusion ignores passed SketchPoints' roles for clockwise arcs: fresh, swapped ends.
            first, last = (point(start), point(end)) if ccw else (point(tuple(xy(end))), point(tuple(xy(start))))
            curve = FakeCurve(first, last)
            sketch.curves.append(curve)
            return curve

        def add_circle(center, radius):
            circle = FakeCircle(sketch, center, radius)
            sketch.circles.append(circle)
            return circle

        self.sketchCurves = SimpleNamespace(sketchLines=SimpleNamespace(addByTwoPoints=add_line),
                                            sketchArcs=SimpleNamespace(addByThreePoints=add_arc),
                                            sketchCircles=SimpleNamespace(addByCenterRadius=add_circle))
        self.geometricConstraints = SimpleNamespace(addCoincident=lambda a, b: None)


def tuple_point(x, y, z=0.0):
    return (x, y, z)


class BuildLoopTests(unittest.TestCase):
    def test_lines_share_sketch_points(self):
        sketch = FakeSketch()
        curves, vertices, warnings = sketch_profile.build_loop(sketch, SQUARE, {}, tuple_point)
        self.assertEqual(len(curves), 4)
        self.assertEqual(warnings, [])
        self.assertEqual(sketch.merges, 0)
        for k, curve in enumerate(curves):
            self.assertIs(curve.startSketchPoint, vertices[k])
            self.assertIs(curve.endSketchPoint, vertices[(k + 1) % 4])
        self.assertTrue(all(v.connectedEntities.count == 2 for v in vertices))

    def test_clockwise_arc_ends_are_matched_and_merged(self):
        sketch = FakeSketch()
        # Bottom edge bowed outward: from (0,0) to (2,0) via (1,-0.5) is counterclockwise; the
        # top edge from (2,2) to (0,2) via (1,1.5) bows inward, i.e. clockwise: Fusion swaps it.
        mids = {0: (1.0, -0.5), 2: (1.0, 1.5)}
        curves, vertices, warnings = sketch_profile.build_loop(sketch, SQUARE, mids, tuple_point)
        self.assertEqual(warnings, [])
        self.assertGreater(sketch.merges, 0)
        self.assertTrue(all(v.connectedEntities.count == 2 for v in vertices))
        top = curves[2]
        self.assertIs(top.startSketchPoint, vertices[0 + 3])  # Swapped: starts at (0,2).
        self.assertIs(top.endSketchPoint, vertices[2])
        for v, expected in zip(vertices, SQUARE):
            self.assertEqual((v.geometry.x, v.geometry.y), expected)

    def test_failed_curve_raises(self):
        sketch = FakeSketch()
        sketch.sketchCurves.sketchLines.addByTwoPoints = lambda a, b: None
        with self.assertRaises(RuntimeError):
            sketch_profile.build_loop(sketch, SQUARE, {}, tuple_point)


class Collection:
    def __init__(self, members):
        self.members = list(members)
        self.count = len(self.members)

    def item(self, i):
        return self.members[i]


def fake_profile(area, curves, outer=True):
    """One profile with a single loop; *curves* are (sketch entity, geometry type) pairs."""
    profile_curves = [SimpleNamespace(sketchEntity=e, geometryType=t) for e, t in curves]
    loop = SimpleNamespace(isOuter=outer, profileCurves=Collection(profile_curves))
    return SimpleNamespace(entityToken=f"profile-{area}", profileLoops=Collection([loop]),
                           areaProperties=lambda _: SimpleNamespace(
                               area=area, centroid=SimpleNamespace(x=0.0, y=0.0, z=0.0)))


CIRCLE_TYPE, ARC_TYPE = 2, 1


class BuildSketch(FakeSketch):
    """A FakeSketch that computes profiles, maps model space and records its life."""

    def __init__(self, model_offset=(0.0, 0.0, 0.0)):
        super().__init__()
        self.name, self.entityToken = "Sketch1", "sketch-token"
        self.model_offset = model_offset
        self.deleted = False
        self.deferred_history = []
        self.fixed_profiles = None

    @property
    def isComputeDeferred(self):
        return self.deferred_history[-1] if self.deferred_history else False

    @isComputeDeferred.setter
    def isComputeDeferred(self, value):
        self.deferred_history.append(value)

    @property
    def profiles(self):
        if self.fixed_profiles is not None:
            return Collection(self.fixed_profiles)
        found = [fake_profile(math.pi * c.radius ** 2, [(c, CIRCLE_TYPE)]) for c in self.circles]
        if self.curves:
            found.append(fake_profile(100.0, [(c, ARC_TYPE) for c in self.curves]))
        return Collection(found)

    def modelToSketchSpace(self, p):
        x, y, z = (a - b for a, b in zip(p, self.model_offset))
        return SimpleNamespace(x=x, y=y, z=z)

    def deleteMe(self):
        self.deleted = True
        return True


def fake_design(sketch):
    sketches = SimpleNamespace(add=lambda plane: sketch, itemByName=lambda name: None)
    component = SimpleNamespace(name="root", sketches=sketches, xYConstructionPlane="xy-plane",
                                xZConstructionPlane="xz-plane", yZConstructionPlane="yz-plane")
    return SimpleNamespace(rootComponent=component, findEntityByToken=lambda token: [])


class HandlerCase(unittest.TestCase):
    """Runs the tool handler against a fake design."""

    def setUp(self):
        for patcher in (patch.object(adsk.core.Point3D, "create", staticmethod(tuple_point)),
                        patch.dict(value_builders.OBJECT_STORE, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, design, **arguments):
        with patch.object(sketch_profile, "_design", return_value=design):
            result = sketch_profile.sketch_profile({"units": "cm", **arguments})
        text = result["content"][0]["text"]
        return result, {"error": text} if text.startswith("Error:") else json.loads(text)


class BuildCirclesTests(unittest.TestCase):
    def test_one_entity_per_circle_at_sketch_z_zero(self):
        sketch = FakeSketch()
        curves = sketch_profile.build_circles(sketch, [{"center": (1.0, 2.0), "radius": 0.5},
                                                       {"center": (4.0, 2.0), "radius": 0.25}], tuple_point)
        self.assertEqual(curves, sketch.circles)
        self.assertEqual(sketch.curves, [])  # No arcs or lines: nothing to split the wall.
        self.assertEqual([c.center_z for c in curves], [0.0, 0.0])
        self.assertEqual([c.radius for c in curves], [0.5, 0.25])

    def test_failed_circle_raises_and_keeps_what_was_drawn(self):
        sketch = FakeSketch()
        real = sketch.sketchCurves.sketchCircles.addByCenterRadius
        answers = iter([real((0.0, 0.0, 0.0), 1.0), None])
        sketch.sketchCurves.sketchCircles.addByCenterRadius = lambda c, r: next(answers)
        drawn = []
        with self.assertRaises(RuntimeError):
            sketch_profile.build_circles(sketch, [{"center": (0.0, 0.0), "radius": 1.0},
                                                  {"center": (5.0, 0.0), "radius": 1.0}], tuple_point, drawn)
        self.assertEqual(len(drawn), 1)


class CircleStatesTests(unittest.TestCase):
    def sketch_with(self, *profiles):
        return SimpleNamespace(profiles=Collection(profiles))

    def test_whole_cut_and_absent(self):
        whole, cut, absent = object(), object(), object()
        sketch = self.sketch_with(fake_profile(1.0, [(whole, CIRCLE_TYPE)]),
                                  fake_profile(2.0, [(cut, ARC_TYPE), ("line", 0)]),
                                  fake_profile(3.0, [(cut, ARC_TYPE)]))
        self.assertEqual(sketch_profile.circle_states(sketch, [whole, cut, absent]), [True, False, None])

    def test_circle_bounding_two_profiles_stays_whole(self):
        hole = object()
        sketch = self.sketch_with(fake_profile(1.0, [(hole, CIRCLE_TYPE)]), fake_profile(9.0, [(hole, CIRCLE_TYPE)]))
        self.assertEqual(sketch_profile.circle_states(sketch, [hole]), [True])

    def test_no_profiles(self):
        self.assertEqual(sketch_profile.circle_states(self.sketch_with(), [object()]), [None])


class CirclesActionTests(HandlerCase):
    ARGS = {"action": "circles", "sketch_plane": "xy", "name": "Holes"}

    def test_draws_many_circles_and_reports_them(self):
        sketch = BuildSketch()
        result, payload = self.run_tool(fake_design(sketch), **self.ARGS, circles=[
            {"center": [1, 1], "diameter": 2}, {"center": [5, 1], "diameter": 1}])
        self.assertFalse(result["isError"])
        self.assertEqual(sketch.name, "Holes")
        self.assertEqual(len(sketch.circles), 2)
        self.assertEqual(sketch.curves, [])
        self.assertEqual(sketch.deferred_history, [True, False])
        self.assertEqual(payload["curve_count"], 2)
        self.assertEqual(payload["profile_count"], 2)
        self.assertEqual(payload["profile"]["index"], 0)  # The larger circle.
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["circles"][1], {"index": 1, "center": [5.0, 1.0], "diameter": 1.0,
                                                 "entity_token": "circle-1", "whole": True})
        self.assertIs(value_builders.OBJECT_STORE[sketch_profile.SKETCH_KEY], sketch)

    def test_user_units_round_trip(self):
        sketch = BuildSketch()
        _, payload = self.run_tool(fake_design(sketch), **self.ARGS, units="in",
                                   circles=[{"center": [1, 2], "diameter": 0.25}])
        self.assertEqual(sketch.circles[0].radius, 0.125 * 2.54)
        self.assertAlmostEqual(payload["circles"][0]["diameter"], 0.25)
        self.assertAlmostEqual(payload["circles"][0]["center"][1], 2.0)

    def test_model_space_drops_z_after_refusing_off_plane(self):
        sketch = BuildSketch(model_offset=(0.0, 0.0, 3.0))
        design = fake_design(sketch)
        design.findEntityByToken = lambda token: [SimpleNamespace(objectType="adsk::fusion::ConstructionPlane")]
        _, payload = self.run_tool(design, action="circles", plane_entity_token="t", space="model",
                                   circles=[{"center": [1, 2, 3], "diameter": 1}])
        self.assertEqual(payload["circles"][0]["center"], [1.0, 2.0])
        self.assertEqual(sketch.circles[0].center_z, 0.0)

    def test_off_plane_center_deletes_the_new_sketch(self):
        sketch = BuildSketch(model_offset=(0.0, 0.0, 3.0))
        design = fake_design(sketch)
        design.findEntityByToken = lambda token: [SimpleNamespace(objectType="adsk::fusion::ConstructionPlane")]
        result, payload = self.run_tool(design, action="circles", plane_entity_token="t", space="model",
                                        circles=[{"center": [1, 2, 0], "diameter": 1}])
        self.assertTrue(result["isError"])
        self.assertIn("circles[0].center is 3 cm off", payload["problems"][0])
        self.assertTrue(sketch.deleted)
        self.assertEqual(sketch.circles, [])

    def test_no_profile_deletes_the_new_sketch(self):
        sketch = BuildSketch()
        sketch.fixed_profiles = []
        result, payload = self.run_tool(fake_design(sketch), **self.ARGS, circles=[{"center": [0, 0], "diameter": 1}])
        self.assertTrue(result["isError"])
        self.assertTrue(sketch.deleted)

    def test_cut_circle_warns(self):
        sketch = BuildSketch()
        sketch.fixed_profiles = [fake_profile(1.0, [("line", 0)])]
        _, payload = self.run_tool(fake_design(sketch), **self.ARGS, circles=[{"center": [0, 0], "diameter": 1}])
        self.assertIsNone(payload["circles"][0]["whole"])
        self.assertIn("bounds no profile", payload["warnings"][0])
        sketch = BuildSketch()
        drawn = sketch.sketchCurves.sketchCircles.addByCenterRadius

        def add_and_cut(center, radius):
            circle = drawn(center, radius)
            sketch.fixed_profiles = [fake_profile(1.0, [(circle, ARC_TYPE)])]
            return circle

        sketch.sketchCurves.sketchCircles.addByCenterRadius = add_and_cut
        _, payload = self.run_tool(fake_design(sketch), **self.ARGS, circles=[{"center": [0, 0], "diameter": 1}])
        self.assertIs(payload["circles"][0]["whole"], False)
        self.assertIn("cannot be threaded", payload["warnings"][0])

    def test_existing_name_refused_before_a_sketch_exists(self):
        design = fake_design(BuildSketch())
        design.rootComponent.sketches.itemByName = lambda name: object()
        design.rootComponent.sketches.add = lambda plane: self.fail("sketch created")
        result, payload = self.run_tool(design, **self.ARGS, circles=[{"center": [0, 0], "diameter": 1}])
        self.assertTrue(result["isError"])
        self.assertIn("already exists", payload["problems"][0])


class ChooseProfileTests(unittest.TestCase):
    def test_largest_and_containing(self):
        described = [{"index": 0, "area": 0.01, "contains_point": False},
                     {"index": 1, "area": 9.0, "contains_point": False},
                     {"index": 2, "area": 4.0, "contains_point": True}]
        self.assertEqual(sketch_profile.choose_profile(described), (1, 2))
        self.assertEqual(sketch_profile.choose_profile([]), (None, None))


class SchemaTests(unittest.TestCase):
    def schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "sketch_profile")["inputSchema"]

    def test_strict_schema(self):
        schema = self.schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["action", "units"])
        self.assertEqual(schema["properties"]["action"]["enum"], list(sketch_profile.ACTIONS))
        self.assertEqual(set(schema["properties"]["units"]["enum"]), set(geo.CM_PER_UNIT))
        self.assertEqual(schema["properties"]["sketch_plane"]["enum"], ["xy", "xz", "yz"])
        self.assertFalse(schema["properties"]["arcs"]["items"]["additionalProperties"])
        self.assertEqual(schema["properties"]["points"]["items"]["items"], {"type": "number"})
        self.assertFalse(schema["properties"]["circles"]["items"]["additionalProperties"])
        self.assertEqual(schema["properties"]["circles"]["items"]["required"], ["center", "diameter"])


if __name__ == "__main__":
    unittest.main()
