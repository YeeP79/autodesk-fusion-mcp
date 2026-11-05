"""find_entities: argument checks, the scan and its result, on fakes."""

import json
import math
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import find_entities as fe
from fusion_bridge import probe_geometry, tool_surface
from fusion_bridge.common import bodies

Budget = probe_geometry.Budget


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


def pt(p):
    return P(*p)


class Box:
    def __init__(self, low, high):
        self.minPoint, self.maxPoint = pt(low), pt(high)


class Evaluator:
    def __init__(self, normal):
        self.normal = normal

    def getNormalAtPoint(self, point):
        return True, pt(self.normal)


class Surface:
    def __init__(self, surface_type, **fields):
        self.surfaceType = surface_type
        self.__dict__.update(fields)


def plane_surface(normal=(0.0, 0.0, 1.0)):
    return Surface(0, normal=pt(normal), origin=pt((0, 0, 0)))


def cylinder_surface(radius=0.5, origin=(1.0, 1.0, 0.0), axis=(0.0, 0.0, 1.0)):
    return Surface(1, radius=radius, origin=pt(origin), axis=pt(axis))


class Vertex:
    def __init__(self, p):
        self.geometry = pt(p)


class Face:
    objectType = "adsk::fusion::BRepFace"
    nativeObject = None
    isParamReversed = None
    _next = 0

    def __init__(self, surface=None, area=4.0, point=(1.0, 1.0, 1.0), normal=(0.0, 0.0, 1.0),
                 box=((0, 0, 1), (2, 2, 1)), vertices=(), body=None):
        Face._next += 1
        self.tempId = Face._next
        self.entityToken = f"face-{self.tempId}"
        self.geometry = surface or plane_surface(normal)
        self.area = area
        self.pointOnFace = pt(point)
        self.evaluator = Evaluator(normal)
        self.boundingBox = Box(*box)
        self.vertices = [Vertex(p) for p in vertices]
        self.edges = []
        self.body = body


def bore(radius=0.5, sweep=360.0, height=2.0, concave=True, at=(1.0, 1.0, 0.0)):
    """A cylinder face whose area gives *sweep*; outward normal per *concave*."""
    x, y, z = at
    return Face(cylinder_surface(radius, at), area=radius * math.radians(sweep) * height,
                point=(x + radius, y, z + height / 2), normal=(-1.0 if concave else 1.0, 0.0, 0.0),
                box=((x - radius, y - radius, z), (x + radius, y + radius, z + height)),
                vertices=[(x + radius, y, z), (x + radius, y, z + height)])


class Curve:
    def __init__(self, curve_type, **fields):
        self.curveType = curve_type
        self.__dict__.update(fields)


class Edge:
    objectType = "adsk::fusion::BRepEdge"
    nativeObject = None
    evaluator = None
    _next = 0

    def __init__(self, curve=None, length=1.0, start=(0, 0, 0), end=(1, 0, 0), point=(0.5, 0.0, 0.0),
                 faces=(), box=((0, 0, 0), (1, 0, 0))):
        Edge._next += 1
        self.tempId = Edge._next
        self.entityToken = f"edge-{self.tempId}"
        self.geometry = curve or Curve(0)
        self.length = length
        self.startVertex, self.endVertex = Vertex(start), Vertex(end)
        self.pointOnEdge = pt(point)
        self.faces = list(faces)
        self.boundingBox = Box(*box)


class Collection:
    """A count/item collection, like BRepFaces."""

    def __init__(self, members):
        self.members = list(members)
        self.count = len(self.members)

    def item(self, index):
        return self.members[index]

    def __iter__(self):
        return iter(self.members)


class Solid:
    def __init__(self, name="Plate", faces=(), edges=(), parent=None):
        self.name = name
        self.faces = Collection(faces)
        self.edges = Collection(edges)
        self.parentComponent = parent
        self.assemblyContext = None
        for member in self.faces:
            member.body = self


def entry(body, path="root", visible=True, solid=True, index=0):
    return {"occurrence_path": path, "component": "c", "linked": False, "body": body.name, "index": index,
            "visible": visible, "solid": solid, "bbox": None, "_body": body}


