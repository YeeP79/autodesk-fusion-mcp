"""check_solids: schema, argument validation, metric/threshold/diff logic and
an end-to-end run against fake Fusion objects (no Fusion needed)."""

import math
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import solid_check as sc
from fusion_bridge import tool_surface

IN = 2.54  # cm per inch


def _defaults():
    opts, err = sc.validate_args({})
    assert err is None, err
    return opts


def _m(key="root/Body1", **kw):
    base = {"key": key, "occurrence_path": "root", "component": "Root", "is_solid": True,
            "volume_cm3": 10.0, "lumps": 1, "shells": 1, "face_count": 6, "bbox": ((0, 0, 0), (1, 1, 1))}
    base.update(kw)
    return base


# -- Fakes -----------------------------------------------------------------------


class Coll:
    def __init__(self, items):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]

    def add(self, x):
        self._items.append(x)


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class BB:
    def __init__(self, lo, hi):
        self.minPoint, self.maxPoint = P(*lo), P(*hi)


class Edge:
    def __init__(self, length):
        self.length = length


class Face:
    def __init__(self, area, edge_lengths):
        self.area = area
        self.edges = Coll(Edge(x) for x in edge_lengths)


class Body:
    def __init__(self, name, volume, faces=None, lumps=1, shells=1, solid=True, visible=True,
                 bbox=((0, 0, 0), (1, 1, 1)), ctx=None):
        self.name, self.volume, self.isSolid, self.isVisible = name, volume, solid, visible
        faces = faces if faces is not None else [Face(1.0, [1, 1, 1, 1]) for _ in range(6)]
        self.faces = Coll(faces)
        self.edges = Coll(e for f in faces for e in f.edges._items)
        self.lumps, self.shells = Coll(range(lumps)), Coll(range(shells))
        self.boundingBox = BB(*bbox)
        self.assemblyContext = ctx


class Comp:
    def __init__(self, name, bodies=(), occurrences=()):
        self.name = name
        self.bRepBodies = Coll(bodies)
        self.allOccurrences = Coll(occurrences)


class Occ:
    def __init__(self, path, component, bodies, visible=True):
        self.fullPathName, self.component, self.isVisible = path, component, visible
        self.bRepBodies = Coll(bodies)
        for b in bodies:
            b.assemblyContext = self


class Design:
    def __init__(self, root, interference=None):
        self.rootComponent = root
        self._interference = interference or []
        if interference is not None:
            self.createInterferenceInput = lambda coll: type("In", (), {"entities": coll})()

    def analyzeInterference(self, interference_input):
        present = interference_input.entities._items
        results = [r for r in self._interference if r.entityOne in present and r.entityTwo in present]
        return Coll(results)


class IResult:
    def __init__(self, a, b, volume):
        self.entityOne, self.entityTwo = a, b
        self.interferenceBody = type("B", (), {"volume": volume})()


# -- Schema ------------------------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def _schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "check_solids")["inputSchema"]

    def test_strict_and_optional(self):
        schema = self._schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required", schema)

    def test_checks_enum_matches_module(self):
        self.assertEqual(self._schema()["properties"]["checks"]["items"]["enum"], sc.CHECKS)

    def test_body_ref_strict(self):
        ref = self._schema()["properties"]["bodies"]["items"]
        self.assertFalse(ref["additionalProperties"])
        self.assertEqual(ref["required"], ["occurrence_path", "body_name"])

    def test_interference_pairs_accepts_all_or_pairs(self):
        from lib.mcp_protocol import validate_arguments
        schema = self._schema()
        ref = {"occurrence_path": "root", "body_name": "B"}
        self.assertIsNone(validate_arguments({"interference_pairs": "all"}, schema))
        self.assertIsNone(validate_arguments({"interference_pairs": [[ref, ref]]}, schema))
        self.assertIsNotNone(validate_arguments({"interference_pairs": [[{"body_name": "B"}]]}, schema))


