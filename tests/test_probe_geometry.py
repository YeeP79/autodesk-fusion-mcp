"""probe_geometry: pure sampling logic against analytic fake bodies, and the
Fusion seam against a fake root that follows findBRepUsingRay's documented
behavior (each entity once, for its first intersection)."""

import json
import math
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

import adsk.core
import adsk.fusion

from fusion_bridge import probe_geometry as pg
from fusion_bridge import tool_surface


# -- Analytic fake solids ------------------------------------------------------


def _subtract(spans, holes):
    for hole_start, hole_end in holes:
        kept = []
        for start, end in spans:
            if hole_end <= start or hole_start >= end:
                kept.append((start, end))
                continue
            if start < hole_start:
                kept.append((start, hole_start))
            if hole_end < end:
                kept.append((hole_end, end))
        spans = kept
    return spans


class Box:
    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi

    def spans(self, origin, direction):
        near, far = -math.inf, math.inf
        for o, d, lo, hi in zip(origin, direction, self.lo, self.hi):
            if abs(d) < 1e-12:
                if not lo <= o <= hi:
                    return []
                continue
            a, b = sorted(((lo - o) / d, (hi - o) / d))
            near, far = max(near, a), min(far, b)
        return [(near, far)] if near < far else []

    def bbox(self):
        return self.lo, self.hi


class Cylinder:
    """About world Z through (0, 0), hollow when *bore* is above zero."""

    def __init__(self, radius, z0, z1, bore=0.0):
        self.radius, self.z0, self.z1, self.bore = radius, z0, z1, bore

    def _disc(self, radius, origin, direction):
        ox, oy, _ = origin
        dx, dy, _ = direction
        a, b, c = dx * dx + dy * dy, 2 * (ox * dx + oy * dy), ox * ox + oy * oy - radius * radius
        if a < 1e-12:
            return (-math.inf, math.inf) if c < 0 else None
        root = b * b - 4 * a * c
        if root <= 0:
            return None
        root = math.sqrt(root)
        return (-b - root) / (2 * a), (-b + root) / (2 * a)

    def _height(self, origin, direction):
        if abs(direction[2]) < 1e-12:
            return (-math.inf, math.inf) if self.z0 <= origin[2] <= self.z1 else None
        return tuple(sorted(((self.z0 - origin[2]) / direction[2], (self.z1 - origin[2]) / direction[2])))

    def spans(self, origin, direction):
        disc, height = self._disc(self.radius, origin, direction), self._height(origin, direction)
        if disc is None or height is None:
            return []
        start, end = max(disc[0], height[0]), min(disc[1], height[1])
        if start >= end:
            return []
        bore = self._disc(self.bore, origin, direction) if self.bore else None
        return _subtract([(start, end)], [bore] if bore else [])

    def bbox(self):
        return (-self.radius, -self.radius, self.z0), (self.radius, self.radius, self.z1)


class FakeSolid:
    """Union of parts, with crossings on the union's boundary."""

    def __init__(self, *parts):
        self.parts = parts

    def spans(self, origin, direction):
        found = [s for part in self.parts for s in part.spans(origin, direction)]
        return [tuple(s) for s in pg.merge_intervals(found, touch=0.0)]

    def crossings(self, origin, direction):
        return sorted(t for span in self.spans(origin, direction) for t in span if t >= 0)

    def state(self, p):
        for direction in ((0.6, 0.48, 0.64),):
            for start, end in self.spans(p, direction):
                if abs(start) < 1e-7 or abs(end) < 1e-7:
                    return "on"
                if start < 0 < end:
                    return "inside"
        return "outside"

    def bbox(self):
        boxes = [part.bbox() for part in self.parts]
        return (tuple(min(b[0][i] for b in boxes) for i in range(3)),
                tuple(max(b[1][i] for b in boxes) for i in range(3)))


class FakeProbe:
    """Same contract as FusionProbe: a body appears once per cast."""

    def __init__(self, *solids):
        self.solids = solids
        self.casts = 0

    def cast(self, origin, direction):
        self.casts += 1
        hits = []
        for index, solid in enumerate(self.solids):
            crossings = solid.crossings(origin, direction)
            if crossings:
                hits.append((crossings[0], index))
        return sorted(hits)

    def contains(self, index, p):
        return self.solids[index].state(p)


def targets_for(*solids):
    return [{"occurrence_path": "root", "body": f"Body{i + 1}", "index": i, "visible": True, "solid": True,
             "bbox": s.bbox()} for i, s in enumerate(solids)]


def run(arguments, *solids, budget=None):
    opts, err = pg.validate_args(arguments)
    assert err is None, err
    return pg.sample(opts, targets_for(*solids), FakeProbe(*solids), budget or pg.Budget())


# A dish: a floor with a rim around it, the shape behind the "at radius r" question.
def dish():
    return FakeSolid(Cylinder(5.0, 0.0, 1.0), Cylinder(5.0, 1.0, 3.0, bore=4.0))