class Scene:
    def __init__(self, *entries, tokens=(), distances=None, inside=None):
        self._entries = bodies.assign_keys(list(entries))
        self.tokens = list(tokens)
        self.distances = distances or {}
        self.inside = inside
        self.measured = []

    def entries(self):
        return self._entries

    def by_token(self, token):
        return self.tokens

    def at_point(self, point, kind, tolerance_cm=0.0):
        return []

    def distance(self, entity, point):
        self.measured.append(entity)
        return self.distances.get(id(entity), 0.0)

    def contains(self, entry, point):
        return self.inside


def options(**arguments):
    arguments.setdefault("entity", "face")
    arguments.setdefault("units", "cm")
    opts, err = fe.validate_args(arguments)
    if err:
        raise AssertionError(err)
    return opts


def run(scene, design=None, budget=None, **arguments):
    return fe.run(design, scene, options(**arguments), budget or Budget(max_samples=10_000))


class ValidationTests(unittest.TestCase):
    def error(self, **arguments):
        arguments.setdefault("entity", "face")
        _, err = fe.validate_args(arguments)
        return err

    def test_minimal(self):
        self.assertIsNone(self.error())

    def test_defaults(self):
        opts = options(units="in")
        self.assertEqual((opts["max_results"], opts["include_hidden"], opts["bodies"]), (50, False, None))
        self.assertTrue(opts["unfiltered"])

    def test_entity_is_required(self):
        self.assertIn("entity", fe.validate_args({})[1])
        self.assertIn("entity", self.error(entity="vertex"))

    def test_unknown_keys(self):
        self.assertIn("unknown", self.error(extra=1))
        self.assertIn("unknown", self.error(normal={"direction": [0, 0, 1], "tol": 1}))
        self.assertIn("unknown", self.error(diameter={"min": 1, "low": 0}))
        self.assertIn("unknown", self.error(near={"point": [0, 0, 0], "within": 1, "x": 1}))

    def test_type_filters_must_fit_the_entity(self):
        self.assertIn("faces", self.error(entity="edge", surface_types=["plane"]))
        self.assertIn("edges", self.error(curve_types=["line"]))
        self.assertIn("edges", self.error(edge_convexity="convex"))
        self.assertIn("faces", self.error(entity="edge", concavity="concave"))
        self.assertIn("faces", self.error(entity="edge", area={"min": 0}))

    def test_type_names(self):
        self.assertIn("surface_types", self.error(surface_types=["blob"]))
        self.assertIn("surface_types", self.error(surface_types=[]))
        self.assertIn("curve_types", self.error(entity="edge", curve_types="line"))

    def test_inverted_range(self):
        self.assertIn("inverted", self.error(diameter={"min": 2, "max": 1}))
        self.assertIsNone(self.error(diameter={"min": 1, "max": 1}))

    def test_empty_or_bad_range(self):
        self.assertIn("min", self.error(area={}))
        self.assertIn("finite", self.error(length={"min": float("nan")}))
        self.assertIn("finite", self.error(length={"max": "1"}))

    def test_zero_direction(self):
        self.assertIn("zero", self.error(normal={"direction": [0, 0, 0]}))
        self.assertIn("zero", self.error(axis={"direction": [0, 0, 0]}))
        self.assertIn("zero", self.error(on_plane={"point": [0, 0, 0], "normal": [0, 0, 0]}))

    def test_direction_is_normalized(self):
        opts = options(normal={"direction": [0, 0, 5]})
        self.assertEqual(opts["filters"]["normal"], {"direction": (0.0, 0.0, 1.0), "tolerance_deg": 1.0, "signed": True})

    def test_axis_defaults_to_unsigned(self):
        self.assertFalse(options(axis={"direction": [0, 0, 1]})["filters"]["axis"]["signed"])

    def test_tolerance_default_follows_the_units(self):
        wanted = options(units="mm", axis={"direction": [0, 0, 1], "through": [0, 0, 0]})["filters"]["axis"]
        self.assertAlmostEqual(wanted["through_tolerance"], 0.0254)
        plane = options(units="in", on_plane={"point": [0, 0, 0], "normal": [0, 0, 1]})["filters"]["on_plane"]
        self.assertAlmostEqual(plane["tolerance"], 0.001)

    def test_through_tolerance_needs_through(self):
        self.assertIn("through", self.error(axis={"direction": [0, 0, 1], "through_tolerance": 1}))

    def test_near_needs_both(self):
        self.assertIn("near.within", self.error(near={"point": [0, 0, 0]}))
        self.assertIn("near.point", self.error(near={"within": 1}))
        self.assertIn("near.within", self.error(near={"point": [0, 0, 0], "within": -1}))

    def test_inverted_box(self):
        self.assertIn("inverted", self.error(in_box={"min": [1, 0, 0], "max": [0, 1, 1]}))

    def test_sort_rules(self):
        self.assertIn("near", self.error(sort_by="distance"))
        self.assertIn("faces", self.error(entity="edge", sort_by="area"))
        self.assertIn("sort_by", self.error(sort_by="name"))

    def test_max_results_limits(self):
        for value in (0, 1001, 1.5, True):
            self.assertIn("max_results", self.error(max_results=value))

    def test_refs_are_checked(self):
        self.assertIn("of_face", self.error(of_face={}))
        self.assertIn("bodies", self.error(bodies=[]))
        self.assertIn("bodies", self.error(bodies=[{"body_name": "B"}]))

    def test_every_listed_filter_clears_unfiltered(self):
        for key, value in (("near", {"point": [0, 0, 0], "within": 1}), ("of_feature", "Boss"),
                           ("of_face", {"token": "t"}), ("diameter", {"min": 1})):
            self.assertFalse(options(**{key: value})["unfiltered"])