# -- Validation ---------------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    def test_defaults(self):
        opts = _defaults()
        self.assertEqual(opts["checks"], ["validity", "slivers", "lumps", "volume_change"])
        self.assertEqual(opts["units"], "in")
        self.assertTrue(opts["snapshot"])
        self.assertAlmostEqual(opts["thresholds"]["min_face_area"], 1e-4 * IN ** 2)
        self.assertAlmostEqual(opts["thresholds"]["min_edge_length"], 1e-3 * IN)

    def test_thresholds_in_caller_units(self):
        opts, err = sc.validate_args({"units": "mm", "min_face_area": 1.0, "min_edge_length": 1.0,
                                      "min_overlap_volume": 1000.0})
        self.assertIsNone(err)
        self.assertAlmostEqual(opts["thresholds"]["min_face_area"], 0.01)
        self.assertAlmostEqual(opts["thresholds"]["min_edge_length"], 0.1)
        self.assertAlmostEqual(opts["thresholds"]["min_overlap_volume"], 1.0)

    def test_pairs_imply_interference(self):
        opts, _ = sc.validate_args({"interference_pairs": "all"})
        self.assertIn("interference", opts["checks"])
        opts, _ = sc.validate_args({"checks": ["validity"], "interference_pairs": "all"})
        self.assertEqual(opts["checks"], ["validity"])

    def test_token_disables_default_snapshot(self):
        opts, _ = sc.validate_args({"baseline_token": "abc"})
        self.assertFalse(opts["snapshot"])
        opts, _ = sc.validate_args({"baseline_token": "abc", "snapshot": True})
        self.assertTrue(opts["snapshot"])

    def test_rejections(self):
        ref = {"occurrence_path": "root", "body_name": "B"}
        bad = [
            ("arguments", None),
            ("units", {"units": "ft"}),
            ("checks", {"checks": []}),
            ("checks", {"checks": ["volume"]}),
            ("bodies", {"bodies": []}),
            ("bodies[0]", {"bodies": [{"occurrence_path": "root"}]}),
            ("bodies[0]", {"bodies": [{"occurrence_path": "root", "body_name": "B", "x": 1}]}),
            ("interference_pairs", {"interference_pairs": "some"}),
            ("interference_pairs[0]", {"interference_pairs": [[ref]]}),
            ("itself", {"interference_pairs": [[ref, ref]]}),
            ("interference_pairs", {"interference_pairs": [[ref, {**ref, "body_name": "C"}]] * 301}),
            ("baseline_token", {"baseline_token": ""}),
            ("snapshot", {"snapshot": "yes"}),
            ("edited_component", {"edited_component": " "}),
            ("min_face_area", {"min_face_area": 0}),
            ("min_edge_length", {"min_edge_length": True}),
            ("sliver_width", {"sliver_width": math.nan}),
            ("max_details", {"max_details": 0}),
        ]
        for needle, args in bad:
            with self.subTest(args=args):
                opts, err = sc.validate_args(args)
                self.assertIsNone(opts)
                self.assertIn(needle, err)

    def test_handler_rejects_before_fusion(self):
        result = sc.check_solids({"units": "yards"})
        self.assertTrue(result["isError"])
        self.assertIn("units", result["content"][0]["text"])


# -- Identity and selection ----------------------------------------------------------


class IdentityTests(unittest.TestCase):
    def _entries(self):
        return sc.assign_keys([
            {"occurrence_path": "root", "body": "A", "index": 0, "visible": True, "occurrence_visible": None},
            {"occurrence_path": "Frame:1", "body": "Tube", "index": 0, "visible": True, "occurrence_visible": True},
            {"occurrence_path": "Frame:1", "body": "Tube", "index": 1, "visible": False, "occurrence_visible": True},
            {"occurrence_path": "Hidden:1", "body": "B", "index": 0, "visible": True, "occurrence_visible": False},
        ])

    def test_keys_disambiguate_duplicates_only(self):
        self.assertEqual([e["key"] for e in self._entries()],
                         ["root/A", "Frame:1/Tube#0", "Frame:1/Tube#1", "Hidden:1/B"])

    def test_default_selection_is_visible(self):
        selected, unresolved = sc.select_entries(self._entries(), None)
        self.assertEqual([e["key"] for e in selected], ["root/A", "Frame:1/Tube#0"])
        self.assertEqual(unresolved, [])

    def test_explicit_selection_includes_hidden_and_reports_missing(self):
        selected, unresolved = sc.select_entries(self._entries(), [("Hidden:1", "B"), ("root", "Nope")])
        self.assertEqual([e["key"] for e in selected], ["Hidden:1/B"])
        self.assertEqual(unresolved, [("root", "Nope")])

    def test_in_component(self):
        e = {"occurrence_path": "Frame:1+Gusset:1", "component": "Gusset"}
        self.assertTrue(sc.in_component(e, "Gusset"))
        self.assertTrue(sc.in_component(e, "Frame:1"))
        self.assertTrue(sc.in_component(e, "Frame:1+Gusset:1"))
        self.assertFalse(sc.in_component(e, "Frame:10"))
        self.assertFalse(sc.in_component(e, "Frame"))
        self.assertFalse(sc.in_component(e, None))