class VectorMathTests(unittest.TestCase):
    def test_unit_rejects_zero(self):
        self.assertIsNone(pg.unit((0, 0, 0)))
        self.assertEqual(pg.unit((0, 3, 4)), (0, 0.6, 0.8))

    def test_frame_is_right_handed_and_orthonormal(self):
        for axis in ((0, 0, 1), (1, 0, 0), (1, 1, 1), (0, -1, 0.2)):
            with self.subTest(axis=axis):
                a, r, b = pg.perpendicular_frame(axis)
                self.assertAlmostEqual(pg.dot(a, r), 0)
                self.assertAlmostEqual(pg.dot(r, r), 1)
                for got, want in zip(pg.cross(a, r), b):
                    self.assertAlmostEqual(got, want)

    def test_frame_default_reference_avoids_axis(self):
        self.assertEqual(pg.perpendicular_frame((0, 0, 1))[1], (1, 0, 0))
        self.assertEqual(pg.perpendicular_frame((1, 0, 0))[1], (0, 1, 0))

    def test_frame_projects_reference_and_refuses_parallel(self):
        self.assertEqual(pg.perpendicular_frame((0, 0, 1), (0, 2, 5))[1], (0, 1, 0))
        self.assertIsNone(pg.perpendicular_frame((0, 0, 1), (0, 0, -3)))

    def test_angle_turns_right_handed_about_axis(self):
        frame = pg.perpendicular_frame((0, 0, 1))
        got = pg.radial_direction(frame, 90)
        for value, want in zip(got, (0, 1, 0)):
            self.assertAlmostEqual(value, want)

    def test_grid_points_include_far_edges(self):
        points = list(pg.grid_points((1, 0, 0), (2, 0, 0), (0, 4, 0), 3, 2))
        self.assertEqual(points[0], (0, 0, (1, 0, 0)))
        self.assertEqual(points[1], (0, 1, (3, 0, 0)))
        self.assertEqual(points[-1], (2, 1, (3, 4, 0)))
        self.assertEqual(len(points), 6)

    def test_grid_single_count_stays_at_origin(self):
        self.assertEqual(list(pg.grid_points((1, 2, 3), (9, 0, 0), (0, 9, 0), 1, 1)), [(0, 0, (1, 2, 3))])

    def test_extents_measure_from_center(self):
        box = ((1, 1, 2), (3, 2, 6))
        self.assertEqual(pg.axial_extent([box], (0, 0, 1), (0, 0, 1)), (1, 5))
        self.assertAlmostEqual(pg.radial_extent([box], (0, 0, 0), (0, 0, 1)), math.sqrt(13))
        self.assertIsNone(pg.axial_extent([], (0, 0, 0), (0, 0, 1)))
        self.assertIsNone(pg.radial_extent([], (0, 0, 0), (0, 0, 1)))

    def test_in_bbox_margin(self):
        box = ((0, 0, 0), (1, 1, 1))
        self.assertTrue(pg.in_bbox(box, (1.005, 0.5, 0.5)))
        self.assertFalse(pg.in_bbox(box, (1.02, 0.5, 0.5)))


class IntervalTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(pg.merge_intervals([]), [])

    def test_sorts_merges_overlap_and_contained(self):
        self.assertEqual(pg.merge_intervals([(5, 6), (1, 3), (2, 4), (1.5, 2)]), [[1, 4], [5, 6]])

    def test_touching_joins_within_tolerance_only(self):
        self.assertEqual(pg.merge_intervals([(0, 1), (1 + 1e-5, 2)]), [[0, 2]])
        self.assertEqual(pg.merge_intervals([(0, 1), (1.01, 2)]), [[0, 1], [1.01, 2]])


class UnitTests(unittest.TestCase):
    def test_from_cm(self):
        self.assertEqual(pg.from_cm(2.54, "in"), 1.0)
        self.assertEqual(pg.from_cm(2.54, "cm"), 2.54)
        self.assertEqual(pg.from_cm(2.54, "mm"), 25.4)

    def test_negative_zero_is_clean(self):
        self.assertEqual(str(pg.from_cm(-0.0, "cm")), "0.0")

    def test_arguments_convert_to_cm(self):
        opts, err = pg.validate_args({"action": "ray", "units": "in", "origin": [1, 0, 0], "direction": [0, 0, 7]})
        self.assertIsNone(err)
        self.assertEqual(opts["origin"], (2.54, 0, 0))
        self.assertEqual(opts["direction"], (0, 0, 1))  # a direction has no unit

    def test_result_in_units(self):
        solid = FakeSolid(Box((2.54, -1, -1), (5.08, 1, 1)))
        got = run({"action": "ray", "units": "in", "origin": [0, 0, 0], "direction": [1, 0, 0]}, solid)
        self.assertEqual([h["distance"] for h in got["hits"]], [1.0, 2.0])
        self.assertEqual(got["hits"][1]["point"], [2.0, 0.0, 0.0])