class ScopeTests(unittest.TestCase):
    def test_hidden_bodies_are_excluded_and_counted(self):
        shown, hidden = Solid("A", [Face()]), Solid("H", [Face()])
        payload, _ = run(Scene(entry(shown), entry(hidden, visible=False)))
        self.assertEqual(payload["bodies_scanned"], 1)
        self.assertEqual(payload["returned"], 1)
        self.assertIn("1 hidden", payload["warnings"][0])

    def test_include_hidden(self):
        scene = Scene(entry(Solid("A", [Face()])), entry(Solid("H", [Face()]), visible=False))
        payload, _ = run(scene, include_hidden=True)
        self.assertEqual(payload["bodies_scanned"], 2)
        self.assertNotIn("warnings", payload)

    def test_named_hidden_body_is_scanned(self):
        scene = Scene(entry(Solid("H", [Face()]), visible=False))
        payload, _ = run(scene, bodies=[{"occurrence_path": "root", "body_name": "H"}])
        self.assertEqual(payload["returned"], 1)

    def test_surface_bodies_are_skipped_unless_named(self):
        payload, _ = run(Scene(entry(Solid("S", [Face()]), solid=False)))
        self.assertEqual(payload["bodies_scanned"], 0)

    def test_missing_body_is_refused_with_candidates(self):
        payload, problem = run(Scene(entry(Solid("A"))), bodies=[{"occurrence_path": "root", "body_name": "B"}])
        self.assertIsNone(payload)
        self.assertIn("no body matches", problem["error"])

    def test_no_filter_on_a_large_body_is_refused(self):
        large = Solid("Big", [Face() for _ in range(fe.UNFILTERED_FACE_LIMIT + 1)])
        _, problem = run(Scene(entry(large)))
        self.assertIn("root/Big", problem["error"])

    def test_at_the_limit_is_allowed(self):
        body = Solid("Ok", [Face() for _ in range(fe.UNFILTERED_FACE_LIMIT)])
        self.assertEqual(run(Scene(entry(body)), max_results=1)[0]["matched"], fe.UNFILTERED_FACE_LIMIT)

    def test_a_filter_lifts_the_limit(self):
        large = Solid("Big", [Face() for _ in range(fe.UNFILTERED_FACE_LIMIT + 1)])
        payload, problem = run(Scene(entry(large)), surface_types=["cylinder"])
        self.assertIsNone(problem)
        self.assertEqual(payload["matched"], 0)