# -- Per-body checks ----------------------------------------------------------------


class ValidityTests(unittest.TestCase):
    def setUp(self):
        self.t = _defaults()["thresholds"]

    def types(self, metrics):
        return [p["type"] for p in sc.validity_problems(metrics, self.t, "in")]

    def test_clean_body(self):
        self.assertEqual(self.types(_m(faces=[{"index": 0, "area": 1.0, "perimeter": 4.0}],
                                       edge_lengths=[1.0])), [])

    def test_non_solid_is_not_also_zero_volume(self):
        self.assertEqual(self.types(_m(is_solid=False, volume_cm3=0.0)), ["non_solid"])

    def test_zero_and_negative_and_missing_volume(self):
        for vol in (0.0, -1.0, None, 1e-9):
            with self.subTest(vol=vol):
                self.assertEqual(self.types(_m(volume_cm3=vol)), ["zero_volume"])

    def test_tiny_faces_and_short_edges(self):
        faces = [{"index": i, "area": a, "perimeter": 1.0} for i, a in enumerate([1.0, 1e-6, 1e-5])]
        problems = sc.validity_problems(_m(faces=faces, edge_lengths=[1.0, 1e-4]), self.t, "in")
        tiny = next(p for p in problems if p["type"] == "tiny_faces")
        self.assertEqual(tiny["count"], 2)
        self.assertEqual(tiny["examples"][0]["face_index"], 1)
        short = next(p for p in problems if p["type"] == "short_edges")
        self.assertEqual(short["count"], 1)
        self.assertAlmostEqual(short["shortest"], 1e-4 / IN, places=6)

    def test_threshold_boundary_is_exclusive(self):
        faces = [{"index": 0, "area": self.t["min_face_area"], "perimeter": 1.0}]
        self.assertEqual(self.types(_m(faces=faces, edge_lengths=[self.t["min_edge_length"]])), [])


class SliverTests(unittest.TestCase):
    def setUp(self):
        self.t = _defaults()["thresholds"]

    def test_face_width(self):
        self.assertAlmostEqual(sc.face_width(10.0 * 0.01, 2 * (10.0 + 0.01)), 0.01 * 10 / 10.01)
        self.assertIsNone(sc.face_width(1.0, 0.0))

    def test_long_thin_strip_is_sliver(self):
        # 5 in x 0.0005 in strip.
        w, length = 0.0005 * IN, 5 * IN
        faces = [{"index": 3, "area": w * length, "perimeter": 2 * (w + length)}]
        problems = sc.sliver_problems(_m(faces=faces), self.t, "in")
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["examples"][0]["face_index"], 3)
        self.assertAlmostEqual(problems[0]["examples"][0]["width"], 0.0005, places=5)

    def test_tube_end_annulus_is_not_sliver(self):
        # 1.75 OD x 0.065 wall end face: mean width = wall.
        R, r = 0.875 * IN, (0.875 - 0.065) * IN
        faces = [{"index": 0, "area": math.pi * (R * R - r * r), "perimeter": 2 * math.pi * (R + r)}]
        self.assertEqual(sc.sliver_problems(_m(faces=faces), self.t, "in"), [])

    def test_small_thin_face_below_aspect_is_not_sliver(self):
        # Tiny square: thin but not long (tiny_faces covers it).
        s = 0.0001 * IN
        faces = [{"index": 0, "area": s * s, "perimeter": 4 * s}]
        self.assertEqual(sc.sliver_problems(_m(faces=faces), self.t, "in"), [])

    def test_no_faces(self):
        self.assertEqual(sc.sliver_problems(_m(), self.t, "in"), [])
        self.assertEqual(sc.sliver_problems(_m(faces=[]), self.t, "in"), [])

    def test_face_count_outlier(self):
        bodies = [_m(f"b{i}", face_count=12) for i in range(4)] + [_m("fin", face_count=400)]
        problems = sc.face_count_outliers(bodies, 5.0)
        self.assertEqual([p["body"] for p in problems], ["fin"])
        self.assertEqual(problems[0]["median_face_count"], 12)

    def test_face_count_outlier_needs_peers_and_excess(self):
        self.assertEqual(sc.face_count_outliers([_m("a", face_count=6), _m("b", face_count=600)], 5.0), [])
        # 6 * 5 = 30 but the +20 floor is 26: 31 faces exceeds both.
        bodies = [_m(f"b{i}", face_count=6) for i in range(3)] + [_m("x", face_count=31)]
        self.assertEqual(len(sc.face_count_outliers(bodies, 5.0)), 1)
        bodies[-1]["face_count"] = 30
        self.assertEqual(sc.face_count_outliers(bodies, 5.0), [])

    def test_median(self):
        self.assertIsNone(sc.median([]))
        self.assertEqual(sc.median([3, 1, 2]), 2)
        self.assertEqual(sc.median([4, 1, 2, 3]), 2.5)


