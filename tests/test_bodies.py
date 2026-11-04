"""list_bodies / describe_body: schema, validation, units and geometry logic with fakes."""

import json
import math
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import bodies, tool_surface


# -- Fakes ---------------------------------------------------------------------


def P(x, y, z):
    return NS(x=x, y=y, z=z)


class Coll:
    def __init__(self, items=()):
        self._items = list(items)
        self.count = len(self._items)

    def item(self, i):
        return self._items[i]


def bbox(lo, hi):
    return NS(minPoint=P(*lo), maxPoint=P(*hi))


class Evaluator:
    def __init__(self, normal):
        self._normal = normal

    def getNormalAtPoint(self, _p):
        return True, P(*self._normal)


def face(geometry, area, point_on_face=None, normal=None, reversed_=False, vertices=(), temp_id=None, native=None):
    f = NS(geometry=geometry, area=area, isParamReversed=reversed_, tempId=temp_id, nativeObject=native,
           pointOnFace=P(*point_on_face) if point_on_face else None,
           evaluator=Evaluator(normal) if normal else None,
           vertices=Coll(NS(geometry=P(*v)) for v in vertices))
    return f


def cylinder(radius, origin=(0, 0, 0), axis=(0, 0, 1)):
    return NS(surfaceType=1, radius=radius, origin=P(*origin), axis=P(*axis))


def body(name, visible=True, volume=2.54 ** 3, area_cm2=6.4516, solid=True, lo=(0, 0, 0), hi=(2.54, 5.08, 25.4),
         faces=(), **extra):
    b = NS(name=name, isVisible=visible, volume=volume, area=area_cm2, isSolid=solid,
           boundingBox=bbox(lo, hi), faces=Coll(faces), nativeObject=None, **extra)
    return b


def occ(path, comp, bodies_, visible=True, linked=False):
    return NS(fullPathName=path, component=NS(name=comp), isVisible=visible, isReferencedComponent=linked,
              bRepBodies=Coll(bodies_))


def root(root_bodies=(), occurrences=()):
    return NS(name="Design", bRepBodies=Coll(root_bodies), allOccurrences=Coll(occurrences))


def opts(**kw):
    o, err = bodies.validate_list_args(kw)
    assert err is None, err
    return o


def text(result):
    return result["content"][0]["text"]


# -- Schema and validation -----------------------------------------------------


class SchemaTests(unittest.TestCase):
    def _schema(self, name):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == name)["inputSchema"]

    def test_strict_schemas(self):
        for name in ("list_bodies", "describe_body"):
            with self.subTest(tool=name):
                schema = self._schema(name)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(schema["properties"]["units"]["enum"], ["in", "cm", "mm"])

    def test_required_fields(self):
        self.assertNotIn("required", self._schema("list_bodies"))
        self.assertEqual(self._schema("describe_body")["required"], ["occurrence_path", "body_name"])


class ValidationTests(unittest.TestCase):
    def test_list_defaults(self):
        o, err = bodies.validate_list_args({})
        self.assertIsNone(err)
        self.assertEqual(o, {"units": "in", "include_hidden": False, "name_contains": None,
                             "component_contains": None, "max_results": 200})

    def test_list_rejects_bad_args(self):
        cases = [({"units": "ft"}, "units"), ({"include_hidden": "yes"}, "include_hidden"),
                 ({"name_contains": 5}, "name_contains"), ({"max_results": 0}, "max_results"),
                 ({"max_results": True}, "max_results"), ({"max_results": 1001}, "max_results")]
        for args, key in cases:
            with self.subTest(args=args):
                _, err = bodies.validate_list_args(args)
                self.assertIn(key, err)

    def test_describe_rejects_bad_args(self):
        base = {"occurrence_path": "root", "body_name": "Body1"}
        cases = [({}, "occurrence_path"), ({"occurrence_path": "root"}, "body_name"),
                 ({"occurrence_path": "root", "body_name": "  "}, "body_name"),
                 ({**base, "body_index": -1}, "body_index"), ({**base, "max_faces": 0}, "max_faces"),
                 ({**base, "units": "IN"}, "units")]
        for args, key in cases:
            with self.subTest(args=args):
                _, err = bodies.validate_describe_args(args)
                self.assertIn(key, err)

    def test_handlers_validate_before_touching_fusion(self):
        with patch.object(bodies, "_root_component", side_effect=AssertionError("touched Fusion")):
            self.assertTrue(bodies.list_bodies({"units": "ft"})["isError"])
            self.assertTrue(bodies.describe_body({"body_name": "x"})["isError"])
            self.assertTrue(bodies.list_bodies("nope")["isError"])

    def test_non_design_product(self):
        with patch.object(bodies, "_root_component", return_value=None):
            self.assertIn("not a Fusion design", text(bodies.list_bodies({})))