class OfFaceTests(unittest.TestCase):
    def setUp(self):
        self.top, self.side = Face(), Face()
        self.edge = Edge(faces=[self.top, self.side])
        self.far = Edge(faces=[self.side])
        self.top.edges = [self.edge]
        self.side.edges = [self.edge, self.far]
        self.body = Solid("A", [self.top, self.side], [self.edge, self.far])

    def test_edges_of_a_face(self):
        scene = Scene(entry(self.body), tokens=[self.top])
        payload, _ = run(scene, entity="edge", of_face={"token": "t"})
        self.assertEqual(payload["returned"], 1)
        self.assertEqual(payload["entities"][0]["ref"]["token"], self.edge.entityToken)
        self.assertEqual(payload["resolved"], [{"role": "of_face", "kind": "face", "resolved_by": "token"}])

    def test_neighbor_faces(self):
        payload, _ = run(Scene(entry(self.body), tokens=[self.top]), of_face={"token": "t"})
        self.assertEqual([e["ref"]["token"] for e in payload["entities"]], [self.side.entityToken])

    def test_ref_resolving_to_nothing_is_refused(self):
        payload, problem = run(Scene(entry(self.body)), of_face={"token": "t"})
        self.assertIsNone(payload)
        self.assertEqual(problem["code"], "not_found")
        self.assertIn("of_face", problem["error"])

    def test_split_face_needs_a_point(self):
        _, problem = run(Scene(entry(self.body), tokens=[Face(), Face()]), of_face={"token": "t"})
        self.assertEqual(problem["code"], "ambiguous_split")

    def test_face_on_a_body_not_named_is_refused(self):
        other = Solid("B", [Face()])
        scene = Scene(entry(self.body), entry(other), tokens=[self.top])
        _, problem = run(scene, of_face={"token": "t"}, bodies=[{"occurrence_path": "root", "body_name": "B"}])
        self.assertIn("root/A", problem["error"])


class Timeline:
    def __init__(self, *items):
        self.items = list(items)

    def __iter__(self):
        return iter(self.items)


class Item:
    isGroup = False
    healthState = 0
    errorOrWarningMessage = ""

    def __init__(self, name, entity, index):
        self.name, self.entity, self.index = name, entity, index


class Feature:
    objectType = "adsk::fusion::ExtrudeFeature"
    parentComponent = None

    def __init__(self, faces=None):
        if faces is not None:
            self.faces = faces


class Design:
    designType = 1

    def __init__(self, *items):
        self.timeline = Timeline(*items)


class OfFeatureTests(unittest.TestCase):
    def setUp(self):
        self.made, self.other = Face(), Face()
        self.edge = Edge(faces=[self.made])
        self.edge.body = None
        self.made.edges = [self.edge]
        self.body = Solid("A", [self.made, self.other], [self.edge, Edge()])
        self.edge.body = self.body
        self.design = Design(Item("Boss", Feature([self.made]), 0))

    def test_faces_of_a_feature(self):
        payload, _ = run(Scene(entry(self.body)), self.design, of_feature="Boss")
        self.assertEqual([e["ref"]["token"] for e in payload["entities"]], [self.made.entityToken])
        self.assertNotIn("warnings", payload)

    def test_edges_of_a_feature(self):
        payload, _ = run(Scene(entry(self.body)), self.design, entity="edge", of_feature="Boss")
        self.assertEqual([e["ref"]["token"] for e in payload["entities"]], [self.edge.entityToken])

    def test_same_temp_id_on_another_body_is_not_a_member(self):
        twin = Face()
        twin.tempId = self.made.tempId
        other = Solid("B", [twin])
        payload, _ = run(Scene(entry(other)), self.design, of_feature="Boss")
        self.assertEqual(payload["matched"], 0)

    def test_warns_when_not_last(self):
        design = Design(Item("Boss", Feature([self.made]), 0), Item("Combine1", Feature([]), 1))
        payload, _ = run(Scene(entry(self.body)), design, of_feature="Boss")
        self.assertIn("not the last", payload["warnings"][0])

    def test_unknown_name(self):
        _, problem = run(Scene(entry(self.body)), self.design, of_feature="Bos")
        self.assertIn("Close matches", problem["error"])

    def test_repeated_name_is_refused(self):
        design = Design(Item("Boss", Feature([]), 0), Item("Boss", Feature([]), 1))
        _, problem = run(Scene(entry(self.body)), design, of_feature="Boss")
        self.assertIn("2 timeline items", problem["error"])

    def test_item_without_faces(self):
        design = Design(Item("Sketch1", Feature(), 0))
        _, problem = run(Scene(entry(self.body)), design, of_feature="Sketch1")
        self.assertIn("no faces", problem["error"])

    def test_direct_design(self):
        design = Design()
        design.designType = 0
        _, problem = run(Scene(entry(self.body)), design, of_feature="Boss")
        self.assertIn("direct", problem["error"])