class LumpTests(unittest.TestCase):
    def test_single_lump_clean(self):
        self.assertEqual(sc.lump_problems(_m()), [])

    def test_multiple_lumps_and_voids(self):
        types = [p["type"] for p in sc.lump_problems(_m(lumps=2, shells=3))]
        self.assertEqual(types, ["multiple_lumps", "extra_shells"])

    def test_missing_counts(self):
        self.assertEqual(sc.lump_problems(_m(lumps=None, shells=None)), [])


# -- Interference --------------------------------------------------------------------


class InterferenceTests(unittest.TestCase):
    def test_bbox_overlap(self):
        a = ((0, 0, 0), (1, 1, 1))
        self.assertTrue(sc.bbox_overlap(a, ((0.5, 0.5, 0.5), (2, 2, 2))))
        self.assertTrue(sc.bbox_overlap(a, ((1, 0, 0), (2, 1, 1))))  # touching
        self.assertFalse(sc.bbox_overlap(a, ((1.1, 0, 0), (2, 1, 1))))
        self.assertTrue(sc.bbox_overlap(None, a))

    def _entries(self, n):
        return sc.assign_keys([{"occurrence_path": "root", "body": f"B{i}", "index": i, "is_solid": True}
                               for i in range(n)])

    def test_all_pairs_and_cap(self):
        entries = self._entries(4)
        pairs, err = sc.resolve_pairs(entries, "all", entries)
        self.assertIsNone(err)
        self.assertEqual(len(pairs), 6)
        big = self._entries(26)  # 325 pairs
        pairs, err = sc.resolve_pairs(big, "all", big)
        self.assertIsNone(pairs)
        self.assertIn("325", err)

    def test_all_pairs_skip_non_solid(self):
        entries = self._entries(3)
        entries[1]["is_solid"] = False
        pairs, _ = sc.resolve_pairs(entries, "all", entries)
        self.assertEqual([(a["body"], b["body"]) for a, b in pairs], [("B0", "B2")])

    def test_explicit_pairs(self):
        entries = self._entries(3)
        pairs, err = sc.resolve_pairs([], [(("root", "B0"), ("root", "B2"))], entries)
        self.assertIsNone(err)
        self.assertEqual([(a["body"], b["body"]) for a, b in pairs], [("B0", "B2")])
        pairs, err = sc.resolve_pairs([], [(("root", "B0"), ("root", "Nope"))], entries)
        self.assertIn("Nope", err)

    def test_interference_threshold_and_order(self):
        t = _defaults()["thresholds"]
        rows = sc.interference_problems([("a", "b", 1e-9), ("c", "d", 2.0), ("e", "f", 5.0), ("g", "h", None)],
                                        t, "cm")
        self.assertEqual([(r["body"], r["other_body"], r["overlap_volume"]) for r in rows],
                         [("e", "f", 5.0), ("c", "d", 2.0)])
        self.assertTrue(all(r["severity"] == "error" for r in rows))


# -- Baselines and diff --------------------------------------------------------------