class ValidationTests(unittest.TestCase):
    def _refused(self, arguments, text):
        result = pg.probe_geometry(arguments)
        self.assertTrue(result["isError"], arguments)
        self.assertIn(text, result["content"][0]["text"])

    def test_units_required_and_checked(self):
        self._refused({"action": "point", "points": [[0, 0, 0]]}, "units")
        self._refused({"action": "point", "units": "ft", "points": [[0, 0, 0]]}, "units")
        self._refused({"action": "point", "units": ["in"], "points": [[0, 0, 0]]}, "units")

    def test_action_checked(self):
        self._refused({"units": "in"}, "action")
        self._refused({"units": "in", "action": "sweep"}, "action")

    def test_arguments_must_be_an_object(self):
        self._refused(None, "object")

    def test_missing_field_for_action(self):
        self._refused({"action": "ray", "units": "in", "origin": [0, 0, 0]}, "direction")
        self._refused({"action": "radial", "units": "in", "center": [0, 0, 0], "axis": [0, 0, 1], "radii": [1]}, "angles")

    def test_field_from_another_action_refused(self):
        self._refused({"action": "point", "units": "in", "points": [[0, 0, 0]], "radii": [1]}, "radii")
        self._refused({"action": "grid", "units": "in", "origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0],
                       "rows": 1, "columns": 1, "sample": "point", "direction": [0, 0, 1]}, "direction")

    def test_bad_vectors(self):
        for bad in ([0, 0], [0, 0, "1"], [0, 0, True], [0, 0, math.nan], [0, 0, math.inf], [0, 0, 10 ** 400], "x"):
            with self.subTest(bad=bad):
                self._refused({"action": "ray", "units": "in", "origin": bad, "direction": [0, 0, 1]}, "origin")

    def test_zero_direction(self):
        self._refused({"action": "ray", "units": "in", "origin": [0, 0, 0], "direction": [0, 0, 0]}, "zero")

    def test_bad_points(self):
        self._refused({"action": "point", "units": "in", "points": []}, "points")
        self._refused({"action": "point", "units": "in", "points": [[0, 0]]}, "points")

    def test_grid_counts(self):
        base = {"action": "grid", "units": "in", "origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0]}
        for bad in (0, -1, 2001, 1.5, True):
            with self.subTest(bad=bad):
                self._refused({**base, "rows": bad, "columns": 2}, "rows")

    def test_grid_parallel_edges_need_a_direction(self):
        base = {"action": "grid", "units": "in", "origin": [0, 0, 0], "u": [1, 0, 0], "v": [2, 0, 0], "rows": 2, "columns": 2}
        self._refused(base, "parallel")
        self.assertIsNone(pg.validate_args({**base, "direction": [0, 0, 1]})[1])
        self.assertIsNone(pg.validate_args({**base, "sample": "point"})[1])

    def test_grid_unknown_sample(self):
        self._refused({"action": "grid", "units": "in", "origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0],
                       "rows": 1, "columns": 1, "sample": "line"}, "sample")

    def test_radial_lists_and_range(self):
        base = {"action": "radial", "units": "in", "center": [0, 0, 0], "axis": [0, 0, 1], "radii": [1], "angles": [0]}
        self._refused({**base, "radii": []}, "radii")
        self._refused({**base, "radii": [-1]}, "radii")
        self._refused({**base, "angles": ["a"]}, "angles")
        self._refused({**base, "axial_range": [3, 1]}, "axial_range")
        self._refused({**base, "axial_range": [1]}, "axial_range")
        self._refused({**base, "axis": [0, 0, 0]}, "axis")
        self._refused({**base, "reference": [0, 0, 2]}, "parallel")

    def test_axial_fields(self):
        base = {"action": "axial", "units": "in", "center": [0, 0, 0], "axis": [0, 0, 1], "stations": [1]}
        self._refused({**base, "stations": []}, "stations")
        self._refused({**base, "max_radius": 0}, "max_radius")
        self.assertEqual(pg.validate_args(base)[0]["angles"], [0, 90, 180, 270])

    def test_bad_bodies(self):
        base = {"action": "point", "units": "in", "points": [[0, 0, 0]]}
        self._refused({**base, "bodies": []}, "bodies")
        self._refused({**base, "bodies": ["Body1"]}, "bodies")
        self._refused({**base, "bodies": [{"body_name": "B"}]}, "occurrence_path")
        self._refused({**base, "bodies": [{"occurrence_path": "root", "body_name": " "}]}, "body_name")
        self._refused({**base, "bodies": [{"occurrence_path": "root", "body_name": "B", "x": 1}]}, "bodies")
        self._refused({**base, "bodies": [{"occurrence_path": "root", "body_name": "B", "body_index": -1}]}, "body_index")


class ResolveTargetsTests(unittest.TestCase):
    def _entry(self, path, body, index=0, visible=True, solid=True):
        return {"occurrence_path": path, "body": body, "index": index, "visible": visible, "solid": solid, "bbox": None}

    def test_default_is_visible_solids(self):
        entries = [self._entry("root", "A"), self._entry("root", "Hidden", visible=False),
                   self._entry("root", "Surface", solid=False)]
        targets, warnings, problem = pg.resolve_targets(entries, None)
        self.assertEqual([t["body"] for t in targets], ["A"])
        self.assertEqual((warnings, problem), ([], None))

    def test_default_with_nothing_visible(self):
        _, _, problem = pg.resolve_targets([self._entry("root", "H", visible=False)], None)
        self.assertIn("no visible solid", problem["error"])

    def test_explicit_may_be_hidden(self):
        entries = [self._entry("a:1", "B", visible=False), self._entry("a:2", "B")]
        targets, _, problem = pg.resolve_targets(entries, [("a:1", "B", None)])
        self.assertIsNone(problem)
        self.assertEqual(targets, [entries[0]])

    def test_miss_gives_candidates(self):
        _, _, problem = pg.resolve_targets([self._entry("a:1", "B")], [("a:2", "B", None)])
        self.assertIn("no body matches", problem["error"])
        self.assertEqual(problem["candidates"][0]["occurrence_path"], "a:1")

    def test_repeated_name_needs_index(self):
        entries = [self._entry("root", "B", 0), self._entry("root", "B", 1)]
        _, _, problem = pg.resolve_targets(entries, [("root", "B", None)])
        self.assertIn("ambiguous", problem["error"])
        targets, _, problem = pg.resolve_targets(entries, [("root", "B", 1)])
        self.assertEqual((targets, problem), ([entries[1]], None))

    def test_same_body_twice_sampled_once(self):
        entries = [self._entry("root", "B")]
        targets, _, _ = pg.resolve_targets(entries, [("root", "B", None), ("root", "B", 0)])
        self.assertEqual(len(targets), 1)

    def test_explicit_surface_warns(self):
        _, warnings, _ = pg.resolve_targets([self._entry("root", "S", solid=False)], [("root", "S", None)])
        self.assertIn("not solid", warnings[0])


class MatchHitTests(unittest.TestCase):
    TARGETS = [{"occurrence_path": "a:1", "body": "B"}, {"occurrence_path": "a:2", "body": "B"},
               {"occurrence_path": "root", "body": "C"}]

    def test_proxy_matches_on_path_and_name(self):
        self.assertEqual(pg.match_hit(self.TARGETS, "a:2", "B", lambda i: "outside"), 1)

    def test_other_occurrence_is_not_a_target(self):
        self.assertIsNone(pg.match_hit(self.TARGETS, "a:3", "B", lambda i: "on"))

    def test_native_hit_goes_to_target_it_lies_on(self):
        self.assertEqual(pg.match_hit(self.TARGETS, "root", "B", lambda i: "on" if i == 1 else "outside"), 1)

    def test_native_hit_on_nothing_is_unmatched(self):
        self.assertIsNone(pg.match_hit(self.TARGETS, "root", "B", lambda i: "outside"))


class RayCrossingTests(unittest.TestCase):
    def test_finds_exit_a_single_cast_never_reports(self):
        probe = FakeProbe(FakeSolid(Box((1, -1, -1), (2, 1, 1))))
        self.assertEqual(len(probe.cast((0, 0, 0), (1, 0, 0))), 1)
        crossings, capped = pg.ray_crossings(probe.cast, (0, 0, 0), (1, 0, 0))
        self.assertFalse(capped)
        self.assertEqual([round(t, 6) for t, _ in crossings], [1, 2])

    def test_every_wall_of_a_tube(self):
        probe = FakeProbe(FakeSolid(Cylinder(2.0, -1, 1, bore=1.5)))
        crossings, _ = pg.ray_crossings(probe.cast, (-5, 0, 0), (1, 0, 0))
        self.assertEqual([round(t, 6) for t, _ in crossings], [3, 3.5, 6.5, 7])

    def test_bodies_interleave_in_order(self):
        a = FakeSolid(Box((1, -1, -1), (2, 1, 1)), Box((5, -1, -1), (6, 1, 1)))
        b = FakeSolid(Box((3, -1, -1), (4, 1, 1)))
        crossings, _ = pg.ray_crossings(FakeProbe(a, b).cast, (0, 0, 0), (1, 0, 0))
        self.assertEqual([(round(t, 6), i) for t, i in crossings],
                         [(1, 0), (2, 0), (3, 1), (4, 1), (5, 0), (6, 0)])

    def test_bodies_in_contact_both_keep_their_hits(self):
        a = FakeSolid(Box((1, -1, -1), (2, 1, 1)))
        b = FakeSolid(Box((2, -1, -1), (3, 1, 1)))
        crossings, _ = pg.ray_crossings(FakeProbe(a, b).cast, (0, 0, 0), (1, 0, 0))
        self.assertEqual(sorted((round(t, 6), i) for t, i in crossings), [(1, 0), (2, 0), (2, 1), (3, 1)])

    def test_start_inside_sees_only_the_exit(self):
        probe = FakeProbe(FakeSolid(Box((-1, -1, -1), (1, 1, 1))))
        crossings, _ = pg.ray_crossings(probe.cast, (0, 0, 0), (1, 0, 0))
        self.assertEqual([round(t, 6) for t, _ in crossings], [1])

    def test_nothing_hit(self):
        probe = FakeProbe(FakeSolid(Box((1, 1, 1), (2, 2, 2))))
        self.assertEqual(pg.ray_crossings(probe.cast, (0, 0, 0), (-1, 0, 0)), ([], False))
        self.assertEqual(probe.casts, 1)

    def test_t_end_stops_the_march(self):
        probe = FakeProbe(FakeSolid(Box((1, -1, -1), (2, 1, 1)), Box((5, -1, -1), (6, 1, 1))))
        crossings, _ = pg.ray_crossings(probe.cast, (0, 0, 0), (1, 0, 0), 0.0, 3.0)
        self.assertEqual([round(t, 6) for t, _ in crossings], [1, 2])

    def test_hit_cap_reports(self):
        boxes = [Box((2 * i + 1, -1, -1), (2 * i + 2, 1, 1)) for i in range(10)]
        crossings, capped = pg.ray_crossings(FakeProbe(FakeSolid(*boxes)).cast, (0, 0, 0), (1, 0, 0), max_hits=4)
        self.assertTrue(capped)
        self.assertEqual(len(crossings), 4)

    def test_a_cast_stuck_on_one_distance_cannot_spin(self):
        crossings, capped = pg.ray_crossings(lambda o, d: [(0.0, 0)], (0, 0, 0), (1, 0, 0), max_hits=3)
        self.assertTrue(capped)
        self.assertLessEqual(len(crossings), 3)


class SolidIntervalTests(unittest.TestCase):
    def _found(self, solids, origin, direction, start, end):
        probe = FakeProbe(*solids)
        return pg.solid_intervals(probe.cast, probe.contains, origin, direction, start, end, len(solids))

    def test_distances_count_from_the_ray_origin(self):
        solid = dish()
        found, flags = self._found([solid], (4.5, 0, -1), (0, 0, 1), 0.0, 10.0)
        self.assertEqual([[round(c, 6) for c in s] for s in found[0]], [[1, 4]])
        self.assertEqual(flags, set())

    def test_gap_between_spans(self):
        solid = FakeSolid(Box((-1, -1, 1), (1, 1, 2)), Box((-1, -1, 3), (1, 1, 4)))
        found, _ = self._found([solid], (0, 0, 0), (0, 0, 1), 0.0, 5.0)
        self.assertEqual([[round(c, 6) for c in s] for s in found[0]], [[1, 2], [3, 4]])

    def test_material_filling_the_whole_range_has_no_crossings(self):
        solid = FakeSolid(Box((-1, -1, -9), (1, 1, 9)))
        found, _ = self._found([solid], (0, 0, 0), (0, 0, 1), -1.0, 1.0)
        self.assertEqual(found, {0: [[-1.0, 1.0]]})

    def test_range_clips_material(self):
        solid = FakeSolid(Box((-1, -1, 1), (1, 1, 5)))
        found, _ = self._found([solid], (0, 0, 0), (0, 0, 1), 2.0, 3.0)
        self.assertEqual(found, {0: [[2.0, 3.0]]})

    def test_empty(self):
        found, _ = self._found([FakeSolid(Box((5, 5, 5), (6, 6, 6)))], (0, 0, 0), (0, 0, 1), 0.0, 9.0)
        self.assertEqual(found, {})

    def test_tangent_touch_adds_no_material(self):
        hits = iter([[(2.0, 0)], []])
        found, flags = pg.solid_intervals(lambda o, d: next(hits), lambda i, p: "outside",
                                          (0, 0, 0), (1, 0, 0), 0.0, 5.0, 1)
        self.assertEqual((found, flags), ({}, set()))

    def test_ray_along_a_surface_is_flagged(self):
        found, flags = pg.solid_intervals(lambda o, d: [], lambda i, p: "on", (0, 0, 0), (1, 0, 0), 0.0, 5.0, 1)
        self.assertEqual(found, {0: [[0.0, 5.0]]})
        self.assertEqual(flags, {"coincident"})

    def test_unknown_is_flagged_and_not_material(self):
        found, flags = pg.solid_intervals(lambda o, d: [], lambda i, p: "unknown", (0, 0, 0), (1, 0, 0), 0.0, 5.0, 1)
        self.assertEqual((found, flags), ({}, {"unknown"}))


class ActionTests(unittest.TestCase):
    def test_point_reports_inside_on_and_implies_outside(self):
        solid = FakeSolid(Box((-1, -1, -1), (1, 1, 1)))
        got = run({"action": "point", "units": "cm", "points": [[0, 0, 0], [1, 0, 0], [5, 0, 0]]}, solid)
        self.assertEqual(got["results"][0], {"point": [0.0, 0.0, 0.0], "inside": ["root/Body1"]})
        self.assertEqual(got["results"][1]["on"], ["root/Body1"])
        self.assertEqual(got["results"][2], {"point": [5.0, 0.0, 0.0]})
        self.assertFalse(got["truncated"])

    def test_point_names_each_body(self):
        a = FakeSolid(Box((-1, -1, -1), (1, 1, 1)))
        b = FakeSolid(Box((-2, -2, -2), (2, 2, 2)))
        got = run({"action": "point", "units": "cm", "points": [[0, 0, 0]]}, a, b)
        self.assertEqual(got["results"][0]["inside"], ["root/Body1", "root/Body2"])
        self.assertEqual(got["bodies"], ["root/Body1", "root/Body2"])

    def test_ray_orders_hits_with_body(self):
        a = FakeSolid(Box((3, -1, -1), (4, 1, 1)))
        b = FakeSolid(Box((1, -1, -1), (2, 1, 1)))
        got = run({"action": "ray", "units": "cm", "origin": [0, 0, 0], "direction": [2, 0, 0]}, a, b)
        self.assertEqual([(h["distance"], h["body"]) for h in got["hits"]],
                         [(1.0, "Body2"), (2.0, "Body2"), (3.0, "Body1"), (4.0, "Body1")])
        self.assertEqual(got["hit_count"], 4)

    def test_ray_miss(self):
        got = run({"action": "ray", "units": "cm", "origin": [0, 0, 0], "direction": [0, 1, 0]},
                  FakeSolid(Box((3, 3, 3), (4, 4, 4))))
        self.assertEqual(got["hits"], [])

    def test_grid_of_rays_matrix(self):
        solid = FakeSolid(Box((0, 0, 1), (1, 3, 2)), Box((1, 0, 2), (2, 3, 3)))
        got = run({"action": "grid", "units": "cm", "origin": [0.5, 0.5, 0], "u": [2, 0, 0], "v": [0, 1, 0],
                   "rows": 2, "columns": 3}, solid)
        self.assertEqual(got["direction"], [0, 0, 1])  # u x v
        self.assertEqual(got["first_hit_distance"], [[1.0, 2.0, None], [1.0, 2.0, None]])
        self.assertEqual(got["first_hit_body"], [[0, 0, None], [0, 0, None]])
        self.assertEqual(got["samples_done"], 6)

    def test_grid_of_points_strings(self):
        solid = FakeSolid(Box((-1, -1, -1), (1, 1, 1)))
        got = run({"action": "grid", "units": "cm", "sample": "point", "origin": [-2, 0, 0], "u": [4, 0, 0],
                   "v": [0, 4, 0], "rows": 2, "columns": 5}, solid)
        self.assertEqual(got["containment"], [".+#+.", "....."])

    def test_grid_point_strongest_state_wins_across_bodies(self):
        a = FakeSolid(Box((5, 5, 5), (6, 6, 6)))
        b = FakeSolid(Box((-1, -1, -1), (1, 1, 1)))
        got = run({"action": "grid", "units": "cm", "sample": "point", "origin": [0, 0, 0], "u": [0, 0, 0],
                   "v": [0, 0, 0], "rows": 1, "columns": 1}, a, b)
        self.assertEqual(got["containment"], ["#"])

    def test_radial_dish_and_rim(self):
        got = run({"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1],
                   "radii": [2, 4.5, 6], "angles": [0, 90]}, dish())
        by_radius = {(r["radius"], r["angle"]): r["intervals"] for r in got["results"]}
        self.assertEqual(by_radius[(2.0, 0.0)], [[0.0, 1.0]])
        self.assertEqual(by_radius[(4.5, 90.0)], [[0.0, 3.0]])
        self.assertEqual(by_radius[(6.0, 0.0)], [])
        self.assertEqual(got["frame"]["reference"], [1, 0, 0])
        self.assertEqual(got["axial_range"], [-0.01, 3.01])

    def test_radial_heights_count_from_center_in_units(self):
        solid = FakeSolid(Box((-2.54, -2.54, 2.54), (2.54, 2.54, 5.08)))
        got = run({"action": "radial", "units": "in", "center": [0, 0, 1], "axis": [0, 0, 1],
                   "radii": [0], "angles": [0]}, solid)
        self.assertEqual(got["results"][0]["intervals"], [[0.0, 1.0]])

    def test_radial_reversed_axis_flips_heights(self):
        got = run({"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, -1],
                   "radii": [2], "angles": [0]}, dish())
        self.assertEqual(got["results"][0]["intervals"], [[-1.0, 0.0]])

    def test_radial_explicit_range(self):
        got = run({"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1], "radii": [4.5],
                   "angles": [0], "axial_range": [0.5, 1.5]}, dish())
        self.assertEqual(got["results"][0]["intervals"], [[0.5, 1.5]])

    def test_radial_two_bodies_union_and_by_body(self):
        a = FakeSolid(Box((-1, -1, 0), (1, 1, 1)))
        b = FakeSolid(Box((-1, -1, 1), (1, 1, 2)))
        got = run({"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1],
                   "radii": [0], "angles": [0]}, a, b)
        row = got["results"][0]
        self.assertEqual(row["intervals"], [[0.0, 2.0]])
        self.assertEqual(row["by_body"], {"root/Body1": [[0.0, 1.0]], "root/Body2": [[1.0, 2.0]]})

    def test_one_body_has_no_by_body(self):
        got = run({"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1],
                   "radii": [2], "angles": [0]}, dish())
        self.assertNotIn("by_body", got["results"][0])

    def test_axial_eye_boss(self):
        eye = FakeSolid(Cylinder(3.0, 0.0, 2.0, bore=1.0))
        got = run({"action": "axial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1],
                   "stations": [1, 5], "angles": [0, 180]}, eye)
        rows = {(r["station"], r["angle"]): r["intervals"] for r in got["results"]}
        self.assertEqual(rows[(1.0, 0.0)], [[1.0, 3.0]])
        self.assertEqual(rows[(1.0, 180.0)], [[1.0, 3.0]])
        self.assertEqual(rows[(5.0, 0.0)], [])

    def test_axial_default_angles_and_reach(self):
        eye = FakeSolid(Cylinder(3.0, 0.0, 2.0))
        got = run({"action": "axial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1], "stations": [1]}, eye)
        self.assertEqual([r["angle"] for r in got["results"]], [0, 90, 180, 270])
        self.assertAlmostEqual(got["max_radius"], 3 * math.sqrt(2) + 0.01, places=4)
        self.assertEqual(got["results"][0]["intervals"], [[0.0, 3.0]])

    def test_axial_max_radius_limits_search(self):
        eye = FakeSolid(Cylinder(3.0, 0.0, 2.0, bore=1.0))
        got = run({"action": "axial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1], "stations": [1],
                   "angles": [0], "max_radius": 2}, eye)
        self.assertEqual(got["results"][0]["intervals"], [[1.0, 2.0]])

    def test_default_range_needs_a_bounding_box(self):
        opts, _ = pg.validate_args({"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1],
                                    "radii": [1], "angles": [0]})
        targets = [{"occurrence_path": "root", "body": "B", "bbox": None}]
        with self.assertRaisesRegex(pg.ProbeError, "axial_range"):
            pg.sample(opts, targets, FakeProbe(), pg.Budget())


class BudgetTests(unittest.TestCase):
    def _point_args(self, count):
        return {"action": "point", "units": "cm", "points": [[0, 0, 0]] * count}

    def test_exact_cap_is_not_truncated(self):
        got = run(self._point_args(3), FakeSolid(Box((-1, -1, -1), (1, 1, 1))), budget=pg.Budget(max_samples=3))
        self.assertFalse(got["truncated"])
        self.assertNotIn("truncation_reason", got)

    def test_point_sample_cap(self):
        got = run(self._point_args(5), FakeSolid(Box((-1, -1, -1), (1, 1, 1))), budget=pg.Budget(max_samples=2))
        self.assertTrue(got["truncated"])
        self.assertEqual(got["truncation_reason"], "sample_cap")
        self.assertEqual((got["samples_requested"], got["samples_done"]), (5, 2))

    def test_time_budget(self):
        ticks = iter(range(100))
        budget = pg.Budget(seconds=3, clock=lambda: next(ticks))
        got = run(self._point_args(50), FakeSolid(Box((-1, -1, -1), (1, 1, 1))), budget=budget)
        self.assertEqual(got["truncation_reason"], "time_budget")
        self.assertEqual(got["samples_done"], 2)

    def test_grid_truncated_mid_row_keeps_a_short_row(self):
        args = {"action": "grid", "units": "cm", "origin": [0, 0, -5], "u": [1, 0, 0], "v": [0, 1, 0],
                "rows": 3, "columns": 2}
        got = run(args, FakeSolid(Box((-9, -9, -1), (9, 9, 1))), budget=pg.Budget(max_samples=3))
        self.assertEqual([len(row) for row in got["first_hit_distance"]], [2, 1])
        self.assertTrue(got["truncated"])

    def test_oversized_grid_is_built_lazily(self):
        args = {"action": "grid", "units": "cm", "sample": "point", "origin": [0, 0, 0], "u": [1, 0, 0],
                "v": [0, 1, 0], "rows": 2000, "columns": 2000}
        got = run(args, FakeSolid(Box((-1, -1, -1), (1, 1, 1))), budget=pg.Budget(max_samples=2000))
        self.assertEqual(got["samples_requested"], 4_000_000)
        self.assertEqual(got["samples_done"], 2000)

    def test_radial_and_axial_truncate(self):
        radial = {"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1],
                  "radii": [1, 2], "angles": [0, 90]}
        axial = {"action": "axial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1], "stations": [1, 2]}
        for args in (radial, axial):
            with self.subTest(action=args["action"]):
                got = run(args, dish(), budget=pg.Budget(max_samples=1))
                self.assertEqual(len(got["results"]), 1)
                self.assertTrue(got["truncated"])

    def test_hit_cap_on_a_ray_reports(self):
        boxes = [Box((2 * i + 1, -1, -1), (2 * i + 2, 1, 1)) for i in range(pg.MAX_HITS_PER_RAY)]
        got = run({"action": "ray", "units": "cm", "origin": [0, 0, 0], "direction": [1, 0, 0]}, FakeSolid(*boxes))
        self.assertEqual(got["hit_count"], pg.MAX_HITS_PER_RAY)
        self.assertEqual(got["truncation_reason"], "hit_cap")


# -- Fusion seam ---------------------------------------------------------------


class _Point:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    @classmethod
    def create(cls, x, y, z):
        return cls(x, y, z)


class _Collection(list):
    @classmethod
    def create(cls):
        return cls()

    count = property(len)

    def add(self, item):
        self.append(item)

    def item(self, i):
        return self[i]


class _Box:
    def __init__(self, bbox):
        self.minPoint, self.maxPoint = _Point(*bbox[0]), _Point(*bbox[1])


class _Occurrence:
    def __init__(self, path, bodies, visible=True):
        self.fullPathName, self.isVisible = path, visible
        self.bRepBodies = _Collection(bodies)
        for body in bodies:
            body.assemblyContext = self


class _Body:
    def __init__(self, name, solid, visible=True, is_solid=True):
        self.name, self.solid, self.isVisible, self.isSolid = name, solid, visible, is_solid
        self.boundingBox = _Box(solid.bbox())
        self.assemblyContext = None

    def pointContainment(self, p):
        return {"inside": 0, "on": 1, "outside": 2}[self.solid.state((p.x, p.y, p.z))]


class _Root:
    """findBRepUsingRay as documented: each body once, nearest first, points in hitPoints."""

    def __init__(self, bodies, occurrences=(), hit_extra_point=False):
        self.bRepBodies = _Collection(bodies)
        self.allOccurrences = _Collection(occurrences)
        self.calls = []
        self.hit_extra_point = hit_extra_point

    def _all(self):
        yield from self.bRepBodies
        for occ in self.allOccurrences:
            yield from occ.bRepBodies

    def findBRepUsingRay(self, origin, direction, entity_type, tolerance, visible_only, hit_points):
        self.calls.append((entity_type, tolerance, visible_only))
        o, d = (origin.x, origin.y, origin.z), (direction.x, direction.y, direction.z)
        found = []
        for body in self._all():
            if visible_only and not body.isVisible:
                continue
            crossings = body.solid.crossings(o, d)
            if crossings:
                found.append((crossings[0], body))
        found.sort(key=lambda hit: hit[0])
        for t, _ in found:
            hit_points.add(_Point(*pg.along(o, d, t)))
        if self.hit_extra_point:
            hit_points.add(_Point(0, 0, 0))
        return _Collection(body for _, body in found)


class FusionLayerTests(unittest.TestCase):
    def setUp(self):
        contain = type("PointContainment", (), {
            "PointInsidePointContainment": 0, "PointOnPointContainment": 1,
            "PointOutsidePointContainment": 2, "UnknownPointContainment": 3})
        entity = type("BRepEntityTypes", (), {"BRepBodyEntityType": "BODY"})
        self.design = type("Design", (), {"cast": staticmethod(lambda product: product)})
        for owner, name, value in (
                (adsk.core, "Point3D", _Point), (adsk.core, "Vector3D", _Point),
                (adsk.core, "ObjectCollection", _Collection), (adsk.fusion, "PointContainment", contain),
                (adsk.fusion, "BRepEntityTypes", entity), (adsk.fusion, "Design", self.design)):
            patcher = mock.patch.object(owner, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, root, arguments):
        product = type("Product", (), {"rootComponent": root})() if root is not None else None
        app = type("App", (), {"activeProduct": product})()
        with mock.patch.object(pg, "get_app", return_value=app):
            return pg.probe_geometry(arguments)

    def _payload(self, root, arguments):
        result = self._call(root, arguments)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def _plate(self, name="Plate", **kwargs):
        return _Body(name, FakeSolid(Box((-1, -1, 1), (1, 1, 2))), **kwargs)

    RAY = {"action": "ray", "units": "cm", "origin": [0, 0, 0], "direction": [0, 0, 1]}

    def test_no_design(self):
        result = self._call(None, self.RAY)
        self.assertTrue(result["isError"])
        self.assertIn("not a Fusion design", result["content"][0]["text"])

    def test_empty_design_refused(self):
        result = self._call(_Root([]), self.RAY)
        self.assertTrue(result["isError"])
        self.assertIn("no visible solid", result["content"][0]["text"])

    def test_ray_sees_entry_and_exit_through_the_real_call(self):
        root = _Root([self._plate()])
        got = self._payload(root, self.RAY)
        self.assertEqual([h["distance"] for h in got["hits"]], [1.0, 2.0])
        self.assertEqual(root.calls[0], ("BODY", -1, True))
        self.assertGreater(len(root.calls), 1)

    def test_occurrence_hit_is_named_by_path(self):
        root = _Root([], [_Occurrence("Arm:1", [self._plate()])])
        got = self._payload(root, self.RAY)
        self.assertEqual(got["hits"][0]["occurrence_path"], "Arm:1")
        self.assertEqual(got["bodies"], ["Arm:1/Plate"])

    def test_non_target_hit_is_reported_not_returned(self):
        other = _Body("Other", FakeSolid(Box((-1, -1, 4), (1, 1, 5))))
        root = _Root([self._plate(), other])
        got = self._payload(root, {**self.RAY, "bodies": [{"occurrence_path": "root", "body_name": "Plate"}]})
        self.assertEqual({h["body"] for h in got["hits"]}, {"Plate"})
        self.assertEqual(got["other_bodies_hit"], ["root/Other"])

    def test_hidden_bodies_skipped_by_default(self):
        root = _Root([self._plate("Seen"), self._plate("Stale", visible=False)])
        self.assertEqual(self._payload(root, self.RAY)["bodies"], ["root/Seen"])

    def test_hidden_occurrence_counts_as_hidden(self):
        root = _Root([self._plate("Seen")], [_Occurrence("Old:1", [self._plate("Stale")], visible=False)])
        self.assertEqual(self._payload(root, self.RAY)["bodies"], ["root/Seen"])

    def test_explicit_hidden_target_widens_the_search(self):
        root = _Root([self._plate("Stale", visible=False)])
        got = self._payload(root, {**self.RAY, "bodies": [{"occurrence_path": "root", "body_name": "Stale"}]})
        self.assertEqual(len(got["hits"]), 2)
        self.assertEqual(root.calls[0], ("BODY", -1, False))

    def test_explicit_surface_body_warns(self):
        root = _Root([self._plate("Skin", is_solid=False)])
        got = self._payload(root, {**self.RAY, "bodies": [{"occurrence_path": "root", "body_name": "Skin"}]})
        self.assertIn("not solid", got["warnings"][0])

    def test_unknown_body_lists_candidates(self):
        result = self._call(_Root([self._plate()]), {**self.RAY, "bodies": [{"occurrence_path": "root", "body_name": "X"}]})
        self.assertTrue(result["isError"])
        self.assertIn("candidates", result["content"][0]["text"])

    def test_containment_maps_states_and_skips_far_points(self):
        plate = self._plate()
        plate.pointContainment = mock.Mock(side_effect=plate.pointContainment)
        got = self._payload(_Root([plate]), {"action": "point", "units": "cm",
                                             "points": [[0, 0, 1.5], [0, 0, 2], [50, 0, 0]]})
        self.assertEqual(got["results"][0]["inside"], ["root/Plate"])
        self.assertEqual(got["results"][1]["on"], ["root/Plate"])
        self.assertEqual(list(got["results"][2]), ["point"])
        self.assertEqual(plate.pointContainment.call_count, 2)

    def test_unknown_containment_is_reported(self):
        plate = self._plate()
        plate.pointContainment = lambda p: 3
        got = self._payload(_Root([plate]), {"action": "point", "units": "cm", "points": [[0, 0, 1.5]]})
        self.assertEqual(got["results"][0]["unknown"], ["root/Plate"])

    def test_mismatched_hit_points_fail_loudly(self):
        result = self._call(_Root([self._plate()], hit_extra_point=True), self.RAY)
        self.assertTrue(result["isError"])
        self.assertIn("hit points", result["content"][0]["text"])

    def test_a_fusion_exception_is_an_error(self):
        root = _Root([self._plate()])
        root.findBRepUsingRay = mock.Mock(side_effect=RuntimeError("boom"))
        result = self._call(root, self.RAY)
        self.assertTrue(result["isError"])
        self.assertIn("boom", result["content"][0]["text"])

    def test_cast_tolerates_none(self):
        root = _Root([self._plate()])
        root.findBRepUsingRay = lambda *args: None
        self.assertEqual(self._payload(root, self.RAY)["hits"], [])

    def test_radial_through_the_real_layer(self):
        rim = _Body("Dish", dish())
        got = self._payload(_Root([rim]), {"action": "radial", "units": "cm", "center": [0, 0, 0], "axis": [0, 0, 1],
                                           "radii": [2, 4.5], "angles": [0]})
        self.assertEqual([r["intervals"] for r in got["results"]], [[[0.0, 1.0]], [[0.0, 3.0]]])
        self.assertIn("elapsed_seconds", got)


class SchemaTests(unittest.TestCase):
    def _tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "probe_geometry")

    def test_required_and_strict(self):
        schema = self._tool()["inputSchema"]
        self.assertEqual(schema["required"], ["action", "units"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["action"]["enum"], list(pg.ACTIONS))
        self.assertEqual(sorted(schema["properties"]["units"]["enum"]), sorted(pg.CM_PER_UNIT))

    def test_every_action_field_is_in_the_schema(self):
        props = set(self._tool()["inputSchema"]["properties"])
        for required, optional in pg._ACTION_FIELDS.values():
            self.assertLessEqual(required | optional, props)
        self.assertLessEqual(pg._COMMON_FIELDS, props)

    def test_no_keywords_clients_reject(self):
        text = json.dumps(self._tool()["inputSchema"])
        for keyword in ("minItems", "minLength", "uniqueItems", "anyOf", "oneOf"):
            self.assertNotIn(keyword, text)


if __name__ == "__main__":
    unittest.main()