# -- Units ---------------------------------------------------------------------


class UnitTests(unittest.TestCase):
    def test_lengths(self):
        self.assertEqual(bodies.length(2.54, "in"), 1.0)
        self.assertEqual(bodies.length(2.54, "mm"), 25.4)
        self.assertEqual(bodies.length(2.54, "cm"), 2.54)

    def test_area_and_volume_scale_by_square_and_cube(self):
        self.assertAlmostEqual(bodies.area(6.4516, "in"), 1.0)
        self.assertAlmostEqual(bodies.volume(2.54 ** 3, "in"), 1.0)
        self.assertAlmostEqual(bodies.volume(1.0, "mm"), 1000.0)

    def test_direction_normalizes_and_handles_zero(self):
        self.assertEqual(bodies.direction((0, 0, -5)), [0.0, 0.0, -1.0])
        self.assertIsNone(bodies.direction((0, 0, 0)))
        self.assertNotIn("-0.0", json.dumps(bodies.direction((-0.0, 3, 0))))


# -- list_bodies ---------------------------------------------------------------


class ListBodiesTests(unittest.TestCase):
    def setUp(self):
        self.root = root(
            [body("Base")],
            [occ("Frame:1", "Frame", [body("Tube 1.75"), body("Old Tube", visible=False)]),
             occ("Frame:1+Ref:1", "Reference", [body("Ghost")], visible=False, linked=True)])

    def test_hidden_excluded_by_default_with_note(self):
        r = bodies.list_body_rows(self.root, opts())
        self.assertEqual([b["body"] for b in r["bodies"]], ["Base", "Tube 1.75"])
        self.assertEqual((r["total_bodies"], r["hidden_bodies"], r["total_matched"]), (4, 2, 2))
        self.assertIn("2 hidden bodies excluded", r["note"])

    def test_include_hidden_reports_occurrence_visibility(self):
        r = bodies.list_body_rows(self.root, opts(include_hidden=True))
        ghost = r["bodies"][-1]
        self.assertEqual(ghost["occurrence_path"], "Frame:1+Ref:1")
        self.assertTrue(ghost["visible"])
        self.assertFalse(ghost["occurrence_visible"])
        self.assertTrue(ghost["linked"])
        self.assertNotIn("note", r)

    def test_root_body_row(self):
        row = bodies.list_body_rows(self.root, opts())["bodies"][0]
        self.assertEqual(row["occurrence_path"], "root")
        self.assertEqual(row["component"], "Design")
        self.assertIsNone(row["occurrence_visible"])
        self.assertEqual((row["volume_in3"], row["volume_cm3"]), (1.0, round(2.54 ** 3, 5)))
        self.assertEqual(row["area"], 1.0)
        self.assertEqual((row["bbox_min"], row["bbox_max"]), ([0.0, 0.0, 0.0], [1.0, 2.0, 10.0]))
        self.assertEqual(row["body_index"], 0)
        self.assertNotIn("_body", row)

    def test_metric_units(self):
        row = bodies.list_body_rows(self.root, opts(units="mm"))["bodies"][0]
        self.assertEqual(row["bbox_max"], [25.4, 50.8, 254.0])
        self.assertEqual(row["volume_in3"], 1.0)  # volumes always in3 + cm3

    def test_filters_case_insensitive(self):
        r = bodies.list_body_rows(self.root, opts(name_contains="TUBE", include_hidden=True))
        self.assertEqual([b["body"] for b in r["bodies"]], ["Tube 1.75", "Old Tube"])
        r = bodies.list_body_rows(self.root, opts(component_contains="ref", include_hidden=True))
        self.assertEqual([b["body"] for b in r["bodies"]], ["Ghost"])

    def test_truncation(self):
        many = root([body(f"B{i}") for i in range(5)])
        r = bodies.list_body_rows(many, opts(max_results=2))
        self.assertEqual((r["returned"], r["total_matched"], r["truncated"]), (2, 5, True))

    def test_missing_volume_is_null(self):
        r = bodies.list_body_rows(root([body("S", volume=None, solid=False)]), opts())
        self.assertIsNone(r["bodies"][0]["volume_in3"])
        self.assertFalse(r["bodies"][0]["solid"])

    def test_handler_returns_json(self):
        with patch.object(bodies, "_root_component", return_value=self.root):
            result = bodies.list_bodies({"units": "cm"})
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(text(result))["units"]["volume"], "cm^3")