class DiffTests(unittest.TestCase):
    def rec(self, volume=10.0, faces=6, bbox=((0, 0, 0), (1, 1, 1)), path="Frame:1", comp="Frame"):
        return {"volume": volume, "faces": faces, "bbox": bbox, "occurrence_path": path, "component": comp}

    def test_no_change(self):
        self.assertEqual(sc.diff_snapshots({"k": self.rec()}, {"k": self.rec(volume=10.0 + 1e-7)}, None, "cm"), [])

    def test_cut_reaching_other_component_is_error(self):
        before = {"Frame:1/T": self.rec(), "Plate:1/P": self.rec(path="Plate:1", comp="Plate")}
        after = {"Frame:1/T": self.rec(volume=9.0, faces=8),
                 "Plate:1/P": self.rec(volume=8.0, path="Plate:1", comp="Plate")}
        rows = sc.diff_snapshots(before, after, "Frame", "cm")
        by = {(r["body"], r["type"]): r for r in rows}
        self.assertEqual(by[("Frame:1/T", "volume_changed")]["severity"], "info")
        self.assertFalse(by[("Frame:1/T", "volume_changed")]["outside_edited_component"])
        self.assertEqual(by[("Frame:1/T", "face_count_changed")]["severity"], "info")
        plate = by[("Plate:1/P", "volume_changed")]
        self.assertEqual(plate["severity"], "error")
        self.assertTrue(plate["outside_edited_component"])
        self.assertEqual(plate["delta"], -2.0)
        self.assertEqual(plate["percent"], -20.0)

    def test_growth_outside_is_warning(self):
        rows = sc.diff_snapshots({"k": self.rec(path="X:1", comp="X")},
                                 {"k": self.rec(volume=11.0, path="X:1", comp="X")}, "Frame", "cm")
        self.assertEqual(rows[0]["severity"], "warning")

    def test_without_edited_component_everything_warns(self):
        rows = sc.diff_snapshots({"k": self.rec()}, {"k": self.rec(volume=1.0)}, None, "cm")
        self.assertEqual(rows[0]["severity"], "warning")
        self.assertNotIn("outside_edited_component", rows[0])

    def test_appeared_and_disappeared(self):
        rows = sc.diff_snapshots({"gone": self.rec(path="P:1", comp="P")}, {"new": self.rec()}, "Frame", "cm")
        by = {r["type"]: r for r in rows}
        self.assertEqual(by["body_disappeared"]["severity"], "error")
        self.assertEqual(by["body_appeared"]["severity"], "info")

    def test_moved_only_when_volume_unchanged(self):
        moved = self.rec(bbox=((1, 0, 0), (2, 1, 1)))
        rows = sc.diff_snapshots({"k": self.rec()}, {"k": moved}, None, "in")
        self.assertEqual([(r["type"], r["severity"]) for r in rows], [("body_moved", "info")])
        self.assertAlmostEqual(rows[0]["max_bbox_shift"], 1 / IN, places=5)
        rows = sc.diff_snapshots({"k": self.rec()}, {"k": {**moved, "volume": 5.0}}, None, "in")
        self.assertEqual([r["type"] for r in rows], ["volume_changed"])

    def test_volume_from_none(self):
        rows = sc.diff_snapshots({"k": self.rec(volume=None)}, {"k": self.rec(volume=3.0)}, None, "cm")
        self.assertEqual(rows[0]["type"], "volume_changed")
        self.assertIsNone(rows[0]["percent"])

    def test_store_is_capped(self):
        sc._BASELINES.clear()
        tokens = [sc.store_baseline({}, "doc", now=i) for i in range(sc.BASELINE_STORE_LIMIT + 2)]
        self.assertIsNone(sc.get_baseline(tokens[0]))
        self.assertIsNone(sc.get_baseline(tokens[1]))
        self.assertEqual(sc.get_baseline(tokens[-1])["created"], sc.BASELINE_STORE_LIMIT + 1)
        self.assertEqual(len(sc._BASELINES), sc.BASELINE_STORE_LIMIT)


class SummaryTests(unittest.TestCase):
    def test_counts_order_and_cap(self):
        problems = [{"type": "body_moved", "severity": "info", "body": "a"},
                    {"type": "tiny_faces", "severity": "warning", "body": "b"},
                    {"type": "non_solid", "severity": "error", "body": "c"}]
        out = sc.summarize(problems, 2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["summary"]["by_severity"], {"error": 1, "warning": 1, "info": 1})
        self.assertEqual(out["summary"]["bodies_with_problems"], 2)
        self.assertEqual([p["type"] for p in out["problems"]], ["non_solid", "tiny_faces"])
        self.assertTrue(out["problems_truncated"])

    def test_info_only_is_ok(self):
        self.assertTrue(sc.summarize([{"type": "body_moved", "severity": "info", "body": "a"}], 5)["ok"])


# -- End to end with fakes -------------------------------------------------------------