class ScanTests(unittest.TestCase):
    def test_bore_not_fillet(self):
        body = Solid("A", [bore(), bore(sweep=90.0, at=(5.0, 5.0, 0.0)), Face()])
        payload, _ = run(Scene(entry(body)), surface_types=["cylinder"], concavity="concave", sweep_deg={"min": 300})
        self.assertEqual(payload["returned"], 1)
        self.assertEqual(payload["entities"][0]["sweep_deg"], 360.0)

    def test_partial_cylinder_adds_the_trap_note(self):
        payload, _ = run(Scene(entry(Solid("A", [bore(sweep=90.0)]))), surface_types=["cylinder"])
        self.assertEqual(payload["warnings"], [bodies.TRAP_NOTE])

    def test_full_cylinder_has_no_note(self):
        self.assertNotIn("warnings", run(Scene(entry(Solid("A", [bore()]))), surface_types=["cylinder"])[0])

    def test_surface_type_rejects_before_describing(self):
        with mock.patch.object(fe, "describe_face") as describe:
            run(Scene(entry(Solid("A", [Face()]))), surface_types=["cylinder"])
        describe.assert_not_called()

    def test_near_measures_only_what_the_box_allows(self):
        close, far = Face(box=((0, 0, 0), (1, 1, 1))), Face(box=((50, 50, 50), (51, 51, 51)))
        scene = Scene(entry(Solid("A", [close, far])), distances={id(close): 0.05})
        payload, _ = run(scene, near={"point": [0, 0, 0], "within": 0.1})
        self.assertEqual(scene.measured, [close])
        self.assertEqual(payload["entities"][0]["distance"], 0.05)

    def test_near_uses_the_callers_units(self):
        face = Face(box=((0, 0, 0), (0, 0, 0)))
        scene = Scene(entry(Solid("A", [face])), distances={id(face): 2.54})
        self.assertEqual(run(scene, units="in", near={"point": [0, 0, 0], "within": 1.0})[0]["matched"], 1)
        self.assertEqual(run(scene, units="in", near={"point": [0, 0, 0], "within": 0.99})[0]["matched"], 0)

    def test_unmeasurable_distance_is_not_near(self):
        face = Face(box=((0, 0, 0), (1, 1, 1)))
        scene = Scene(entry(Solid("A", [face])), distances={id(face): None})
        self.assertEqual(run(scene, near={"point": [0, 0, 0], "within": 1})[0]["matched"], 0)

    def test_convexity_runs_only_when_asked(self):
        faces = [Face(normal=(1, 0, 0)), Face(normal=(0, 1, 0))]
        body = Solid("A", faces, [Edge(faces=faces)])
        scene = Scene(entry(body), inside="outside")
        with mock.patch.object(fe, "read_edge_convexity") as read:
            run(scene, entity="edge")
        read.assert_not_called()
        payload, _ = run(scene, entity="edge", edge_convexity="convex")
        self.assertEqual(payload["entities"][0]["convexity"], "convex")
        self.assertEqual(run(scene, entity="edge", edge_convexity="concave")[0]["matched"], 0)

    def test_edge_rows_carry_face_refs_only_when_returned(self):
        body = Solid("A", edges=[Edge(faces=[Face()]), Edge(faces=[Face()], box=((5, 0, 0), (6, 0, 0)))])
        with mock.patch.object(fe, "adjacent_refs", wraps=fe.adjacent_refs) as refs:
            payload, _ = run(Scene(entry(body)), entity="edge", max_results=1)
        self.assertEqual(refs.call_count, 1)
        self.assertEqual(payload["entities"][0]["faces"][0]["kind"], "face")

    def test_unreadable_entity_is_counted(self):
        real = fe.describe_face
        bad = Face()

        def describe(face, *args):
            if face is bad:
                raise RuntimeError("stale proxy")
            return real(face, *args)

        with mock.patch.object(fe, "describe_face", describe), mock.patch.object(fe, "log"):
            payload, _ = run(Scene(entry(Solid("A", [bad, Face()]))))
        self.assertEqual(payload["returned"], 1)
        self.assertIn("1 entities could not be read", payload["warnings"][0])

    def test_budget_sets_scan_capped(self):
        ticks = iter(range(1000))
        budget = Budget(max_samples=10_000, seconds=3, clock=lambda: next(ticks))
        body = Solid("A", [Face() for _ in range(10)])
        payload, _ = run(Scene(entry(body)), budget=budget)
        self.assertTrue(payload["scan_capped"])
        self.assertLess(payload["scanned"], 10)
        self.assertTrue(any("budget" in w for w in payload["warnings"]))

    def test_sample_cap_also_caps(self):
        body = Solid("A", [Face() for _ in range(5)])
        payload, _ = run(Scene(entry(body)), budget=Budget(max_samples=2))
        self.assertEqual((payload["scanned"], payload["scan_capped"]), (2, True))

    def test_no_private_keys_reach_the_result(self):
        payload, _ = run(Scene(entry(Solid("A", [Face(), bore()]))))
        text = json.dumps(payload)
        for key in ("_entity", "_owner", "_samples", "_temp_id"):
            self.assertNotIn(key, text)