# -- Resolution ----------------------------------------------------------------


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.entries = list(bodies.iter_body_entries(root(
            [body("Body1")], [occ("A:1", "A", [body("Tube"), body("Tube")]), occ("B:1", "B", [body("Tube")])])))

    def test_exact(self):
        e, err = bodies.resolve_body(self.entries, "B:1", "Tube")
        self.assertIsNone(err)
        self.assertEqual(e["occurrence_path"], "B:1")

    def test_ambiguous_lists_candidates(self):
        e, err = bodies.resolve_body(self.entries, "A:1", "Tube")
        self.assertIsNone(e)
        self.assertIn("ambiguous", err["error"])
        self.assertEqual([c["body_index"] for c in err["candidates"]], [0, 1])

    def test_index_disambiguates(self):
        e, err = bodies.resolve_body(self.entries, "A:1", "Tube", body_index=1)
        self.assertIsNone(err)
        self.assertEqual(e["index"], 1)

    def test_missing_lists_near_matches(self):
        e, err = bodies.resolve_body(self.entries, "C:1", "tube")
        self.assertIsNone(e)
        self.assertEqual(len(err["candidates"]), 3)

    def test_describe_handler_refuses_ambiguous(self):
        with patch.object(bodies, "_root_component", return_value=root(
                occurrences=[occ("A:1", "A", [body("Tube"), body("Tube")])])):
            result = bodies.describe_body({"occurrence_path": "A:1", "body_name": "Tube"})
        self.assertTrue(result["isError"])
        self.assertEqual(len(json.loads(text(result))["candidates"]), 2)


# -- Geometry helpers ----------------------------------------------------------


