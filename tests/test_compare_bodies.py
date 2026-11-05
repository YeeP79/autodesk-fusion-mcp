"""compare_bodies: schema, argument validation, verdict and consistency logic,
the sampled path, and whole runs against voxel-set fake bodies (no Fusion needed)."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import compare_bodies as cb
from fusion_bridge import tool_surface
from fusion_bridge.common import geometry

Budget = cb.Budget

A = {"occurrence_path": "root", "body_name": "A"}
B = {"occurrence_path": "root", "body_name": "B"}


def options(**overrides):
    opts, err = cb.validate_args({"a": A, "b": B, "units": "cm", **overrides})
    assert err is None, err
    return opts


# -- Fakes -----------------------------------------------------------------------


class Coll:
    def __init__(self, members):
        self.members = list(members)

    @property
    def count(self):
        return len(self.members)

    def item(self, i):
        return self.members[i]


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Box:
    def __init__(self, low, high):
        self.minPoint, self.maxPoint = P(*low), P(*high)


def box_of(cells):
    """Bounds of unit cells, each named by its low corner."""
    low = tuple(min(c[i] for c in cells) for i in range(3))
    high = tuple(max(c[i] for c in cells) + 1 for i in range(3))
    return low, high


def split_lumps(cells):
    """Connected groups of unit cells."""
    left, groups = set(cells), []
    while left:
        stack, group = [left.pop()], set()
        while stack:
            c = stack.pop()
            group.add(c)
            for axis in range(3):
                for step in (-1, 1):
                    n = tuple(v + step * (i == axis) for i, v in enumerate(c))
                    if n in left:
                        left.remove(n)
                        stack.append(n)
        groups.append(group)
    return groups


class Lump:
    def __init__(self, cells):
        self.volume = float(len(cells))
        self.boundingBox = Box(*box_of(cells))


class VoxelBody:
    """A solid as a set of unit cells, enough to run real booleans on."""

    def __init__(self, cells, name="Body", solid=True):
        self.cells, self.name, self.isSolid, self.isVisible = set(cells), name, solid, True
        self.area = 6.0 * len(self.cells)

    @property
    def volume(self):
        return float(len(self.cells))

    @property
    def boundingBox(self):
        return Box(*box_of(self.cells)) if self.cells else None

    @property
    def faces(self):
        return Coll(range(6))

    @property
    def lumps(self):
        return Coll(Lump(group) for group in split_lumps(self.cells))


class Copies:
    """The ``copies`` collaborator over voxel bodies; *fail* and *corrupt* name a boolean call."""

    def __init__(self, a, b, offset=(0, 0, 0), fail=None, corrupt=None):
        self.bodies, self.offset, self.fail, self.corrupt = {"a": a, "b": b}, offset, fail, corrupt
        self.calls = []

    def fresh(self, side):
        cells = self.bodies[side].cells
        if side == "b":
            cells = {tuple(c + o for c, o in zip(cell, self.offset)) for cell in cells}
        return VoxelBody(cells)

    def combine(self, target, tool, kind):
        self.calls.append(kind)
        index = len(self.calls)
        if index == self.fail:
            return False
        target.cells = target.cells & tool.cells if kind == cb.INTERSECTION else target.cells - tool.cells
        if index == self.corrupt:
            target.cells = set()
        return True


def cube(n=2, origin=(0, 0, 0)):
    return {(origin[0] + x, origin[1] + y, origin[2] + z) for x in range(n) for y in range(n) for z in range(n)}


def entry(body, name="A", offset=(0, 0, 0)):
    return {"occurrence_path": "root", "body": name, "index": 0, "visible": True, "solid": body.isSolid,
            "bbox": box_of(body.cells), "_body": body}


def state_of(body):
    def state(point):
        return "inside" if tuple(int(c // 1) for c in point) in body.cells else "outside"
    return state


def run(cells_a, cells_b, offset=(0, 0, 0), budget=None, states=None, **kwargs):
    overrides = kwargs.pop("opts", {})
    a, b = VoxelBody(cells_a), VoxelBody(cells_b)
    opts = options(translate_b=list(offset), **overrides)
    copies = Copies(a, b, offset, **kwargs)
    moved = VoxelBody({tuple(c + o for c, o in zip(cell, offset)) for cell in b.cells})
    return cb.compare(entry(a), entry(b, "B"), opts, copies, states or (state_of(a), state_of(moved)),
                      budget or Budget())


# -- Arguments -------------------------------------------------------------------


class ArgumentTests(unittest.TestCase):
    def refused(self, arguments, text):
        opts, err = cb.validate_args(arguments)
        self.assertIsNone(opts)
        self.assertIn(text, err)

    def test_defaults(self):
        opts, err = cb.validate_args({"a": A, "b": B})
        self.assertIsNone(err)
        self.assertEqual(opts["units"], "in")
        self.assertEqual(opts["a"], ("root", "A", None))
        self.assertAlmostEqual(opts["volume_tolerance"], geometry.to_cm(1e-4, "in", 3))
        self.assertAlmostEqual(opts["length_tolerance"], 0.00254)
        self.assertEqual(opts["max_lumps"], 20)
        self.assertTrue(opts["sample_fallback"])
        self.assertEqual(opts["translate_b"], (0.0, 0.0, 0.0))

    def test_default_tolerances_mean_the_same_in_any_units(self):
        self.assertAlmostEqual(options()["volume_tolerance"], cb.validate_args({"a": A, "b": B})[0]["volume_tolerance"])

    def test_values_convert_from_the_callers_units(self):
        opts = options(units="mm", volume_tolerance=1000, length_tolerance=10, translate_b=[10, 0, -5])
        self.assertAlmostEqual(opts["volume_tolerance"], 1.0)
        self.assertAlmostEqual(opts["length_tolerance"], 1.0)
        self.assertEqual(opts["translate_b"], (1.0, 0.0, -0.5))

    def test_zero_tolerance_is_allowed(self):
        self.assertEqual(options(volume_tolerance=0)["volume_tolerance"], 0.0)

    def test_refusals(self):
        self.refused("x", "object")
        self.refused({"b": B}, "'a' is required")
        self.refused({"a": A}, "'b' is required")
        self.refused({"a": A, "b": "B"}, "'b'")
        self.refused({"a": A, "b": B, "units": "ft"}, "'units'")
        self.refused({"a": A, "b": B, "extra": 1}, "extra")
        self.refused({"a": A, "b": B, "volume_tolerance": -1}, "volume_tolerance")
        self.refused({"a": A, "b": B, "length_tolerance": -0.1}, "length_tolerance")
        self.refused({"a": A, "b": B, "volume_tolerance": True}, "volume_tolerance")
        self.refused({"a": A, "b": B, "length_tolerance": float("nan")}, "length_tolerance")
        self.refused({"a": A, "b": B, "max_lumps": -1}, "max_lumps")
        self.refused({"a": A, "b": B, "sample_fallback": "yes"}, "sample_fallback")
        self.refused({"a": A, "b": B, "translate_b": [1, 2]}, "translate_b")
        self.refused({"a": A, "b": B, "translate_b": [1, 2, "3"]}, "translate_b")


class PickPairTests(unittest.TestCase):
    def setUp(self):
        self.a = entry(VoxelBody(cube()), "A")
        self.b = entry(VoxelBody(cube()), "B")

    def test_picks_both(self):
        a, b, warnings, problem = cb.pick_pair([self.a, self.b], ("root", "A", None), ("root", "B", None))
        self.assertIsNone(problem)
        self.assertEqual((a, b, warnings), (self.a, self.b, []))

    def test_same_body_refused(self):
        _, _, _, problem = cb.pick_pair([self.a, self.b], ("root", "A", None), ("root", "A", 0))
        self.assertIn("same body", problem["error"])

    def test_two_occurrences_of_one_body_are_two_bodies(self):
        one, two = {**self.a, "occurrence_path": "Sub:1"}, {**self.a, "occurrence_path": "Sub:2"}
        _, _, _, problem = cb.pick_pair([one, two], ("Sub:1", "A", None), ("Sub:2", "A", None))
        self.assertIsNone(problem)

    def test_surface_refused(self):
        self.b["solid"] = False
        _, _, _, problem = cb.pick_pair([self.a, self.b], ("root", "A", None), ("root", "B", None))
        self.assertIn("root/B", problem["error"])

    def test_missing_ref_lists_candidates(self):
        _, _, _, problem = cb.pick_pair([self.a, self.b], ("root", "A", None), ("root", "Nope", None))
        self.assertIn("no body matches", problem["error"])
        self.assertEqual(len(problem["candidates"]), 2)

    def test_hidden_body_is_allowed_with_a_warning(self):
        self.b["visible"] = False
        _, _, warnings, problem = cb.pick_pair([self.a, self.b], ("root", "A", None), ("root", "B", None))
        self.assertIsNone(problem)
        self.assertIn("root/B", warnings[0])


# -- Pure logic ------------------------------------------------------------------


class VerdictTests(unittest.TestCase):
    def test_same(self):
        self.assertEqual(cb.verdict(0.0, 0.0, 0.01, 0.01), "same")

    def test_at_both_tolerances_is_same(self):
        self.assertEqual(cb.verdict(0.01, 0.01, 0.01, 0.01), "same")

    def test_over_volume_tolerance(self):
        self.assertEqual(cb.verdict(0.011, 0.0, 0.01, 0.01), "different")

    def test_equal_volume_but_shifted(self):
        self.assertEqual(cb.verdict(0.0, 0.5, 0.01, 0.01), "different")

    def test_missing_measurement_is_unknown(self):
        self.assertEqual(cb.verdict(None, 0.0, 0.01, 0.01), "unknown")
        self.assertEqual(cb.verdict(0.0, None, 0.01, 0.01), "unknown")

    def test_uncertainty_straddling_the_tolerance_is_unknown(self):
        self.assertEqual(cb.verdict(0.0, 0.0, 0.01, 0.01, uncertainty=0.02), "unknown")
        self.assertEqual(cb.verdict(0.02, 0.0, 0.01, 0.01, uncertainty=0.02), "unknown")

    def test_uncertainty_clear_of_the_tolerance_decides(self):
        self.assertEqual(cb.verdict(0.0, 0.0, 0.01, 0.01, uncertainty=0.005), "same")
        self.assertEqual(cb.verdict(1.0, 0.0, 0.01, 0.01, uncertainty=0.5), "different")


class ConsistentTests(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(cb.consistent(8.0, 8.0, 7.9, 0.1, 0.1, 0.01)[2], True)

    def test_boundary(self):
        gap_a, _, ok = cb.consistent(8.0, 8.0, 7.5, 0.25, 0.5, 0.25)
        self.assertEqual(gap_a, 0.25)
        self.assertTrue(ok)
        self.assertFalse(cb.consistent(8.0, 8.0, 7.5, 0.25, 0.5, 0.2499)[2])

    def test_either_side_can_fail(self):
        self.assertFalse(cb.consistent(8.0, 8.0, 7.0, 1.0, 0.0, 0.01)[2])
        self.assertFalse(cb.consistent(8.0, 8.0, 7.0, 0.0, 1.0, 0.01)[2])

    def test_a_large_body_gets_relative_room(self):
        self.assertTrue(cb.consistent(4e6, 4e6, 4e6 - 1.0, 0.0, 0.0, 1e-4)[2])

    def test_a_missing_value_is_not_consistent(self):
        self.assertEqual(cb.consistent(None, 8.0, 8.0, 0.0, 0.0, 0.01), (None, None, False))
        self.assertFalse(cb.consistent(8.0, 8.0, None, 0.0, 0.0, 0.01)[2])


class GridTests(unittest.TestCase):
    def test_grid_is_close_to_cubic_and_within_the_cap(self):
        self.assertEqual(cb.grid_dims((0, 0, 0), (1, 1, 1), 1000), (10, 10, 10))
        dims = cb.grid_dims((0, 0, 0), (10, 1, 1), 1000)
        self.assertGreater(dims[0], dims[1])
        self.assertLessEqual(dims[0] * dims[1] * dims[2], 1000)

    def test_thin_box_stays_within_the_cap(self):
        dims = cb.grid_dims((0, 0, 0), (100, 100, 0.001), 2000)
        self.assertEqual(dims[2], 1)
        self.assertLessEqual(dims[0] * dims[1], 2000)

    def test_flat_and_empty_boxes(self):
        self.assertEqual(cb.grid_dims((0, 0, 0), (2, 2, 0), 4)[2], 1)
        self.assertEqual(cb.grid_dims((0, 0, 0), (0, 0, 0), 4), (1, 1, 1))

    def test_cell_centers(self):
        self.assertEqual(list(cb.cell_centers((0, 0, 0), (2, 1, 1), (2, 1, 1))), [(0.5, 0.5, 0.5), (1.5, 0.5, 0.5)])

    def test_estimate(self):
        counts = {"both": 10, "a_only": 3, "b_only": 1, "neither": 2, "ambiguous": 2, "unsampled": 1}
        estimate = cb.grid_estimate(counts, 0.5)
        self.assertEqual((estimate["overlap"], estimate["a_only"], estimate["b_only"]), (5.0, 1.5, 0.5))
        self.assertAlmostEqual(estimate["uncertainty"], 0.5 * (1 + 2 + 2 + 1))

    def test_no_differing_cells_still_carry_one_cell_of_doubt(self):
        counts = {"both": 10, "a_only": 0, "b_only": 0, "neither": 0, "ambiguous": 0, "unsampled": 0}
        self.assertEqual(cb.grid_estimate(counts, 0.5)["uncertainty"], 0.5)

    def test_boundary_and_unread_states_are_ambiguous(self):
        grid = cb.sample_regions(lambda p: "on", lambda p: "inside", ((0, 0, 0), (1, 1, 1)), Budget(), max_cells=8)
        self.assertEqual(grid["counts"]["ambiguous"], 8)
        grid = cb.sample_regions(lambda p: "inside", lambda p: "unknown", ((0, 0, 0), (1, 1, 1)), Budget(), max_cells=8)
        self.assertEqual(grid["counts"]["ambiguous"], 8)

    def test_budget_leaves_cells_unsampled(self):
        grid = cb.sample_regions(lambda p: "inside", lambda p: "inside", ((0, 0, 0), (1, 1, 1)),
                                 Budget(max_samples=3), max_cells=8)
        self.assertEqual(grid["counts"]["both"], 3)
        self.assertEqual(grid["counts"]["unsampled"], 5)

    def test_difference_box_covers_whole_cells(self):
        grid = cb.sample_regions(lambda p: "inside", lambda p: "outside" if p[0] > 1 else "inside",
                                 ((0, 0, 0), (2, 2, 2)), Budget(), max_cells=8)
        self.assertEqual(grid["boxes"]["a_only"], ((1.0, 0.0, 0.0), (2.0, 2.0, 2.0)))
        self.assertIsNone(grid["boxes"]["b_only"])
        self.assertEqual(grid["cell_volume"], 1.0)


class LumpTests(unittest.TestCase):
    def test_capped_largest_first(self):
        lumps = [{"volume": v} for v in (1, 3, 2)]
        self.assertEqual([x["volume"] for x in cb.capped_lumps(lumps, 2)], [3, 2])

    def test_empty_result_is_zero_volume(self):
        self.assertEqual(cb.volume_of(VoxelBody(set())), 0.0)
        self.assertEqual(cb.region_of(VoxelBody(set()), 5), {"volume": 0.0, "lumps": [], "lump_count": 0})

    def test_an_unreadable_volume_is_a_failure_not_zero(self):
        class Unreadable(VoxelBody):
            volume = None

        with self.assertRaises(RuntimeError):
            cb.volume_of(Unreadable(cube()))


# -- Whole runs ------------------------------------------------------------------


class CompareTests(unittest.TestCase):
    def test_identical_bodies_are_same(self):
        report = run(cube(), cube())
        self.assertEqual(report["verdict"], "same")
        self.assertEqual(report["method"], "temporary_brep")
        self.assertEqual(report["overlap"], {"volume": 8.0, "percent_of_a": 100.0, "percent_of_b": 100.0})
        self.assertEqual(report["symmetric_difference"]["volume"], 0.0)
        self.assertEqual(report["symmetric_difference"]["a_only"]["lumps"], [])
        self.assertTrue(report["consistency"]["ok"])
        self.assertEqual(report["warnings"], [])

    def test_equal_volume_shifted_is_different(self):
        report = run(cube(), cube(origin=(1, 0, 0)))
        self.assertEqual(report["verdict"], "different")
        self.assertEqual(report["delta"]["volume"], 0.0)
        self.assertEqual(report["delta"]["bbox_max_shift"], 1.0)

    def test_a_missing_notch_is_located(self):
        report = run(cube(3), cube(3) - {(2, 2, 2)})
        self.assertEqual(report["verdict"], "different")
        a_only = report["symmetric_difference"]["a_only"]
        self.assertEqual(a_only["volume"], 1.0)
        self.assertEqual(a_only["lumps"], [{"volume": 1.0, "bbox": [[2.0, 2.0, 2.0], [3.0, 3.0, 3.0]]}])
        self.assertEqual(report["symmetric_difference"]["b_only"]["volume"], 0.0)
        self.assertEqual(report["delta"]["percent"], -3.704)

    def test_same_box_and_volume_with_different_cells_is_different(self):
        a = cube(3) - {(0, 0, 0)}
        b = cube(3) - {(0, 0, 2)}
        report = run(a, b)
        self.assertEqual(report["delta"]["volume"], 0.0)
        self.assertEqual(report["delta"]["bbox_max_shift"], 0.0)
        self.assertEqual(report["verdict"], "different")

    def test_disjoint_bodies_skip_the_booleans(self):
        a, b = VoxelBody(cube()), VoxelBody(cube(origin=(10, 0, 0)))
        copies = Copies(a, b)
        report = cb.compare(entry(a), entry(b, "B"), options(), copies, (None, None), Budget())
        self.assertEqual(copies.calls, [])
        self.assertEqual(report["verdict"], "different")
        self.assertEqual(report["overlap"]["volume"], 0.0)
        self.assertEqual(report["symmetric_difference"]["volume"], 16.0)
        self.assertTrue(report["consistency"]["ok"])

    def test_translate_moves_b_only(self):
        report = run(cube(), cube(origin=(5, 0, 0)), offset=(-5, 0, 0))
        self.assertEqual(report["verdict"], "same")
        self.assertEqual(report["a"]["bbox"], [[0, 0, 0], [2, 2, 2]])
        self.assertEqual(report["b"]["bbox"], [[0, 0, 0], [2, 2, 2]])
        self.assertEqual(report["translate_b"], [-5, 0, 0])

    def test_lumps_are_capped_but_counted(self):
        a = cube(1) | cube(1, (2, 0, 0)) | cube(1, (4, 0, 0)) | cube(9, (0, 5, 0))
        report = run(a, cube(9, (0, 5, 0)), opts={"max_lumps": 2})
        a_only = report["symmetric_difference"]["a_only"]
        self.assertEqual(a_only["lump_count"], 3)
        self.assertEqual(len(a_only["lumps"]), 2)
        self.assertEqual(a_only["volume"], 3.0)

    def test_volumes_report_in_the_callers_units(self):
        report = run(cube(), cube(), opts={"units": "mm"})
        self.assertEqual(report["units"]["volume"], "mm^3")
        self.assertEqual(report["a"]["volume"], 8000.0)
        self.assertEqual(report["a"]["bbox"][1], [20.0, 20.0, 20.0])
        self.assertEqual(report["delta"]["bbox_max_shift"], 0.0)


class FallbackTests(unittest.TestCase):
    def test_failed_boolean_samples_and_finds_a_real_difference(self):
        report = run(cube(4), cube(4) - cube(2), fail=1)
        self.assertEqual(report["method"], "sampled")
        self.assertEqual(report["verdict"], "different")
        self.assertIn("failed", report["warnings"][0])
        self.assertIn("estimates", report["warnings"][1])
        self.assertAlmostEqual(report["symmetric_difference"]["a_only"]["volume"], 8.0, delta=1.0)
        self.assertEqual(len(report["symmetric_difference"]["a_only"]["lumps"]), 1)
        self.assertIsNone(report["consistency"]["ok"])

    def test_inconsistent_booleans_never_say_same(self):
        # An emptied intersection leaves both differences empty: it looks like a match until checked.
        report = run(cube(), cube(), corrupt=1, opts={"sample_fallback": False})
        self.assertEqual(report["verdict"], "unknown")
        self.assertEqual(report["method"], "temporary_brep")
        self.assertFalse(report["consistency"]["ok"])
        self.assertEqual(report["consistency"]["a_minus_overlap_vs_a_only"], 8.0)

    def test_an_emptied_difference_is_caught_too(self):
        report = run(cube(3), cube(3) - {(2, 2, 2)}, corrupt=2, opts={"sample_fallback": False})
        self.assertEqual(report["verdict"], "unknown")

    def test_a_clean_grid_is_not_proof_of_same(self):
        report = run(cube(), cube(), fail=1)
        self.assertEqual(report["method"], "sampled")
        self.assertEqual(report["symmetric_difference"]["volume"], 0.0)
        self.assertEqual(report["verdict"], "unknown")

    def test_a_clean_grid_within_a_loose_tolerance_is_same(self):
        report = run(cube(), cube(), fail=1, opts={"volume_tolerance": 1.0})
        self.assertEqual(report["verdict"], "same")

    def test_no_fallback_leaves_unknown(self):
        report = run(cube(), cube(), fail=1, opts={"sample_fallback": False})
        self.assertEqual(report["verdict"], "unknown")
        self.assertEqual(report["method"], "temporary_brep")
        self.assertIsNone(report["symmetric_difference"]["volume"])

    def test_both_methods_failing_is_unknown_not_an_exception(self):
        def broken(point):
            raise RuntimeError("no containment")

        report = run(cube(), cube(), fail=1, states=(broken, broken))
        self.assertEqual(report["verdict"], "unknown")
        self.assertEqual(report["method"], "sampled")
        self.assertIn("sampling failed", report["warnings"][1])

    def test_an_exhausted_budget_cannot_say_same(self):
        report = run(cube(), cube(), fail=1, budget=Budget(seconds=0), opts={"volume_tolerance": 1.0})
        self.assertEqual(report["verdict"], "unknown")
        self.assertIn("sampled 0 of", report["warnings"][1])

    def test_a_copy_that_cannot_be_placed_falls_back(self):
        a, b = VoxelBody(cube()), VoxelBody(cube())

        class NoCopy(Copies):
            def fresh(self, side):
                raise RuntimeError("did not land")

        report = cb.compare(entry(a), entry(b, "B"), options(), NoCopy(a, b), (state_of(a), state_of(b)), Budget())
        self.assertEqual(report["method"], "sampled")

    def test_a_wrong_call_is_not_swallowed_as_a_geometry_failure(self):
        a, b = VoxelBody(cube()), VoxelBody(cube())

        class Wrong(Copies):
            def combine(self, target, tool, kind):
                raise TypeError("bad arguments")

        with self.assertRaises(TypeError):
            cb.compare(entry(a), entry(b, "B"), options(), Wrong(a, b), (None, None), Budget())


class TemporaryCopiesTests(unittest.TestCase):
    class Manager:
        def __init__(self, transform_ok=True):
            self.transformed, self.transform_ok = [], transform_ok

        def copy(self, body):
            return VoxelBody(body.cells)

        def transform(self, body, matrix):
            self.transformed.append(body)
            return self.transform_ok

    def setUp(self):
        self.a, self.b = VoxelBody(cube()), VoxelBody(cube(origin=(5, 0, 0)))

    def test_translate_applies_to_b_only(self):
        manager = self.Manager()
        copies = cb.TemporaryCopies(manager, self.a, self.b, (1.0, 0.0, 0.0))
        with mock.patch.object(cb, "_translation", return_value="matrix"):
            copy_a, copy_b = copies.fresh("a"), copies.fresh("b")
        self.assertEqual(manager.transformed, [copy_b])
        self.assertIsNot(copy_a, copy_b)

    def test_zero_translate_is_no_transform(self):
        manager = self.Manager()
        cb.TemporaryCopies(manager, self.a, self.b, (0.0, 0.0, 0.0)).fresh("b")
        self.assertEqual(manager.transformed, [])

    def test_failed_translate_raises(self):
        copies = cb.TemporaryCopies(self.Manager(transform_ok=False), self.a, self.b, (1.0, 0.0, 0.0))
        with mock.patch.object(cb, "_translation", return_value="matrix"), self.assertRaises(RuntimeError):
            copies.fresh("b")

    def test_every_fresh_call_is_a_new_copy(self):
        copies = cb.TemporaryCopies(self.Manager(), self.a, self.b, (0.0, 0.0, 0.0))
        self.assertIsNot(copies.fresh("a"), copies.fresh("a"))


class HandlerTests(unittest.TestCase):
    def text(self, result):
        return result["content"][0]["text"]

    def test_bad_arguments_never_reach_fusion(self):
        with mock.patch.object(cb, "get_app") as get_app:
            result = cb.compare_bodies({"a": A})
        self.assertTrue(result["isError"])
        get_app.assert_not_called()

    def design(self):
        a, b = VoxelBody(cube(), "A"), VoxelBody(cube(), "B")
        return mock.Mock(rootComponent=mock.Mock(bRepBodies=Coll([a, b]), allOccurrences=Coll([])))

    def call(self, design, arguments):
        fusion = mock.Mock()
        fusion.Design.cast.return_value = design
        with mock.patch.object(cb, "get_app"), mock.patch.object(cb.adsk, "fusion", fusion, create=True), \
                mock.patch.object(cb, "TemporaryCopies", Copies_for), mock.patch.object(cb, "FusionProbe"):
            return cb.compare_bodies(arguments)

    def test_no_design(self):
        result = self.call(None, {"a": A, "b": B})
        self.assertTrue(result["isError"])
        self.assertIn("not a Fusion design", self.text(result))

    def test_same_body_is_an_error_with_a_reason(self):
        result = self.call(self.design(), {"a": A, "b": A})
        self.assertTrue(result["isError"])
        self.assertIn("same body", json.loads(self.text(result))["error"])

    def test_a_run_reports_json(self):
        result = self.call(self.design(), {"a": A, "b": B, "units": "cm"})
        self.assertFalse(result["isError"], self.text(result))
        payload = json.loads(self.text(result))
        self.assertEqual(payload["verdict"], "same")
        self.assertIn("elapsed_seconds", payload)

    def test_unknown_is_an_answer_not_an_error(self):
        with mock.patch.object(cb, "boolean_parts", side_effect=RuntimeError("no")):
            result = self.call(self.design(), {"a": A, "b": B, "sample_fallback": False})
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(self.text(result))["verdict"], "unknown")


def Copies_for(manager, body_a, body_b, translate):
    return Copies(body_a, body_b)


class SchemaTests(unittest.TestCase):
    def _tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == tool_surface.COMPARE_BODIES)

    def test_schema_matches_the_handler(self):
        schema = self._tool()["inputSchema"]
        self.assertEqual(set(schema["properties"]), cb._KNOWN)
        self.assertEqual(schema["required"], ["a", "b"])
        self.assertFalse(schema["additionalProperties"])

    def test_refs_take_body_index(self):
        self.assertIn("body_index", self._tool()["inputSchema"]["properties"]["a"]["properties"])

    def test_no_keywords_clients_reject(self):
        text = json.dumps(self._tool()["inputSchema"])
        for keyword in ("minItems", "minLength", "uniqueItems", "anyOf", "oneOf"):
            self.assertNotIn(keyword, text)


if __name__ == "__main__":
    unittest.main()