def row(label, **fields):
    return {"label": label, "ref": {"point": [0, 0, 0]}, **fields}


class OrderTests(unittest.TestCase):
    def labels(self, records, sort_by=None):
        return [r["label"] for r in fe.order(records, sort_by)]

    def test_default_is_by_label_whatever_fusion_listed(self):
        a, b = [row("b"), row("a")], [row("a"), row("b")]
        self.assertEqual(self.labels(a), self.labels(b))

    def test_equal_labels_fall_to_body_then_point(self):
        records = [{"label": "x", "ref": {"body": {"occurrence_path": "b:1", "body_name": "B"}, "point": [0, 0, 0]}},
                   {"label": "x", "ref": {"body": {"occurrence_path": "a:1", "body_name": "B"}, "point": [0, 0, 0]}}]
        self.assertEqual(fe.order(records, None)[0]["ref"]["body"]["occurrence_path"], "a:1")

    def test_distance_is_nearest_first(self):
        self.assertEqual(self.labels([row("far", distance=2.0), row("near", distance=1.0)], "distance"), ["near", "far"])

    def test_sizes_are_largest_first_and_missing_last(self):
        records = [row("none"), row("small", diameter=1.0), row("big", diameter=2.0)]
        self.assertEqual(self.labels(records, "diameter"), ["big", "small", "none"])

    def test_length_reads_either_field(self):
        records = [row("edge", length=1.0), row("cyl", length_along_axis=2.0)]
        self.assertEqual(self.labels(records, "length"), ["cyl", "edge"])

    def test_ties_keep_label_order(self):
        self.assertEqual(self.labels([row("b", area=1.0), row("a", area=1.0)], "area"), ["a", "b"])


