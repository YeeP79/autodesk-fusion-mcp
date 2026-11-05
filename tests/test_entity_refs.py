"""EntityRef parsing, resolution, description and the pure filters, on fakes."""

import ast
import json
import math
import os
import types
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401
import adsk.core
import adsk.fusion
from fusion_bridge.common import bodies
from fusion_bridge.common import entity_refs as er


class P:
    """A Fusion point or vector."""

    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


def pt(p):
    return P(*p)


class Evaluator:
    def __init__(self, normal):
        self.normal = normal

    def getNormalAtPoint(self, point):
        return True, pt(self.normal)


class Body:
    def __init__(self, name="Plate", context=None):
        self.name = name
        self.assemblyContext = context


class Occurrence:
    fullPathName = "Sub:1"


class Entity:
    """A face or edge a token or point search can return."""

    def __init__(self, kind="BRepFace", body=None, context=None, at=None, proxy=None):
        self.objectType = f"adsk::fusion::{kind}"
        self.body = body
        self.assemblyContext = context
        self.at = at
        self.proxy = proxy

    def createForAssemblyContext(self, occurrence):
        return self.proxy


class Scene:
    """``FusionScene`` with its answers scripted. ``at`` on an entity is where it lies."""

    def __init__(self, tokens=(), hits=(), entries=()):
        self.tokens = list(tokens)
        self.hits = list(hits)
        self._entries = bodies.assign_keys([dict(e) for e in entries])
        self.searches = []

    def entries(self):
        return self._entries

    def by_token(self, token):
        return self.tokens

    def at_point(self, point, kind, tolerance_cm=er.REF_TOLERANCE_CM):
        self.searches.append((point, kind))
        return self.hits

    def distance(self, entity, point):
        return None if entity.at is None else math.dist(entity.at, point)


def entry(body, path="root", name="Plate", index=0):
    return {"occurrence_path": path, "body": name, "index": index, "visible": True, "solid": True,
            "bbox": None, "component": "c", "linked": False, "_body": body}


def ref(token="t", kind="face", point=(1.0, 1.0, 1.0), body=None):
    return {"token": token, "kind": kind, "point": point, "body": body}