class GeometryTests(unittest.TestCase):
    def test_concavity(self):
        # Point at radius 1 on +x; outward normal +x is a boss, -x is a bore.
        self.assertEqual(bodies.cylinder_concavity((1, 0, 5), (1, 0, 0), (0, 0, 0), (0, 0, 2)), "convex")
        self.assertEqual(bodies.cylinder_concavity((1, 0, 5), (-1, 0, 0), (0, 0, 0), (0, 0, 2)), "concave")
        self.assertIsNone(bodies.cylinder_concavity((1, 0, 0), (0, 0, 1), (0, 0, 0), (0, 0, 1)))
        self.assertIsNone(bodies.cylinder_concavity((1, 0, 0), (1, 0, 0), (0, 0, 0), (0, 0, 0)))

    def test_axial_extent(self):
        self.assertEqual(bodies.axial_extent([(1, 0, -2), (0, 1, 3)], (0, 0, 0), (0, 0, 5)), 5)
        self.assertEqual(bodies.axial_extent([(1, 0, 0)], (0, 0, 0), (0, 0, 1)), 0)
        self.assertEqual(bodies.axial_extent([], (0, 0, 0), (0, 0, 1)), 0)

    def test_sweep(self):
        r, length = 2.0, 10.0
        self.assertEqual(bodies.sweep_degrees(2 * math.pi * r * length, r, length), 360.0)
        self.assertEqual(bodies.sweep_degrees(math.pi / 2 * r * length, r, length), 90.0)
        self.assertEqual(bodies.sweep_degrees(1e6, r, length), 360.0)
        self.assertIsNone(bodies.sweep_degrees(5.0, r, 0.0))
        self.assertIsNone(bodies.sweep_degrees(5.0, 0.0, 1.0))

    def test_hints(self):
        self.assertIn("fillet", bodies.cylinder_hint({"created_by_type": "FilletFeature", "sweep_deg": 360}))
        self.assertIn("partial", bodies.cylinder_hint({"created_by_type": None, "sweep_deg": 90.0}))
        self.assertIsNone(bodies.cylinder_hint({"created_by_type": "ExtrudeFeature", "sweep_deg": 360.0}))
        self.assertIsNone(bodies.cylinder_hint({"sweep_deg": None}))

    def test_sort_and_cap(self):
        groups = {"cylinders": [{"diameter": d, "area": 1} for d in (1, 3, 2)], "planes": [{"area": 1}, {"area": 4}]}
        out, totals, truncated = bodies.sort_and_cap(groups, 2)
        self.assertEqual([r["diameter"] for r in out["cylinders"]], [3, 2])
        self.assertEqual([r["area"] for r in out["planes"]], [4, 1])
        self.assertEqual(totals, {"cylinders": 3, "planes": 2})
        self.assertTrue(truncated)


# -- describe with a fake tube -------------------------------------------------