class ResultTests(unittest.TestCase):
    def test_max_results_truncates_and_keeps_order(self):
        faces = [Face(area=float(i + 1), box=((i, 0, 0), (i, 1, 1))) for i in range(5)]
        payload, _ = run(Scene(entry(Solid("A", faces))), max_results=2, sort_by="area")
        self.assertEqual((payload["matched"], payload["returned"], payload["truncated"]), (5, 2, True))
        self.assertEqual([e["area"] for e in payload["entities"]], [5.0, 4.0])

    def test_two_runs_agree_whatever_the_face_order(self):
        faces = [Face(area=float(i + 1), box=((i, 0, 0), (i, 1, 1))) for i in range(4)]
        first, _ = run(Scene(entry(Solid("A", faces))))
        second, _ = run(Scene(entry(Solid("A", list(reversed(faces))))))
        self.assertEqual([e["label"] for e in first["entities"]], [e["label"] for e in second["entities"]])

    def test_shape(self):
        payload, _ = run(Scene(entry(Solid("A", [Face()]))))
        self.assertEqual(payload["units"], {"length": "cm", "area": "cm^2", "volume": "cm^3"})
        self.assertEqual((payload["scanned"], payload["truncated"], payload["scan_capped"]), (1, False, False))
        row_ = payload["entities"][0]
        self.assertEqual(row_["ref"]["body"], {"occurrence_path": "root", "body_name": "A"})
        self.assertEqual(row_["normal"], [0.0, 0.0, 1.0])
        json.dumps(payload)

    def test_created_by_fills_only_when_asked(self):
        face = Face(bore().geometry, vertices=[(0, 0, 0), (0, 0, 1)])
        features = type("Fs", (), {"count": 1, "item": lambda self, i: Feature2([face])})()
        parent = type("C", (), {"features": features})()
        body = Solid("A", [face], parent=parent)
        self.assertIsNone(run(Scene(entry(body)))[0]["entities"][0]["created_by"])
        made = run(Scene(entry(body)), include_created_by=True)[0]["entities"][0]
        self.assertEqual((made["created_by"], made["created_by_type"]), ("Hole", "ExtrudeFeature"))


class Feature2:
    name = "Hole"
    objectType = "adsk::fusion::ExtrudeFeature"

    def __init__(self, faces):
        self.faces = faces


class HandlerTests(unittest.TestCase):
    def call(self, design, **arguments):
        app = type("App", (), {"activeProduct": design})()
        with mock.patch.object(fe, "get_app", lambda: app), mock.patch.object(fe, "log"), \
                mock.patch.object(fe.adsk.fusion, "Design", type("D", (), {"cast": staticmethod(lambda p: p)}), create=True):
            return fe.find_entities(arguments)

    def test_bad_arguments_never_touch_fusion(self):
        with mock.patch.object(fe, "get_app", side_effect=AssertionError("touched")):
            result = fe.find_entities({"entity": "blob"})
        self.assertTrue(result["isError"])

    def test_no_design(self):
        result = self.call(None, entity="face")
        self.assertTrue(result["isError"])
        self.assertIn("not a Fusion design", result["content"][0]["text"])

    def test_refusal_is_an_error_with_a_reason(self):
        scene = Scene(entry(Solid("A")))
        with mock.patch.object(fe, "FusionScene", return_value=scene):
            result = self.call(object(), entity="face", bodies=[{"occurrence_path": "root", "body_name": "B"}])
        self.assertTrue(result["isError"])
        self.assertIn("no body matches", json.loads(result["content"][0]["text"])["error"])

    def test_success_is_compact_json(self):
        with mock.patch.object(fe, "FusionScene", return_value=Scene(entry(Solid("A", [Face()])))):
            result = self.call(object(), entity="face")
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertNotIn("\n", text)
        self.assertEqual(json.loads(text)["returned"], 1)
        self.assertIn("elapsed_seconds", json.loads(text))

    def test_failure_is_reported(self):
        with mock.patch.object(fe, "FusionScene", side_effect=RuntimeError("boom")):
            result = self.call(object(), entity="face")
        self.assertTrue(result["isError"])
        self.assertIn("boom", result["content"][0]["text"])


class SchemaTests(unittest.TestCase):
    def tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "find_entities")

    def test_schema_matches_the_handler(self):
        self.assertEqual(set(self.tool()["inputSchema"]["properties"]), fe._ARGS)

    def test_rejects_unknown_keys_at_every_object(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.tool()["inputSchema"])

    def test_no_keywords_clients_reject(self):
        text = json.dumps(self.tool()["inputSchema"])
        for keyword in ("minItems", "minLength", "uniqueItems", "anyOf", "oneOf"):
            self.assertNotIn(keyword, text)

    def test_enums_match(self):
        props = self.tool()["inputSchema"]["properties"]
        self.assertEqual(sorted(props["surface_types"]["items"]["enum"]), fe.SURFACE_NAMES)
        self.assertEqual(sorted(props["curve_types"]["items"]["enum"]), fe.CURVE_NAMES)
        self.assertEqual(tuple(props["sort_by"]["enum"]), fe.SORT_KEYS)


if __name__ == "__main__":
    unittest.main()