class ParseRefTests(unittest.TestCase):
    def test_point_comes_back_in_cm(self):
        parsed = er.parse_ref({"token": "t", "kind": "face", "point": [1, 0, 0]}, "in")
        self.assertEqual(parsed["point"], (2.54, 0.0, 0.0))
        self.assertEqual(parsed["token"], "t")
        self.assertIsNone(parsed["body"])

    def test_body_is_parsed(self):
        parsed = er.parse_ref({"token": "t", "body": {"occurrence_path": "root", "body_name": "B", "body_index": 2}}, "cm")
        self.assertEqual(parsed["body"], ("root", "B", 2))

    def test_refusals(self):
        for value in ("x", None, {}, {"kind": "face"}, {"point": [0, 0, 0]}, {"token": ""}, {"token": 3},
                      {"token": "t", "kind": "loop"}, {"token": "t", "extra": 1}, {"token": "t", "point": [0, 0]},
                      {"token": "t", "point": [0, 0, float("nan")]}, {"token": "t", "body": {"body_name": "B"}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                er.parse_ref(value, "in")

    def test_message_names_the_argument(self):
        with self.assertRaisesRegex(ValueError, "'of_face'"):
            er.parse_ref({}, "in", "of_face")


class ResolveTests(unittest.TestCase):
    def test_single_result_on_the_point(self):
        face = Entity(at=(1.0, 1.0, 1.0))
        got = er.resolve(Scene(tokens=[face]), ref())
        self.assertIs(got.entity, face)
        self.assertEqual((got.resolved_by, got.kind, got.warnings, got.refusal), ("token", "face", [], None))

    def test_tolerance_decides_token_or_point(self):
        inside = Entity(at=(1.0, 1.0, 1.0 + er.REF_TOLERANCE_CM * 0.99))
        self.assertEqual(er.resolve(Scene(tokens=[inside]), ref()).resolved_by, "token")
        outside = Entity(at=(1.0, 1.0, 1.0 + er.REF_TOLERANCE_CM * 1.01))
        self.assertEqual(er.resolve(Scene(tokens=[outside], hits=[Entity()]), ref()).resolved_by, "point")

    def test_split_face_is_chosen_by_point_not_order(self):
        far, near = Entity(at=(9.0, 9.0, 9.0)), Entity(at=(1.0, 1.0, 1.0))
        self.assertIs(er.resolve(Scene(tokens=[far, near]), ref()).entity, near)
        self.assertIs(er.resolve(Scene(tokens=[near, far]), ref()).entity, near)

    def test_split_with_the_point_on_none_is_refused(self):
        got = er.resolve(Scene(tokens=[Entity(at=(9.0, 0, 0)), Entity(at=(8.0, 0, 0))]), ref())
        self.assertEqual(got.refusal["code"], "ambiguous_split")
        self.assertIsNone(got.entity)

    def test_split_with_the_point_on_two_is_refused(self):
        tokens = [Entity(at=(1.0, 1.0, 1.0)), Entity(at=(1.0, 1.0, 1.0))]
        self.assertEqual(er.resolve(Scene(tokens=tokens), ref()).refusal["code"], "ambiguous_split")

    def test_split_with_no_point_is_refused_not_guessed(self):
        got = er.resolve(Scene(tokens=[Entity(), Entity()]), ref(point=None))
        self.assertEqual(got.refusal["code"], "ambiguous_split")

    def test_the_same_entity_twice_is_not_a_split(self):
        face = Entity(at=(1.0, 1.0, 1.0))
        self.assertIs(er.resolve(Scene(tokens=[face, face]), ref()).entity, face)

    def test_token_only(self):
        face = Entity()
        got = er.resolve(Scene(tokens=[face]), ref(point=None))
        self.assertIs(got.entity, face)
        self.assertEqual(got.resolved_by, "token")

    def test_dead_token_with_no_point(self):
        self.assertEqual(er.resolve(Scene(), ref(point=None)).refusal["code"], "not_found")

    def test_token_moved_falls_back_to_the_point(self):
        moved, there = Entity(at=(5.0, 5.0, 5.0)), Entity()
        scene = Scene(tokens=[moved], hits=[there])
        got = er.resolve(scene, ref())
        self.assertIs(got.entity, there)
        self.assertEqual(got.resolved_by, "point")
        self.assertEqual(scene.searches, [((1.0, 1.0, 1.0), "face")])

    def test_dead_token_falls_back_to_the_point(self):
        there = Entity()
        self.assertIs(er.resolve(Scene(hits=[there]), ref()).entity, there)

    def test_point_only(self):
        there = Entity(kind="BRepEdge")
        got = er.resolve(Scene(hits=[there]), ref(token=None, kind="edge"))
        self.assertIs(got.entity, there)
        self.assertEqual(got.resolved_by, "point")

    def test_two_hits_at_the_point_are_refused(self):
        got = er.resolve(Scene(hits=[Entity(), Entity()]), ref(token=None))
        self.assertEqual(got.refusal["code"], "ambiguous_point")

    def test_nothing_at_the_point_accepts_the_token_with_a_warning(self):
        trimmed = Entity(at=(5.0, 5.0, 5.0))
        got = er.resolve(Scene(tokens=[trimmed]), ref())
        self.assertIs(got.entity, trimmed)
        self.assertEqual(got.resolved_by, "token")
        self.assertEqual(len(got.warnings), 1)

    def test_unmeasurable_token_entity_falls_through_the_same_way(self):
        got = er.resolve(Scene(tokens=[Entity(at=None)]), ref())
        self.assertEqual(len(got.warnings), 1)

    def test_nothing_anywhere(self):
        self.assertEqual(er.resolve(Scene(), ref()).refusal["code"], "not_found")

    def test_token_of_the_wrong_kind_is_not_that_entity(self):
        edge = Entity(kind="BRepEdge", at=(1.0, 1.0, 1.0))
        self.assertEqual(er.resolve(Scene(tokens=[edge]), ref(kind="face")).refusal["code"], "not_found")

    def test_kind_comes_from_the_token_when_the_ref_has_none(self):
        moved, there = Entity(kind="BRepEdge", at=(5.0, 5.0, 5.0)), Entity(kind="BRepEdge")
        scene = Scene(tokens=[moved], hits=[there])
        self.assertEqual(er.resolve(scene, ref(kind=None)).kind, "edge")
        self.assertEqual(scene.searches[0][1], "edge")

    def test_a_non_brep_token_passes_with_no_kind(self):
        plane = Entity(kind="ConstructionPlane")
        got = er.resolve(Scene(tokens=[plane]), ref(kind=None, point=None))
        self.assertIs(got.entity, plane)
        self.assertIsNone(got.kind)


class ResolveBodyTests(unittest.TestCase):
    def setUp(self):
        self.plate, self.other = Body("Plate"), Body("Other")
        self.entries = [entry(self.plate), entry(self.other, name="Other")]
        self.body = ("root", "Plate", None)

    def test_hits_are_filtered_to_the_body(self):
        mine, theirs = Entity(body=self.plate), Entity(body=self.other)
        got = er.resolve(Scene(hits=[theirs, mine], entries=self.entries), ref(token=None, body=self.body))
        self.assertIs(got.entity, mine)

    def test_token_on_another_body_is_not_accepted(self):
        theirs = Entity(body=self.other, at=(1.0, 1.0, 1.0))
        got = er.resolve(Scene(tokens=[theirs], entries=self.entries), ref(body=self.body))
        self.assertEqual(got.refusal["code"], "not_found")

    def test_token_on_another_body_falls_back_to_the_point(self):
        theirs, mine = Entity(body=self.other, at=(1.0, 1.0, 1.0)), Entity(body=self.plate)
        got = er.resolve(Scene(tokens=[theirs], hits=[mine], entries=self.entries), ref(body=self.body))
        self.assertIs(got.entity, mine)
        self.assertEqual(got.resolved_by, "point")

    def test_trimmed_token_on_the_wrong_body_gets_no_warning_pass(self):
        theirs = Entity(body=self.other, at=(5.0, 5.0, 5.0))
        got = er.resolve(Scene(tokens=[theirs], entries=self.entries), ref(body=self.body))
        self.assertEqual(got.refusal["code"], "not_found")

    def test_missing_body(self):
        got = er.resolve(Scene(entries=self.entries), ref(body=("root", "Gone", None)))
        self.assertEqual(got.refusal["code"], "body_not_found")

    def test_repeated_body_name_needs_an_index(self):
        entries = [entry(Body(), index=0), entry(Body(), index=1)]
        got = er.resolve(Scene(entries=entries), ref(body=("root", "Plate", None)))
        self.assertEqual(got.refusal["code"], "ambiguous_body")
        self.assertEqual(len(got.refusal["candidates"]), 2)

    def test_repeated_name_never_matches_by_name(self):
        first, second = Body(), Body()
        entries = [entry(first, index=0), entry(second, index=1)]
        hit = Entity(body=first)
        got = er.resolve(Scene(hits=[hit], entries=entries), ref(token=None, body=("root", "Plate", 1)))
        self.assertEqual(got.refusal["code"], "not_found")

    def test_equal_path_and_name_stands_in_for_identity(self):
        lookalike = Body("Plate")
        got = er.resolve(Scene(hits=[Entity(body=lookalike)], entries=self.entries), ref(token=None, body=self.body))
        self.assertIsNotNone(got.entity)


class ResolveContextTests(unittest.TestCase):
    def setUp(self):
        self.occ = Occurrence()
        self.sub = Body("SubBlock", self.occ)
        self.entries = [entry(self.sub, path="Sub:1", name="SubBlock")]
        self.body = ("Sub:1", "SubBlock", None)

    def test_native_token_result_is_mapped_before_it_is_measured(self):
        proxy = Entity(body=self.sub, context=self.occ, at=(1.0, 1.0, 1.0))
        native = Entity(body=Body("SubBlock"), at=(50.0, 50.0, 50.0), proxy=proxy)
        got = er.resolve(Scene(tokens=[native], entries=self.entries), ref(body=self.body))
        self.assertIs(got.entity, proxy)
        self.assertEqual(got.resolved_by, "token")

    def test_native_point_hit_is_mapped(self):
        proxy = Entity(body=self.sub, context=self.occ)
        native = Entity(body=Body("SubBlock"), proxy=proxy)
        got = er.resolve(Scene(hits=[native], entries=self.entries), ref(token=None, body=self.body))
        self.assertIs(got.entity, proxy)

    def test_proxy_is_left_alone(self):
        proxy = Entity(body=self.sub, context=self.occ, at=(1.0, 1.0, 1.0), proxy="never")
        self.assertIs(er.resolve(Scene(tokens=[proxy], entries=self.entries), ref(body=self.body)).entity, proxy)

    def test_split_results_in_the_occurrence_keep_it(self):
        a = Entity(body=self.sub, context=self.occ, at=(9.0, 0.0, 0.0))
        b = Entity(body=self.sub, context=self.occ, at=(1.0, 1.0, 1.0))
        got = er.resolve(Scene(tokens=[a, b], entries=self.entries), ref(body=self.body))
        self.assertIs(got.entity, b)
        self.assertIs(got.entity.assemblyContext, self.occ)

    def test_refused_mapping_is_not_taken_for_the_body(self):
        native = Entity(body=Body("Elsewhere"), at=(1.0, 1.0, 1.0), proxy=None)
        got = er.resolve(Scene(tokens=[native], entries=self.entries), ref(body=self.body))
        self.assertEqual(got.refusal["code"], "not_found")


class RowTests(unittest.TestCase):
    def test_resolved_row(self):
        got = er.resolve(Scene(hits=[Entity()]), ref(token=None))
        self.assertEqual(er.resolved_row(got), {"kind": "face", "resolved_by": "point"})
        self.assertEqual(er.resolved_row(got, "of_face")["role"], "of_face")

    def test_body_ref_gives_an_index_only_for_repeated_names(self):
        keyed = bodies.assign_keys([entry(Body()), entry(Body(), name="Twin", index=1), entry(Body(), name="Twin", index=2)])
        self.assertEqual(er.body_ref(keyed[0]), {"occurrence_path": "root", "body_name": "Plate"})
        self.assertEqual(er.body_ref(keyed[2])["body_index"], 2)


class Surface:
    def __init__(self, surface_type, **fields):
        self.surfaceType = surface_type
        self.__dict__.update(fields)


class Box:
    def __init__(self, low, high):
        self.minPoint, self.maxPoint = pt(low), pt(high)


class Vertex:
    def __init__(self, p):
        self.geometry = pt(p)


class Face:
    objectType = "adsk::fusion::BRepFace"
    tempId = 7
    entityToken = "tok"
    isParamReversed = False
    nativeObject = None

    def __init__(self, geometry, area=4.0, point=(0.0, 0.0, 1.0), normal=(0.0, 0.0, 1.0),
                 box=((0, 0, 0), (2, 2, 1)), vertices=(), edges=()):
        self.geometry = geometry
        self.area = area
        self.pointOnFace = pt(point)
        self.evaluator = Evaluator(normal)
        self.boundingBox = Box(*box)
        self.vertices = [Vertex(p) for p in vertices]
        self.edges = list(edges)


def plane(normal=(0.0, 0.0, 1.0)):
    return Surface(0, normal=pt(normal), origin=pt((0, 0, 0)))


def cylinder(radius=0.5, origin=(1.0, 1.0, 0.0), axis=(0.0, 0.0, 1.0)):
    return Surface(1, radius=radius, origin=pt(origin), axis=pt(axis))


class EdgeOf:
    def __init__(self, *faces):
        self.faces = list(faces)


class DescribeFaceTests(unittest.TestCase):
    def test_reversed_plane_reports_the_outward_normal(self):
        # The surface says +z; the Build 223 case is a bottom face whose evaluator says -z.
        face = Face(plane((0.0, 0.0, 1.0)), normal=(0.0, 0.0, -1.0), point=(1.0, 1.0, 0.0))
        rec = er.describe_face(face, "cm")
        self.assertEqual(rec["normal"], [0.0, 0.0, -1.0])
        self.assertEqual(rec["type"], "plane")

    def test_ref_point_is_point_on_face(self):
        face = Face(plane(), point=(2.54, 0.0, 0.0))
        rec = er.describe_face(face, "in", {"occurrence_path": "root", "body": "B", "index": 0, "key": "root/B"})
        self.assertEqual(rec["ref"], {"kind": "face", "token": "tok", "point": [1.0, 0.0, 0.0],
                                      "body": {"occurrence_path": "root", "body_name": "B"}})

    def test_a_described_ref_parses_back(self):
        keyed = bodies.assign_keys([entry(Body())])[0]
        rec = er.describe_face(Face(plane(), point=(2.54, 0.0, 0.0)), "in", keyed)
        parsed = er.parse_ref(json.loads(json.dumps(rec["ref"])), "in")
        self.assertEqual(parsed, {"token": "tok", "kind": "face", "point": (2.54, 0.0, 0.0),
                                  "body": ("root", "Plate", None)})

    def test_cylinder_fields(self):
        face = Face(cylinder(), area=math.pi * 2.0, normal=(1.0, 0.0, 0.0), point=(1.5, 1.0, 0.5),
                    vertices=[(1.5, 1.0, 0.0), (1.5, 1.0, 2.0)], box=((0.5, 0.5, 0), (1.5, 1.5, 2)))
        rec = er.describe_face(face, "cm")
        self.assertEqual(rec["diameter"], 1.0)
        self.assertEqual(rec["axis_origin"], [1.0, 1.0, 0.0])
        self.assertEqual(rec["axis_direction"], [0.0, 0.0, 1.0])
        self.assertEqual(rec["concavity"], "convex")
        self.assertEqual(rec["sweep_deg"], 360.0)
        self.assertEqual(rec["length_along_axis"], 2.0)
        self.assertEqual(rec["split_siblings"], 0)
        self.assertIsNone(rec["created_by"])
        self.assertEqual(rec["label"], "cylinder d=1.000 axis=+z len=2.000 at [1,1,1]")

    def test_only_relevant_fields_appear(self):
        rec = er.public(er.describe_face(Face(plane()), "cm"))
        self.assertIn("normal", rec)
        for key in ("diameter", "concavity", "sweep_deg", "split_siblings", "_entity"):
            self.assertNotIn(key, rec)

    def test_nurbs_has_a_label(self):
        rec = er.describe_face(Face(Surface(7)), "cm")
        self.assertEqual(rec["label"], "nurbs area=4.000 at [1,1,0.5]")

    def test_label_is_stable_under_noise(self):
        def label(noise):
            face = Face(cylinder(0.5 + noise, (1.0 + noise, 1.0 - noise, noise), (noise, -noise, 1.0)),
                        area=6.28 + noise, vertices=[(1.5, 1.0, noise), (1.5, 1.0, 2.0 + noise)],
                        box=((noise, 0, 0), (2, 2 - noise, 1)))
            return er.describe_face(face, "cm")["label"]

        self.assertEqual(label(0.0), label(1e-9))
        self.assertEqual(label(0.0), label(-1e-9))

    def test_label_has_no_negative_zero(self):
        rec = er.describe_face(Face(plane(), box=((-1e-9, -1e-9, -1e-9), (1e-9, 1e-9, 1e-9))), "cm")
        self.assertNotIn("-0", rec["label"])

    def test_an_axis_reads_the_same_in_either_sense(self):
        self.assertEqual(er.direction_label((0.0, 0.0, -1.0), unsigned=True), "+z")
        self.assertEqual(er.direction_label((0.0, 0.0, -1.0)), "-z")
        self.assertEqual(er.direction_label((-0.7071, 0.7071, 0.0), unsigned=True), "[0.707,-0.707,0]")


class SplitSiblingTests(unittest.TestCase):
    def link(self, face, *others):
        face.edges = [EdgeOf(face, other) for other in others]

    def test_two_halves_of_a_bore(self):
        a, b = Face(cylinder()), Face(cylinder(axis=(0.0, 0.0, -1.0), origin=(1.0, 1.0, 5.0)))
        self.link(a, b, b)
        self.assertEqual(er.split_siblings(a), 1)

    def test_different_radius_or_axis_is_not_a_sibling(self):
        a = Face(cylinder())
        self.link(a, Face(cylinder(0.6)), Face(cylinder(origin=(3.0, 1.0, 0.0))), Face(cylinder(axis=(1.0, 0.0, 0.0))))
        self.assertEqual(er.split_siblings(a), 0)

    def test_two_separate_bores_do_not_touch(self):
        self.assertEqual(er.split_siblings(Face(cylinder())), 0)

    def test_seam_is_not_a_sibling(self):
        a = Face(cylinder())
        self.link(a, a)
        self.assertEqual(er.split_siblings(a), 0)

    def test_same_cylinder_tolerance(self):
        a = ((0, 0, 0), (0, 0, 1), 1.0)
        self.assertTrue(er.same_cylinder(a, ((0, 0, 9), (0, 0, -2), 1.0)))
        self.assertFalse(er.same_cylinder(a, ((0.01, 0, 0), (0, 0, 1), 1.0)))
        self.assertFalse(er.same_cylinder(a, ((0, 0, 0), (0, 0, 0), 1.0)))


class OwnerEntryTests(unittest.TestCase):
    def setUp(self):
        self.native = object()
        self.one = types.SimpleNamespace(nativeObject=self.native)
        self.two = types.SimpleNamespace(nativeObject=self.native)
        self.entries = [{"occurrence_path": "Sub:1", "_body": self.one}, {"occurrence_path": "Sub:2", "_body": self.two}]

    def test_a_proxy_takes_its_own_occurrence(self):
        face = types.SimpleNamespace(body=self.two, assemblyContext=types.SimpleNamespace(fullPathName="Sub:2"))
        self.assertIs(er.owner_entry(face, self.entries), self.entries[1])

    def test_a_native_entity_takes_the_first_instance(self):
        face = types.SimpleNamespace(body=self.native, assemblyContext=None)
        self.assertIs(er.owner_entry(face, self.entries), self.entries[0])

    def test_a_body_not_in_the_design(self):
        face = types.SimpleNamespace(body=object(), assemblyContext=None)
        self.assertIsNone(er.owner_entry(face, self.entries))


class Curve:
    def __init__(self, curve_type, **fields):
        self.curveType = curve_type
        self.__dict__.update(fields)


class Edge:
    objectType = "adsk::fusion::BRepEdge"
    tempId = 3
    entityToken = "etok"
    nativeObject = None

    def __init__(self, geometry, length=1.0, start=(0, 0, 0), end=(1, 0, 0), point=(0.5, 0.0, 0.0),
                 faces=(), box=((0, 0, 0), (1, 0, 0)), evaluator=None):
        self.geometry = geometry
        self.length = length
        self.startVertex = Vertex(start) if start else None
        self.endVertex = Vertex(end) if end else None
        self.pointOnEdge = pt(point)
        self.faces = list(faces)
        self.boundingBox = Box(*box)
        self.evaluator = evaluator


class LineEvaluator:
    def getParameterExtents(self):
        return True, 0.0, 1.0

    def getPointAtParameter(self, t):
        return True, pt((t, 0.0, 0.0))


class DescribeEdgeTests(unittest.TestCase):
    def circle(self, **kw):
        g = Curve(2, center=pt((1, 1, 1)), normal=pt((0, 0, 1)), radius=0.5)
        return Edge(g, length=math.pi, start=(1.5, 1, 1), end=(1.5, 1, 1), point=(0.5, 1.0, 1.0),
                    box=((0.5, 0.5, 1), (1.5, 1.5, 1)), **kw)

    def test_circle(self):
        rec = er.describe_edge(self.circle(), "cm")
        self.assertEqual(rec["type"], "circle")
        self.assertTrue(rec["closed"])
        self.assertEqual((rec["center"], rec["radius"], rec["diameter"]), ([1, 1, 1], 0.5, 1.0))
        self.assertEqual(rec["normal"], [0.0, 0.0, 1.0])
        self.assertEqual(rec["start"], rec["end"])
        self.assertEqual(rec["sweep_deg"], 360.0)
        self.assertEqual(rec["label"], "circle d=1.000 at [1,1,1]")

    def test_arc_sweep_comes_from_length(self):
        g = Curve(1, center=pt((0, 0, 0)), normal=pt((0, 0, 1)), radius=2.0)
        rec = er.describe_edge(Edge(g, length=math.pi, start=(2, 0, 0), end=(-2, 0, 0)), "cm")
        self.assertEqual(rec["sweep_deg"], 90.0)
        self.assertFalse(rec["closed"])

    def test_line_direction(self):
        rec = er.describe_edge(Edge(Curve(0), start=(0, 0, 0), end=(0, 0, 2)), "cm")
        self.assertEqual(rec["direction"], [0.0, 0.0, 1.0])
        self.assertNotIn("center", rec)

    def test_closed_spline(self):
        rec = er.describe_edge(Edge(Curve(6), start=(1, 0, 0), end=(1, 0, 0)), "cm")
        self.assertEqual(rec["type"], "spline")
        self.assertTrue(rec["closed"])

    def test_elliptical_arc_is_an_ellipse(self):
        g = Curve(4, center=pt((0, 0, 0)), normal=pt((0, 0, 1)))
        self.assertEqual(er.describe_edge(Edge(g), "cm")["type"], "ellipse")

    def test_no_vertices(self):
        rec = er.describe_edge(Edge(Curve(0), start=None, end=None), "cm")
        self.assertNotIn("start", rec)
        self.assertFalse(rec["closed"])

    def test_adjacent_faces_are_refs_and_a_seam_counts_once(self):
        face = Face(plane())
        rec = er.describe_edge(Edge(Curve(0), faces=[face, face]), "cm")
        self.assertEqual(len(rec["faces"]), 1)
        self.assertEqual(rec["faces"][0]["kind"], "face")
        self.assertNotIn("faces", er.describe_edge(Edge(Curve(0)), "cm", adjacent=False))

    def test_samples_walk_the_evaluator(self):
        rec = er.describe_edge(Edge(Curve(0), evaluator=LineEvaluator()), "cm")
        self.assertEqual(len(rec["_samples"]), er._EDGE_SAMPLES + 1)
        self.assertEqual(rec["_samples"][-2], [1.0, 0.0, 0.0])

    def test_samples_fall_back_to_the_ends(self):
        rec = er.describe_edge(Edge(Curve(0), evaluator=None), "cm")
        self.assertEqual(len(rec["_samples"]), 3)
        self.assertNotIn("_samples", er.describe_edge(Edge(Curve(0)), "cm", samples=False))


def face_rec(**fields):
    return {"type": "cylinder", "bbox": [[0, 0, 0], [2, 2, 1]], "_samples": [[0, 0, 1]], **fields}


def direction(vector, tolerance=1.0, signed=True, **extra):
    return {"direction": vector, "tolerance_deg": tolerance, "signed": signed, **extra}


class FilterTests(unittest.TestCase):
    def test_in_range_is_inclusive(self):
        self.assertTrue(er.in_range(1.0, (1.0, 1.0)))
        self.assertTrue(er.in_range(5, (None, 5)))
        self.assertTrue(er.in_range(5, (5, None)))
        self.assertFalse(er.in_range(5.0001, (None, 5)))
        self.assertFalse(er.in_range(4.9999, (5, None)))

    def test_nan_and_missing_never_match(self):
        for value in (float("nan"), float("inf"), None, "1"):
            self.assertFalse(er.in_range(value, (None, 1e300)))
            self.assertFalse(er.in_range(value, (-1e300, None)))

    def test_surface_types(self):
        self.assertTrue(er.matches_face(face_rec(), {"surface_types": {"cylinder"}}))
        self.assertFalse(er.matches_face(face_rec(type="plane"), {"surface_types": {"cylinder"}}))

    def test_signed_normal_on_a_reversed_plane(self):
        top, bottom = face_rec(normal=[0, 0, 1]), face_rec(normal=[0, 0, -1])
        wanted = {"normal": direction((0.0, 0.0, 1.0))}
        self.assertTrue(er.matches_face(top, wanted))
        self.assertFalse(er.matches_face(bottom, wanted))

    def test_unsigned_normal_takes_both_sides(self):
        wanted = {"normal": direction((0.0, 0.0, 1.0), signed=False)}
        self.assertTrue(er.matches_face(face_rec(normal=[0, 0, -1]), wanted))

    def test_normal_tolerance(self):
        tilted = [math.sin(math.radians(1.0)), 0.0, math.cos(math.radians(1.0))]
        self.assertTrue(er.matches_face(face_rec(normal=tilted), {"normal": direction((0.0, 0.0, 1.0), 1.001)}))
        self.assertFalse(er.matches_face(face_rec(normal=tilted), {"normal": direction((0.0, 0.0, 1.0), 0.999)}))

    def test_a_face_with_no_normal_never_matches_one(self):
        self.assertFalse(er.matches_face(face_rec(), {"normal": direction((0.0, 0.0, 1.0))}))

    def test_axis_is_unsigned_by_choice(self):
        rec = face_rec(axis_direction=[0, 0, -1], axis_origin=[1, 1, 0])
        self.assertTrue(er.matches_face(rec, {"axis": direction((0.0, 0.0, 1.0), signed=False)}))
        self.assertFalse(er.matches_face(rec, {"axis": direction((0.0, 0.0, 1.0), signed=True)}))

    def test_axis_through_a_point(self):
        rec = face_rec(axis_direction=[0, 0, 1], axis_origin=[1, 1, 0])
        wanted = direction((0.0, 0.0, 1.0), signed=False, through_tolerance=0.001)
        self.assertTrue(er.matches_face(rec, {"axis": {**wanted, "through": (1.0, 1.0, 9.0)}}))
        self.assertTrue(er.matches_face(rec, {"axis": {**wanted, "through": (1.0005, 1.0, 0.0)}}))
        self.assertFalse(er.matches_face(rec, {"axis": {**wanted, "through": (1.1, 1.0, 0.0)}}))

    def test_ranges(self):
        rec = face_rec(diameter=1.0, area=6.0, length_along_axis=2.0, sweep_deg=360.0)
        self.assertTrue(er.matches_face(rec, {"diameter": (0.9, 1.1), "area": (6.0, None),
                                              "length": (None, 2.0), "sweep_deg": (300, None)}))
        for wanted in ({"diameter": (1.1, 2)}, {"area": (7, None)}, {"length": (None, 1)}, {"sweep_deg": (None, 90)}):
            self.assertFalse(er.matches_face(rec, wanted))

    def test_a_fillet_is_not_a_bore(self):
        fillet = face_rec(diameter=1.0, concavity="concave", sweep_deg=90.0)
        bore = face_rec(diameter=1.0, concavity="concave", sweep_deg=360.0)
        wanted = {"surface_types": {"cylinder"}, "concavity": "concave", "sweep_deg": (300, None)}
        self.assertFalse(er.matches_face(fillet, wanted))
        self.assertTrue(er.matches_face(bore, wanted))

    def test_filter_on_a_value_the_type_lacks_fails(self):
        for wanted in ({"diameter": (0, None)}, {"length": (0, None)}, {"sweep_deg": (0, None)}, {"concavity": "convex"}):
            self.assertFalse(er.matches_face(face_rec(type="plane"), wanted))

    def test_in_box(self):
        box = {"in_box": ((0, 0, 0), (2, 2, 1))}
        self.assertTrue(er.matches_face(face_rec(), box))
        self.assertFalse(er.matches_face(face_rec(bbox=[[0, 0, 0], [2, 2, 1.1]]), box))
        self.assertFalse(er.matches_face(face_rec(bbox=None), box))

    def test_on_plane(self):
        wanted = {"on_plane": {"point": (0, 0, 1), "normal": (0, 0, 1), "tolerance": 0.001}}
        self.assertTrue(er.matches_face(face_rec(normal=[0, 0, -1]), wanted))
        self.assertTrue(er.matches_face(face_rec(_samples=[[3, 3, 1.001]]), wanted))
        self.assertFalse(er.matches_face(face_rec(_samples=[[3, 3, 1.1]]), wanted))
        self.assertFalse(er.matches_face(face_rec(_samples=[]), wanted))

    def test_tilted_disc_with_its_samples_on_the_plane_is_not_on_it(self):
        wanted = {"on_plane": {"point": (0, 0, 1), "normal": (0, 0, 1), "tolerance": 0.001}}
        self.assertFalse(er.matches_face(face_rec(normal=[0, 0.5, 0.866], _samples=[[1, 0, 1], [-1, 0, 1]]), wanted))

    def test_edge_filters(self):
        circle = {"type": "circle", "diameter": 1.0, "length": 3.14, "normal": [0, 0, 1], "center": [1, 1, 1],
                  "bbox": [[0, 0, 1], [2, 2, 1]], "_samples": [[1, 0, 1]], "sweep_deg": 360.0}
        self.assertTrue(er.matches_edge(circle, {"curve_types": {"circle"}, "diameter": (1.0, 1.0), "length": (3, 4)}))
        self.assertFalse(er.matches_edge(circle, {"curve_types": {"line"}}))
        wanted = direction((0.0, 0.0, 1.0), signed=False, through=(1.0, 1.0, 0.0), through_tolerance=0.001)
        self.assertTrue(er.matches_edge(circle, {"axis": wanted}))

    def test_line_axis_is_its_direction(self):
        line = {"type": "line", "direction": [1, 0, 0], "start": [0, 2, 0], "length": 1.0, "bbox": None}
        self.assertTrue(er.matches_edge(line, {"axis": direction((1.0, 0.0, 0.0), signed=False)}))
        self.assertFalse(er.matches_edge(line, {"axis": direction((0.0, 1.0, 0.0), signed=False)}))

    def test_bbox_distance(self):
        box = ((0, 0, 0), (1, 1, 1))
        self.assertEqual(er.bbox_distance(box, (0.5, 0.5, 0.5)), 0.0)
        self.assertEqual(er.bbox_distance(box, (4, 5, 1)), 5.0)


def wedge(interior_deg):
    """A solid wedge along z: its two outward normals, and a containment test.

    The material fills *interior_deg* around -x. Over 180 the edge is concave.
    """
    half = math.radians(interior_deg) / 2

    def inside(p):
        angle = abs(math.atan2(p[1], -p[0]))
        return "inside" if angle < half else "outside"

    # Boundary rays sit at +-half from -x; each outward normal is square to its ray, away from the material.
    normal_a = (math.sin(half), math.cos(half), 0.0)
    normal_b = (math.sin(half), -math.cos(half), 0.0)
    return normal_a, normal_b, inside


class ConvexityTests(unittest.TestCase):
    def test_convex_wedges(self):
        for interior in (30, 60, 90, 120, 170):
            with self.subTest(interior=interior):
                a, b, contains = wedge(interior)
                self.assertEqual(er.classify_edge((0.0, 0.0, 0.0), a, b, contains), "convex")

    def test_concave_wedges(self):
        for interior in (190, 240, 270, 300, 330):
            with self.subTest(interior=interior):
                a, b, contains = wedge(interior)
                self.assertEqual(er.classify_edge((0.0, 0.0, 0.0), a, b, contains), "concave")

    def test_stepping_along_the_sum_could_not_tell_them_apart(self):
        # Why the step is the difference: the sum is outside at both.
        for interior in (90, 270):
            a, b, contains = wedge(interior)
            self.assertEqual(contains(tuple(1e-3 * (x + y) for x, y in zip(a, b))), "outside")

    def test_parallel_normals_are_tangent(self):
        self.assertEqual(er.classify_edge((0, 0, 0), (0, 0, 1), (0, 0, 1), lambda p: "inside"), "tangent")

    def test_just_past_tangent_is_measured(self):
        tilt = math.radians(er.TANGENT_DEG * 2)
        b = (math.sin(tilt), 0.0, math.cos(tilt))
        self.assertEqual(er.classify_edge((0, 0, 0), (0, 0, 1), b, lambda p: "outside"), "convex")

    def test_disagreement_is_no_answer(self):
        states = iter(["inside", "outside"])
        self.assertIsNone(er.classify_edge((0, 0, 0), (1, 0, 0), (0, 1, 0), lambda p: next(states)))

    def test_on_or_unknown_is_no_answer(self):
        self.assertIsNone(er.classify_edge((0, 0, 0), (1, 0, 0), (0, 1, 0), lambda p: "on"))

    def test_zero_normal_is_no_answer(self):
        self.assertIsNone(er.classify_edge((0, 0, 0), (0, 0, 0), (0, 1, 0), lambda p: "inside"))

    def test_read_from_faces(self):
        scene = type("S", (), {"contains": lambda self, entry, p: "outside"})()
        faces = [Face(plane(), normal=(1, 0, 0)), Face(plane(), normal=(0, 1, 0))]
        self.assertEqual(er.read_edge_convexity(Edge(Curve(0), faces=faces), {}, scene), "convex")

    def test_seam_reads_as_tangent(self):
        face = Face(cylinder())
        self.assertEqual(er.read_edge_convexity(Edge(Curve(0), faces=[face, face]), {}, None), "tangent")

    def test_evaluator_refusal_is_no_answer(self):
        bad = Face(plane())
        bad.evaluator = None
        self.assertIsNone(er.read_edge_convexity(Edge(Curve(0), faces=[bad, Face(plane())]), {}, None))


class FusionSceneTests(unittest.TestCase):
    """The only code asking Fusion: right calls, right arguments, and failures read as nothing."""

    class Types:
        BRepFaceEntityType, BRepEdgeEntityType, BRepVertexEntityType = 1, 2, 3

    def setUp(self):
        self.root = mock.Mock(allOccurrences=[], bRepBodies=[])
        self.design = mock.Mock(rootComponent=self.root)
        self.manager = mock.Mock()
        app = mock.Mock(measureManager=self.manager)
        for target, name, value in ((adsk.fusion, "BRepEntityTypes", self.Types),
                                    (adsk.core.Point3D, "create", lambda *p: p),
                                    (adsk.core.Application, "get", lambda: app)):
            patcher = mock.patch.object(target, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scene = er.FusionScene(self.design)

    def test_by_token(self):
        self.design.findEntityByToken.return_value = ["a", "b"]
        self.assertEqual(self.scene.by_token("t"), ["a", "b"])

    def test_rejected_token_names_nothing(self):
        self.design.findEntityByToken.side_effect = RuntimeError("bad token")
        self.assertEqual(self.scene.by_token("t"), [])

    def test_at_point_searches_hidden_bodies_too(self):
        self.root.findBRepUsingPoint.return_value = ["hit"]
        self.assertEqual(self.scene.at_point((1.0, 2.0, 3.0), "edge", 0.01), ["hit"])
        self.root.findBRepUsingPoint.assert_called_once_with((1.0, 2.0, 3.0), 2, 0.01, False)

    def test_distance(self):
        self.manager.measureMinimumDistance.return_value = mock.Mock(value=0.5)
        self.assertEqual(self.scene.distance("face", (0.0, 0.0, 0.0)), 0.5)
        self.manager.measureMinimumDistance.assert_called_once_with((0.0, 0.0, 0.0), "face")

    def test_unmeasurable_distance_is_none(self):
        self.manager.measureMinimumDistance.side_effect = RuntimeError("no")
        self.assertIsNone(self.scene.distance("face", (0.0, 0.0, 0.0)))

    def test_contains(self):
        body = mock.Mock()
        for value, name in ((0, "inside"), (1, "on"), (2, "outside"), (3, "unknown")):
            body.pointContainment.return_value = value
            self.assertEqual(self.scene.contains({"_body": body}, (0.0, 0.0, 0.0)), name)

    def test_entries_are_keyed_and_read_once(self):
        self.root.bRepBodies = [mock.Mock(isVisible=True, isSolid=True, boundingBox=None)]
        self.root.bRepBodies[0].name = "A"
        first = self.scene.entries()
        self.assertEqual(first[0]["key"], "root/A")
        self.assertIs(self.scene.entries(), first)


class LayerTests(unittest.TestCase):
    def test_nothing_at_module_level_imports_adsk(self):
        with open(os.path.join(os.path.dirname(er.__file__), "entity_refs.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse({name for name in names if name.startswith("adsk")})


if __name__ == "__main__":
    unittest.main()