class RunChecksTests(unittest.TestCase):
    def setUp(self):
        sc._BASELINES.clear()
        self.tube = Body("Tube", 10.0)
        self.gusset = Body("Gusset", 2.0, lumps=2, shells=2)
        fin_faces = [Face(1.0, [1, 1, 1, 1]) for _ in range(5)] + [Face(1e-4, [5.0, 5.0, 1e-5, 1e-5])]
        self.loft = Body("Loft", 3.0, faces=fin_faces)
        self.hidden = Body("Old", 1.0, visible=False)
        self.plate = Body("Plate", 20.0)
        frame = Occ("Frame:1", Comp("Frame"), [self.tube, self.gusset, self.loft, self.hidden])
        plate = Occ("Plate:1", Comp("Plate"), [self.plate])
        self.root = Comp("Root", occurrences=[frame, plate])

    def run_checks(self, design, args):
        opts, err = sc.validate_args(args)
        self.assertIsNone(err)
        with mock.patch.object(sc.adsk.core.ObjectCollection, "create", staticmethod(lambda: Coll([]))):
            return sc.run_checks(design, "Doc v1", opts)

    def test_default_run(self):
        report, err = self.run_checks(Design(self.root), {"units": "cm"})
        self.assertIsNone(err)
        self.assertEqual(list(report)[:2], ["ok", "summary"])
        self.assertEqual(report["bodies_checked"], 4)  # hidden body excluded
        by_type = report["summary"]["by_type"]
        self.assertEqual(by_type["multiple_lumps"], 1)
        self.assertEqual(by_type["sliver_faces"], 1)
        self.assertEqual(by_type["short_edges"], 1)
        self.assertNotIn("interference", report)
        self.assertIn("baseline_token", report)
        self.assertEqual(report["volume_change"]["snapshot_bodies"], 5)  # hidden included

    def test_unknown_body_filter(self):
        opts, _ = sc.validate_args({"bodies": [{"occurrence_path": "Frame:1", "body_name": "Nope"}]})
        report, err = sc.run_checks(Design(self.root), "Doc", opts)
        self.assertIsNone(report)
        self.assertIn("Frame:1/Nope", err)

    def test_interference_all_pairs(self):
        design = Design(self.root, interference=[IResult(self.tube, self.plate, 0.5),
                                                 IResult(self.tube, self.gusset, 1e-12)])
        report, err = self.run_checks(design, {"units": "cm", "checks": ["interference"]})
        self.assertIsNone(err)
        self.assertEqual(report["interference"], {"pairs_checked": 6, "method": "analyzeInterference"})
        overlaps = [p for p in report["problems"] if p["type"] == "interference"]
        self.assertEqual([(p["body"], p["other_body"], p["overlap_volume"]) for p in overlaps],
                         [("Frame:1/Tube", "Plate:1/Plate", 0.5)])

    def test_interference_explicit_pair_runs_per_pair(self):
        design = Design(self.root, interference=[IResult(self.tube, self.plate, 0.5),
                                                 IResult(self.tube, self.gusset, 0.7)])
        pair = [{"occurrence_path": "Frame:1", "body_name": "Tube"},
                {"occurrence_path": "Plate:1", "body_name": "Plate"}]
        report, _ = self.run_checks(design, {"units": "cm", "checks": ["interference"],
                                             "interference_pairs": [pair]})
        self.assertEqual(report["interference"]["pairs_checked"], 1)
        self.assertEqual([p["other_body"] for p in report["problems"]], ["Plate:1/Plate"])

    def test_volume_change_round_trip(self):
        design = Design(self.root)
        first, _ = self.run_checks(design, {"checks": ["volume_change"]})
        token = first["baseline_token"]
        self.assertTrue(first["ok"])
        # A cut in Frame that also took material out of the plate.
        self.tube.volume = 9.0
        self.plate.volume = 18.0
        second, err = self.run_checks(design, {"checks": ["volume_change"], "baseline_token": token,
                                               "edited_component": "Frame", "units": "cm"})
        self.assertIsNone(err)
        self.assertNotIn("baseline_token", second)
        self.assertFalse(second["ok"])
        self.assertEqual(second["volume_change"]["outside_edited_component"], 1)
        self.assertEqual(second["problems"][0]["body"], "Plate:1/Plate")
        self.assertEqual(second["problems"][0]["severity"], "error")

    def test_unknown_token(self):
        opts, _ = sc.validate_args({"baseline_token": "missing"})
        report, err = sc.run_checks(Design(self.root), "Doc", opts)
        self.assertIsNone(report)
        self.assertIn("missing", err)

    def test_document_mismatch_noted(self):
        token = sc.store_baseline({}, "Other doc")
        report, _ = self.run_checks(Design(self.root), {"checks": ["volume_change"], "baseline_token": token})
        self.assertTrue(any("Other doc" in n for n in report["notes"]))


if __name__ == "__main__":
    unittest.main()