class DescribeTests(unittest.TestCase):
    """A 2" OD x 1.5" ID x 10" tube along z in a component, placed at x=+100 cm,
    plus a quarter-round fillet cylinder, an end plane and a torus."""

    OFFSET = 100.0

    def _pair(self, native, world_geometry, **world_kw):
        """Proxy face: world geometry, native face reachable via nativeObject."""
        return face(world_geometry, native.area, native=native, reversed_=native.isParamReversed, **world_kw)

    def setUp(self):
        L, ro, ri, rf = 25.4, 2.54, 1.905, 0.5
        ends = [(ro, 0, 0), (ro, 0, L)]
        od = face(cylinder(ro), 2 * math.pi * ro * L, (ro, 0, 1), (1, 0, 0), vertices=ends, temp_id=1)
        bore = face(cylinder(ri), 2 * math.pi * ri * L, (ri, 0, 1), (-1, 0, 0), reversed_=True,
                    vertices=[(ri, 0, 0), (ri, 0, L)], temp_id=2)
        fillet = face(cylinder(rf, origin=(5, 5, 0)), math.pi / 2 * rf * L, (5 + rf, 5, 1), (-1, 0, 0),
                      reversed_=True, vertices=[(5 + rf, 5, 0), (5, 5 + rf, L)], temp_id=3)

        def world(g):
            return NS(**{**vars(g), "origin": P(g.origin.x + self.OFFSET, g.origin.y, g.origin.z)})

        plane = face(NS(surfaceType=0, origin=P(0, 0, 0), normal=P(0, 0, 1)), 7.0,
                     point_on_face=(self.OFFSET + 2, 0, 0), normal=(0, 0, -1))
        torus = face(NS(surfaceType=4, origin=P(self.OFFSET, 0, 0), axis=P(0, 0, 1), majorRadius=5.08,
                        minorRadius=0.5), 3.0)
        nurbs = face(NS(surfaceType=7), 1.0)
        features = Coll([NS(name="Extrude1", objectType="adsk::fusion::ExtrudeFeature", faces=Coll([od, bore])),
                         NS(name="Fillet2", objectType="adsk::fusion::FilletFeature", faces=Coll([fillet]))])
        native_body = NS(parentComponent=NS(features=features))
        for f in (od, bore, fillet):
            f.body = native_body
        props = NS(centerOfMass=P(self.OFFSET, 0, L / 2),
                   getPrincipalAxes=lambda: (True, P(1, 0, 0), P(0, 1, 0), P(0, 0, 1)))
        faces = [self._pair(od, world(od.geometry)), self._pair(bore, world(bore.geometry)),
                 self._pair(fillet, world(fillet.geometry)), plane, torus, nurbs]
        b = body("Tube", faces=faces, edges=Coll([1] * 12), shells=Coll([1]), lumps=Coll([1]),
                 physicalProperties=props)
        b.nativeObject = native_body
        self.entry = next(bodies.iter_body_entries(root(occurrences=[occ("Frame:1", "Frame", [b])])))
        self.payload = bodies.describe(self.entry, "in", 25)

    def test_counts(self):
        counts = self.payload["counts"]
        self.assertEqual(counts["faces_by_type"], {"cylinder": 3, "plane": 1, "torus": 1, "nurbs": 1})
        self.assertEqual((counts["faces"], counts["edges"], counts["shells"], counts["lumps"]), (6, 12, 1, 1))

    def test_cylinders_sorted_with_world_axis(self):
        cyl = self.payload["cylinders"]
        self.assertEqual([c["diameter"] for c in cyl], [2.0, 1.5, round(1.0 / 2.54, 5)])
        self.assertAlmostEqual(cyl[0]["axis_origin"][0], self.OFFSET / 2.54, places=4)
        self.assertEqual(cyl[0]["axis_direction"], [0.0, 0.0, 1.0])
        self.assertEqual(cyl[0]["length_along_axis"], 10.0)

    def test_bore_boss_and_fillet_distinguished(self):
        od, bore, fillet = self.payload["cylinders"]
        self.assertEqual((od["concavity"], od["sweep_deg"], od["created_by"]), ("convex", 360.0, "Extrude1"))
        self.assertEqual((bore["concavity"], bore["concavity_basis"]), ("concave", "evaluator"))
        self.assertNotIn("hint", od)
        self.assertEqual((fillet["concavity"], fillet["sweep_deg"]), ("concave", 90.0))
        self.assertEqual(fillet["created_by_type"], "FilletFeature")
        self.assertIn("fillet", fillet["hint"])

    def test_other_faces(self):
        plane = self.payload["planes"][0]
        self.assertEqual(plane["normal"], [0.0, 0.0, -1.0])
        self.assertAlmostEqual(plane["point"][0], (self.OFFSET + 2) / 2.54, places=4)
        torus = self.payload["tori"][0]
        self.assertEqual((torus["major_radius"], torus["created_by"]), (2.0, None))
        self.assertNotIn("_temp_id", json.dumps(self.payload))

    def test_physical_and_body(self):
        self.assertEqual(self.payload["physical"]["principal_axes"]["z"], [0.0, 0.0, 1.0])
        self.assertAlmostEqual(self.payload["physical"]["center_of_mass"][2], 5.0)
        self.assertEqual(self.payload["body"]["occurrence_path"], "Frame:1")
        self.assertTrue(self.payload["is_solid"])
        self.assertIn("Fillet faces", self.payload["notes"][0])

    def test_face_cap(self):
        payload = bodies.describe(self.entry, "in", 1)
        self.assertEqual(len(payload["cylinders"]), 1)
        self.assertEqual(payload["face_totals"]["cylinders"], 3)
        self.assertTrue(payload["faces_truncated"])

    def test_concavity_falls_back_to_param_reversed(self):
        native = face(cylinder(1.0), 10.0, temp_id=9, reversed_=True)
        rec = bodies._face_record(face(cylinder(1.0), 10.0, native=native, reversed_=True), "cylinder", "cm")
        self.assertEqual((rec["concavity"], rec["concavity_basis"]), ("concave", "isParamReversed"))
        self.assertIsNone(rec["length_along_axis"])

    def test_feature_map_tolerates_bad_features(self):
        broken = NS(name="Broken")  # no faces attribute
        found, note = bodies.feature_map(NS(features=Coll([broken])), {1}, None)
        self.assertEqual((found, note), ({}, None))
        found, note = bodies.feature_map(NS(), {1}, None)
        self.assertIn("unavailable", note)


if __name__ == "__main__":
    unittest.main()
