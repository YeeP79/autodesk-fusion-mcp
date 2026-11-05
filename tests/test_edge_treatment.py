"""edge_treatment: schema, validation, the pure logic, and whole runs against a fake
block whose feature adds refuse by scope and move edges the way a chamfer does."""

import math
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import edge_treatment as et
from fusion_bridge import tool_surface
from fusion_bridge.common import bodies
from fusion_bridge.common.entity_refs import Resolution

BLOCK = {"occurrence_path": "root", "body_name": "Block"}
TOP_Z = 1.0


def options(**overrides):
    arguments = {"kind": "chamfer", "units": "cm", "sets": [{"points": [[0, 0, 0]], "body": BLOCK, "size": 0.1}],
                 **overrides}
    opts, err = et.validate_args(arguments)
    assert err is None, err
    return opts


def error_of(**overrides):
    arguments = {"kind": "chamfer", "units": "cm", "sets": [{"points": [[0, 0, 0]], "body": BLOCK, "size": 0.1}],
                 **overrides}
    opts, err = et.validate_args(arguments)
    assert opts is None
    return err


def one_set(**fields):
    return error_of(sets=[{"points": [[0, 0, 0]], "body": BLOCK, "size": 0.1, **fields}])


# -- Fakes -----------------------------------------------------------------------


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Box:
    def __init__(self, points):
        self.minPoint = P(*(min(p[i] for p in points) for i in range(3)))
        self.maxPoint = P(*(max(p[i] for p in points) for i in range(3)))


class Members(list):
    count = property(len)


class Vertex:
    def __init__(self, point):
        self.point = point
        self.edges = Members()

    geometry = property(lambda self: P(*self.point))


class Line:
    curveType = 0


class Evaluator:
    def __init__(self, edge):
        self.edge = edge

    def getParameterExtents(self):
        return True, 0.0, 1.0

    def getPointAtParameter(self, t):
        a, b = self.edge.start.point, self.edge.end.point
        return True, P(*(p + (q - p) * t for p, q in zip(a, b)))


class Edge:
    objectType = "adsk::fusion::BRepEdge"
    assemblyContext = None
    nativeObject = None
    geometry = Line()

    def __init__(self, name, start, end, body):
        self.name, self.start, self.end, self.body = name, start, end, body
        self.startVertex, self.endVertex = start, end
        self.faces = Members()
        self.entityToken = f"edge-{name}"
        start.edges.append(self)
        end.edges.append(self)

    evaluator = property(Evaluator)
    length = property(lambda self: math.dist(self.start.point, self.end.point))
    boundingBox = property(lambda self: Box([self.start.point, self.end.point]))
    pointOnEdge = property(lambda self: P(*(sum(c) / 2 for c in zip(self.start.point, self.end.point))))


class Face:
    objectType = "adsk::fusion::BRepFace"
    assemblyContext = None
    nativeObject = None
    area = 4.0
    geometry = None

    def __init__(self, name, center, body):
        self.name, self.center, self.body = name, center, body
        self.edges = Members()
        self.entityToken = f"face-{name}"

    pointOnFace = property(lambda self: P(*self.center))


class Body:
    isSolid = True
    isVisible = True
    nativeObject = None
    assemblyContext = None
    isValid = True
    name = "Block"
    lumps = Members()
    shells = Members()
    boundingBox = Box([(0, 0, 0), (2, 2, 1)])

    def __init__(self):
        self.volume = 4.0
        self.edges, self.faces = Members(), Members()
        self.parentComponent = None


class Root:
    allOccurrences = ()

    def __init__(self, members):
        self.bRepBodies = members


class Design:
    designType = 1
    activeEditObject = None
    parentDocument = None
    timeline = type("T", (), {"count": 0, "markerPosition": 0, "__iter__": lambda self: iter(())})()

    def __init__(self, body):
        self.rootComponent = Root(Members([body]))


class Feature:
    healthState = 0
    errorOrWarningMessage = ""
    parentComponent = None
    timelineObject = None
    objectType = "adsk::fusion::ChamferFeature"

    def __init__(self, world, saved, name, faces=()):
        self.world, self.saved, self.name, self.faces = world, saved, name, Members(faces)

    def deleteMe(self):
        self.world.deletes += 1
        self.world.restore(self.saved)
        return True


class Curved:
    def __init__(self, surface, **radii):
        self.geometry = type("G", (), {"surfaceType": surface, **radii})()


def link(body, face, edge):
    face.edges.append(edge)
    edge.faces.append(face)


def block():
    """A 2 x 2 block with a top face, four side faces and the four top edges north, east, south, west."""
    body = Body()
    corners = [Vertex(p) for p in [(0, 0, TOP_Z), (2, 0, TOP_Z), (2, 2, TOP_Z), (0, 2, TOP_Z)]]
    top = Face("top", (1, 1, TOP_Z), body)
    body.faces.append(top)
    for i, name in enumerate(("south", "east", "north", "west")):
        a, b = corners[i], corners[(i + 1) % 4]
        edge = Edge(name, a, b, body)
        middle = tuple((p + q) / 2 for p, q in zip(a.point, b.point))
        side = Face(f"{name}-side", (middle[0], middle[1], 0.5), body)
        link(body, top, edge)
        link(body, side, edge)
        body.edges.append(edge)
        body.faces.append(side)
    return body


MIDPOINTS = {"south": [1, 0, TOP_Z], "east": [2, 1, TOP_Z], "north": [1, 2, TOP_Z], "west": [0, 1, TOP_Z]}


class World:
    """The session ``Job`` drives: refuses by scope, and a build moves edges into the faces."""

    def __init__(self, body=None, refuse=None, swap=False, sizes=None, error=None, lose_after_first=(), radii=()):
        self.body = body or block()
        self.design = Design(self.body)
        self.refuse, self.swap, self.sizes, self.error = refuse, swap, sizes or {}, error
        self.lose_after_first, self.radii = list(lose_after_first), radii
        self.adds, self.flips, self.deletes, self.refreshes = [], [], 0, 0

    def entries(self):
        return bodies.assign_keys(list(bodies.body_entries(self.design.rootComponent)))

    def refresh(self):
        self.refreshes += 1

    def by_token(self, token):
        members = list(self.body.edges) + list(self.body.faces)
        return [m for m in members if m.entityToken == token]

    def at_point(self, point, kind, tolerance_cm=0.0):
        if kind != "edge":
            return []
        return [e for e in self.body.edges if self.distance(e, point) <= tolerance_cm]

    def distance(self, entity, point):
        return et.point_to_segment(point, entity.start.point, entity.end.point)

    def length_cm(self, value, units):
        if value["number"] is None:
            return self.sizes[value["text"]]
        return value["number"]

    def _saved(self):
        return {"edges": list(self.body.edges), "volume": self.body.volume,
                "faces": {id(f): (f, list(f.edges)) for f in self.body.faces}}

    def restore(self, saved):
        self.body.edges[:] = saved["edges"]
        self.body.volume = saved["volume"]
        for face, edges in saved["faces"].values():
            face.edges[:] = edges

    def _treat(self, edge, on_top, on_side):
        """Replace *edge* with one inset on each of its two faces, at those distances."""
        self.body.edges.remove(edge)
        for face in edge.faces:
            inset = on_top if face.name == "top" else on_side
            middle = tuple((a + b) / 2 for a, b in zip(edge.start.point, edge.end.point))
            toward = [c - m for c, m in zip(face.center, middle)]
            length = math.hypot(*toward)
            shift = [t / length * inset for t in toward]
            start = Vertex(tuple(p + s for p, s in zip(edge.start.point, shift)))
            end = Vertex(tuple(p + s for p, s in zip(edge.end.point, shift)))
            face.edges.remove(edge)
            face.edges.append(Edge(f"{edge.name}-{face.name}", start, end, self.body))

    def add(self, entry, located, kind, flipped):
        names = sorted(e.name for item in located for e in item["edges"])
        self.adds.append(names)
        self.flips.append(sorted(flipped))
        if self.refuse and self.refuse(names, located):
            raise RuntimeError(f"ASM_BL_CAP_COMPLEX {names}")
        saved = self._saved()
        for item in located:
            job = item["set"]
            size, two = job["size_cm"], job["size_two_cm"]
            if two is None:
                two = size
            elif self.swap == "always" or (self.swap and job["index"] not in flipped):
                size, two = two, size
            for edge in item["edges"]:
                self._treat(edge, size, two)
        self.body.volume -= 0.01 * len(names)
        for name in self.lose_after_first:
            self.body.edges[:] = [e for e in self.body.edges if e.name != name]
        self.lose_after_first = []
        if self.error:
            feature = Feature(self, saved, "Bad")
            feature.healthState, feature.errorOrWarningMessage = 2, self.error
            return feature
        return Feature(self, saved, f"Chamfer{len(self.adds)}", [Curved(s, **r) for s, r in self.radii])


def run(world, **overrides):
    arguments = {"kind": "chamfer", "units": "cm",
                 "sets": [{"points": list(MIDPOINTS.values()), "body": BLOCK, "size": 0.1}], **overrides}
    opts, err = et.validate_args(arguments)
    assert err is None, err
    return et.Job(world.design, None, world, opts).run()


def scopes(payload):
    return [(row["scope"], row["edges"], row["outcome"]) for row in payload["attempts"]]


# -- Schema ----------------------------------------------------------------------


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == tool_surface.EDGE_TREATMENT)

    def test_strict_with_required_kind_and_sets(self):
        schema = self.tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(sorted(schema["required"]), ["kind", "sets"])
        self.assertFalse(schema["properties"]["sets"]["items"]["additionalProperties"])

    def test_every_key_the_handler_takes_is_in_the_schema(self):
        properties = self.tool["inputSchema"]["properties"]
        self.assertEqual(set(properties), et._KNOWN)
        self.assertEqual(set(properties["sets"]["items"]["properties"]), et._SET_KNOWN)

    def test_no_forbidden_keywords(self):
        text = str(self.tool["inputSchema"])
        for keyword in ("minItems", "minLength", "uniqueItems", "anyOf", "oneOf"):
            self.assertNotIn(keyword, text)


# -- Validation ------------------------------------------------------------------


class TestValidation(unittest.TestCase):
    def test_defaults(self):
        opts = options()
        self.assertEqual(opts["strategy"], "split_on_refusal")
        self.assertFalse(opts["dry_run"])
        self.assertAlmostEqual(opts["tolerance"], 0.00254)

    def test_tolerance_is_in_units(self):
        self.assertAlmostEqual(options(units="in", tolerance=0.01)["tolerance"], 0.0254)

    def test_arguments_not_an_object(self):
        self.assertIn("object", et.validate_args([])[1])

    def test_unknown_key(self):
        self.assertIn("unknown", error_of(spin=1))

    def test_bad_kind_units_strategy(self):
        self.assertIn("kind", error_of(kind="bevel"))
        self.assertIn("units", error_of(units="ft"))
        self.assertIn("strategy", error_of(strategy="hope"))

    def test_bad_tolerance(self):
        for value in (0, -1, float("nan"), "1", True):
            self.assertIn("tolerance", error_of(tolerance=value))

    def test_sets_must_be_a_non_empty_array(self):
        self.assertIn("sets", error_of(sets=[]))
        self.assertIn("sets", error_of(sets="edge"))
        self.assertIn("must be an object", error_of(sets=[3]))

    def test_size_must_be_above_zero(self):
        self.assertIn("greater than 0", one_set(size=0))
        self.assertIn("greater than 0", one_set(size=-0.1))

    def test_size_and_expression_are_exclusive(self):
        self.assertIn("not both", one_set(size_expression="1 mm"))

    def test_size_required(self):
        self.assertIn("required", error_of(sets=[{"points": [[0, 0, 0]], "body": BLOCK}]))

    def test_bare_expression_refused(self):
        arguments = {"points": [[0, 0, 0]], "body": BLOCK, "size_expression": "2"}
        self.assertIn("no unit", error_of(sets=[arguments]))

    def test_fillet_takes_no_size_two(self):
        err = error_of(kind="fillet", sets=[{"points": [[0, 0, 0]], "body": BLOCK, "size": 0.1, "size_two": 0.2}])
        self.assertIn("one radius", err)

    def test_size_two_needs_a_face(self):
        self.assertIn("size_on_face", one_set(size_two=0.2))

    def test_face_needs_size_two(self):
        self.assertIn("only means", one_set(size_on_face={"token": "t"}))

    def test_a_kind_per_set_is_refused_with_the_reason(self):
        self.assertIn("cannot share", one_set(kind="fillet"))

    def test_points_need_a_body(self):
        self.assertIn("'body'", error_of(sets=[{"points": [[0, 0, 0]], "size": 0.1}]))

    def test_no_edges_named(self):
        self.assertIn("names no edges", error_of(sets=[{"size": 0.1}]))

    def test_bad_point(self):
        self.assertIn("three finite", error_of(sets=[{"points": [[0, 0]], "body": BLOCK, "size": 0.1}]))
        self.assertIn("three finite", error_of(sets=[{"points": [[0, 0, math.inf]], "body": BLOCK, "size": 0.1}]))

    def test_an_edge_ref_cannot_be_a_face(self):
        self.assertIn("not an edge", error_of(sets=[{"edges": [{"token": "t", "kind": "face"}], "size": 0.1}]))

    def test_size_on_face_cannot_be_an_edge(self):
        self.assertIn("not a face", one_set(size_two=0.2, size_on_face={"token": "t", "kind": "edge"}))

    def test_ref_takes_the_sets_body_and_kind(self):
        opts = options(sets=[{"edges": [{"token": "t"}], "body": BLOCK, "size": 0.1}])
        ref = opts["sets"][0]["refs"][0]
        self.assertEqual(ref["kind"], "edge")
        self.assertEqual(ref["body"], ("root", "Block", None))

    def test_blank_expression_counts_as_absent(self):
        opts = options(sets=[{"points": [[0, 0, 0]], "body": BLOCK, "size": 0.1, "size_expression": ""}])
        self.assertEqual(opts["sets"][0]["size"]["number"], 0.1)


# -- Pure geometry ---------------------------------------------------------------


class TestPointToSegment(unittest.TestCase):
    def test_on_the_span_anywhere(self):
        self.assertAlmostEqual(et.point_to_segment((0.3, 0, 0), (0, 0, 0), (1, 0, 0)), 0.0)

    def test_beyond_an_end_measures_from_that_end(self):
        self.assertAlmostEqual(et.point_to_segment((2, 0, 0), (0, 0, 0), (1, 0, 0)), 1.0)
        self.assertAlmostEqual(et.point_to_segment((-3, 4, 0), (0, 0, 0), (1, 0, 0)), 5.0)

    def test_beside(self):
        self.assertAlmostEqual(et.point_to_segment((0.5, 2, 0), (0, 0, 0), (1, 0, 0)), 2.0)

    def test_zero_length(self):
        self.assertAlmostEqual(et.point_to_segment((0, 3, 4), (0, 0, 0), (0, 0, 0)), 5.0)


class TestOffCircle(unittest.TestCase):
    def test_on_the_rim(self):
        self.assertAlmostEqual(et.off_circle((0, 2, 0), (0, 0, 0), (0, 0, 1), 2.0), 0.0)

    def test_out_of_plane_and_off_the_rim_cannot_cancel(self):
        self.assertAlmostEqual(et.off_circle((3, 0, 1), (0, 0, 0), (0, 0, 1), 2.0), math.sqrt(2))


class TestCoplanar(unittest.TestCase):
    def test_square(self):
        self.assertTrue(et.coplanar([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], 1e-6))

    def test_off_plane(self):
        self.assertFalse(et.coplanar([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0.1)], 1e-3))

    def test_collinear_and_few(self):
        self.assertTrue(et.coplanar([(0, 0, 0), (1, 0, 0), (2, 0, 0)], 1e-6))
        self.assertTrue(et.coplanar([(0, 0, 0)], 1e-6))
        self.assertTrue(et.coplanar([(1, 1, 1), (1, 1, 1), (1, 1, 1)], 1e-6))


class TestPlaneGroups(unittest.TestCase):
    def test_two_planes(self):
        top_a = [(0, 0, 1), (1, 0, 1), (0, 1, 1)]
        bottom = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        top_b = [(2, 2, 1), (3, 2, 1), (2, 3, 1)]
        self.assertEqual(et.plane_groups([top_a, bottom, top_b], 1e-6), [[0, 2], [1]])

    def test_a_lone_line_joins_a_plane_it_lies_on(self):
        top = [(0, 0, 1), (1, 0, 1), (0, 1, 1)]
        line = [(5, 5, 1), (6, 5, 1)]
        self.assertEqual(et.plane_groups([top, line], 1e-6), [[0, 1]])


# -- Chains ----------------------------------------------------------------------


def path(points, body=None, close=False):
    vertices = [Vertex(p) for p in points]
    pairs = list(zip(vertices, vertices[1:]))
    if close:
        pairs.append((vertices[-1], vertices[0]))
    return [Edge(str(i), a, b, body) for i, (a, b) in enumerate(pairs)]


SQUARE = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]


class Unreadable:
    @property
    def startVertex(self):
        raise RuntimeError("stale")


class TestChains(unittest.TestCase):
    def test_square_is_one_closed_chain(self):
        edges = path(SQUARE, close=True)
        self.assertEqual(et.chains(edges), [[0, 1, 2, 3]])
        self.assertTrue(et.chain_closed(edges, [0, 1, 2, 3]))

    def test_two_squares(self):
        edges = path(SQUARE, close=True) + path([(x + 5, y, z) for x, y, z in SQUARE], close=True)
        self.assertEqual(et.chains(edges), [[0, 1, 2, 3], [4, 5, 6, 7]])

    def test_open_path_is_one_open_chain(self):
        edges = path(SQUARE)
        self.assertEqual(et.chains(edges), [[0, 1, 2]])
        self.assertFalse(et.chain_closed(edges, [0, 1, 2]))

    def test_an_edge_left_out_of_a_loop_opens_it(self):
        edges = path(SQUARE, close=True)
        del edges[1]
        self.assertEqual(et.chains(edges), [[0, 1, 2]])
        self.assertFalse(et.chain_closed(edges, [0, 1, 2]))

    def test_unreadable_vertices_stand_alone(self):
        edges = path(SQUARE) + [Unreadable()]
        self.assertEqual(et.chains(edges), [[0, 1, 2], [3]])
        self.assertFalse(et.chain_closed(edges, [3]))

    def test_order_within_a_chain_is_input_order(self):
        edges = path(SQUARE, close=True)
        shuffled = [edges[2], edges[0], edges[3], edges[1]]
        self.assertEqual(et.chains(shuffled), [[0, 1, 2, 3]])

    def test_a_circle_is_a_closed_chain_of_one(self):
        vertex = Vertex((1, 0, 0))
        edge = Edge("rim", vertex, vertex, None)
        self.assertTrue(et.chain_closed([edge], [0]))

    def test_empty(self):
        self.assertEqual(et.chains([]), [])


# -- Verdicts --------------------------------------------------------------------


class TestDecisive(unittest.TestCase):
    def test_at_the_two_percent_line(self):
        self.assertTrue(et.decisive(1.0, 0.98))
        self.assertFalse(et.decisive(1.0, 0.981))

    def test_either_way_round(self):
        self.assertTrue(et.decisive(0.98, 1.0))

    def test_equal(self):
        self.assertFalse(et.decisive(0.5, 0.5))


class TestClassifyInset(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(et.classify_inset(0.1, 0.1, 0.2), "size")
        self.assertEqual(et.classify_inset(0.2, 0.1, 0.2), "size_two")

    def test_at_a_quarter_of_the_gap(self):
        self.assertEqual(et.classify_inset(1.25, 1.0, 2.0), "size")
        self.assertEqual(et.classify_inset(1.75, 1.0, 2.0), "size_two")

    def test_just_past_a_quarter_is_neither(self):
        self.assertIsNone(et.classify_inset(1.26, 1.0, 2.0))
        self.assertIsNone(et.classify_inset(1.74, 1.0, 2.0))

    def test_far_off(self):
        self.assertIsNone(et.classify_inset(9.0, 1.0, 2.0))


class TestInsetVerdict(unittest.TestCase):
    def test_all_right(self):
        self.assertEqual(et.inset_verdict(["size", "size"]), "ok")

    def test_any_wrong_way_fails_the_set(self):
        self.assertEqual(et.inset_verdict(["size", "size_two", "size"]), et.SWAPPED)

    def test_neither_is_a_mismatch(self):
        self.assertEqual(et.inset_verdict(["size", None]), "mismatch")

    def test_nothing_read(self):
        self.assertEqual(et.inset_verdict([]), "not_measured")

    def test_a_neighbor_laying_size_two_proves_nothing(self):
        self.assertEqual(et.inset_verdict(["size_two"], beside_swap=True), "not_measured")


class TestEqualVerdict(unittest.TestCase):
    def test_ok_within_two_percent(self):
        self.assertEqual(et.equal_verdict([0.1, 0.1015], 0.1, 1e-4), "ok")

    def test_mismatch(self):
        self.assertEqual(et.equal_verdict([0.1, 0.2], 0.1, 1e-4), "mismatch")

    def test_nothing_read(self):
        self.assertEqual(et.equal_verdict([], 0.1, 1e-4), "not_measured")

    def test_inset_at_a_neighbors_size_is_not_judged(self):
        self.assertEqual(et.equal_verdict([0.1, 0.3], 0.1, 1e-4, beside=[0.3]), "ok")
        self.assertEqual(et.equal_verdict([0.3], 0.1, 1e-4, beside=[0.3]), "not_measured")


class TestRadiusVerdict(unittest.TestCase):
    def test_a_radius_matches(self):
        self.assertEqual(et.radius_verdict([0.5, 0.25], 0.25, 1e-3), ("ok", 0.25))

    def test_none_matches(self):
        self.assertEqual(et.radius_verdict([0.5], 0.25, 1e-3), ("mismatch", 0.5))

    def test_no_radius_to_read(self):
        self.assertEqual(et.radius_verdict([], 0.25, 1e-3), ("not_measured", None))


class TestFilletRadii(unittest.TestCase):
    def test_cylinder_and_torus_minor_only(self):
        feature = type("F", (), {"faces": [Curved(1, radius=0.25), Curved(4, majorRadius=3.0, minorRadius=0.5),
                                           Curved(7), Curved(0)]})()
        self.assertEqual(et.fillet_radii(feature), [0.25, 0.5])

    def test_unreadable_feature(self):
        self.assertEqual(et.fillet_radii(object()), [])


class TestEdgeSuspects(unittest.TestCase):
    show = staticmethod(lambda value: value)

    def test_neighbor_shorter_than_size(self):
        why = et.edge_suspects(1.0, [0.004, 2.0], [], 0.06, self.show)
        self.assertEqual(why, ["neighbor edge 0.004 long is shorter than size 0.06"])

    def test_at_the_size_is_not_a_suspect(self):
        self.assertEqual(et.edge_suspects(0.06, [0.06], [0.06], 0.06, self.show), [])

    def test_edge_itself_short(self):
        self.assertEqual(et.edge_suspects(0.05, [], [], 0.06, self.show),
                         ["edge 0.05 long is shorter than size 0.06"])

    def test_sliver_face(self):
        self.assertIn("sliver face 0.01", et.edge_suspects(1.0, [], [0.01, 2.0], 0.06, self.show)[0])

    def test_unreadable_values_are_not_suspects(self):
        self.assertEqual(et.edge_suspects(None, [None], [None], 0.06, self.show), [])


class TestOffEdge(unittest.TestCase):
    def test_line_uses_the_span(self):
        edge = path([(0, 0, 0), (1, 0, 0)])[0]
        self.assertAlmostEqual(et.off_edge(edge, (0.5, 1, 0)), 1.0)

    def test_circle_uses_the_curve_not_the_chord(self):
        vertex = Vertex((2, 0, 0))
        edge = Edge("rim", vertex, vertex, None)
        edge.geometry = type("C", (), {"curveType": 2, "center": P(0, 0, 0), "normal": P(0, 0, 1), "radius": 2.0})()
        self.assertAlmostEqual(et.off_edge(edge, (-2, 0, 0)), 0.0)

    def test_unreadable(self):
        self.assertIsNone(et.off_edge(Unreadable(), (0, 0, 0)))


class Scene:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def distance(self, edge, point):
        self.asked.append(edge)
        return self.answer(edge, point) if callable(self.answer) else self.answer


class TestEdgeDistance(unittest.TestCase):
    def setUp(self):
        self.edge = path([(0, 0, 0), (1, 0, 0)])[0]

    def test_far_box_settles_it_unasked(self):
        scene = Scene(0.0)
        self.assertAlmostEqual(et.edge_distance(scene, self.edge, (5, 0, 0), limit=0.1), 4.0)
        self.assertEqual(scene.asked, [])

    def test_near_box_asks_fusion(self):
        scene = Scene(0.05)
        self.assertEqual(et.edge_distance(scene, self.edge, (0.5, 0, 0), limit=0.1), 0.05)
        self.assertEqual(len(scene.asked), 1)

    def test_a_failed_measure_falls_back_to_the_chord(self):
        self.assertAlmostEqual(et.edge_distance(Scene(None), self.edge, (0.5, 2, 0)), 2.0)


class TestNearest(unittest.TestCase):
    def test_near_edges_nearest_first(self):
        a, b, c = path([(0, 0, 0), (1, 0, 0)])[0], path([(0, 0.1, 0), (1, 0.1, 0)])[0], path([(0, 9, 0), (1, 9, 0)])[0]
        scene = Scene(lambda edge, point: et.point_to_segment(point, edge.start.point, edge.end.point))
        self.assertEqual(et.near_edges(scene, [b, c, a], (0.5, 0.01, 0), 0.5), [a, b])

    def test_nearest_distance_skips_boxes_past_the_best(self):
        a, far = path([(0, 0, 0), (1, 0, 0)])[0], path([(0, 9, 0), (1, 9, 0)])[0]
        scene = Scene(lambda edge, point: et.point_to_segment(point, edge.start.point, edge.end.point))
        self.assertAlmostEqual(et.nearest_distance(scene, [far, a], (0.5, 0.5, 0)), 0.5)
        self.assertEqual(scene.asked, [a])

    def test_nearest_distance_of_nothing(self):
        self.assertIsNone(et.nearest_distance(Scene(0.0), [], (0, 0, 0)))


# -- The ladder, alone -----------------------------------------------------------


def part(name, *anchors, tangent=False):
    return {"name": name, "set": {"tangent": tangent},
            "anchors": [{"name": a, "state": "pending"} for a in anchors]}


class Ladder:
    """Refuses any step whose edges are all in ``refused`` scope names."""

    def __init__(self, refuse, planes=None, chain_map=None):
        self.refuse, self.planes, self.chain_map = refuse, planes, chain_map or {}
        self.steps, self.gave_up = [], []

    def try_step(self, scope, parts):
        names = sorted(a["name"] for p in parts for a in et.pending(p))
        self.steps.append((scope, names))
        if self.refuse(scope, names):
            return f"no {names}"
        for p in parts:
            for anchor in et.pending(p):
                anchor["state"] = "built"
        return None

    def split(self, parts, scope):
        def by_plane(members):
            return self.planes(members) if self.planes else [members]

        def chains_of(member):
            names = self.chain_map.get(member["name"])
            if not names:
                return [et.pending(member)]
            lookup = {a["name"]: a for a in member["anchors"]}
            return [[lookup[n] for n in chain] for chain in names]

        return et.next_rung(parts, scope, by_plane, chains_of)

    def give_up(self, scope, parts, refusal):
        self.gave_up.append((scope, sorted(a["name"] for p in parts for a in et.pending(p))))
        for p in parts:
            for anchor in et.pending(p):
                anchor["state"] = "refused"

    def run(self, parts):
        et.ladder(parts, "together", self.try_step, self.split, self.give_up)


class TestLadder(unittest.TestCase):
    def test_built_first_time_goes_no_lower(self):
        ladder = Ladder(lambda scope, names: False)
        ladder.run([part("A", "a1", "a2")])
        self.assertEqual(ladder.steps, [("together", ["a1", "a2"])])

    def test_order_with_every_rung(self):
        planes = lambda members: [[m for m in members if m["name"] != "C"], [m for m in members if m["name"] == "C"]]  # noqa: E731
        ladder = Ladder(lambda scope, names: scope != "edge", planes, {"A": [["a1", "a2"], ["a3"]]})
        ladder.run([part("A", "a1", "a2", "a3"), part("B", "b1"), part("C", "c1")])
        self.assertEqual([step[0] for step in ladder.steps[:3]], ["together", "plane", "set"])
        self.assertEqual(ladder.steps[3], ("loop", ["a1", "a2"]))
        self.assertEqual(ladder.steps[4:6], [("edge", ["a1"]), ("edge", ["a2"])])
        self.assertEqual(ladder.steps[6], ("loop", ["a3"]))

    def test_plane_rung_skipped_when_all_sets_share_a_plane(self):
        ladder = Ladder(lambda scope, names: scope == "together")
        ladder.run([part("A", "a1"), part("B", "b1")])
        self.assertEqual([s for s, _ in ladder.steps], ["together", "set", "set"])

    def test_plane_rung_skipped_when_every_set_has_its_own_plane(self):
        ladder = Ladder(lambda scope, names: scope == "together", lambda members: [[m] for m in members])
        ladder.run([part("A", "a1"), part("B", "b1")])
        self.assertEqual([s for s, _ in ladder.steps], ["together", "set", "set"])

    def test_set_rung_skipped_with_one_set(self):
        ladder = Ladder(lambda scope, names: scope == "together", chain_map={"A": [["a1"], ["a2"]]})
        ladder.run([part("A", "a1", "a2")])
        self.assertEqual([s for s, _ in ladder.steps], ["together", "loop", "loop"])

    def test_loop_rung_skipped_with_one_chain(self):
        ladder = Ladder(lambda scope, names: scope == "together")
        ladder.run([part("A", "a1", "a2")])
        self.assertEqual(ladder.steps, [("together", ["a1", "a2"]), ("edge", ["a1"]), ("edge", ["a2"])])

    def test_one_edge_refused_is_not_asked_again(self):
        ladder = Ladder(lambda scope, names: True)
        ladder.run([part("A", "a1")])
        self.assertEqual(ladder.steps, [("together", ["a1"])])
        self.assertEqual(ladder.gave_up, [("together", ["a1"])])

    def test_a_chain_builds_and_its_edges_are_never_tried(self):
        ladder = Ladder(lambda scope, names: scope == "together", chain_map={"A": [["a1", "a2"], ["a3"]]})
        ladder.run([part("A", "a1", "a2", "a3")])
        self.assertNotIn("edge", [s for s, _ in ladder.steps])

    def test_only_a_refused_chain_is_split(self):
        ladder = Ladder(lambda scope, names: scope == "together" or names == ["a3", "a4"],
                        chain_map={"A": [["a1", "a2"], ["a3", "a4"]]})
        ladder.run([part("A", "a1", "a2", "a3", "a4")])
        self.assertEqual(ladder.steps[1:], [("loop", ["a1", "a2"]), ("loop", ["a3", "a4"]),
                                            ("edge", ["a3"]), ("edge", ["a4"])])

    def test_a_tangent_chain_is_not_split_into_edges(self):
        ladder = Ladder(lambda scope, names: True)
        ladder.run([part("A", "a1", "a2", tangent=True)])
        self.assertEqual(ladder.steps, [("together", ["a1", "a2"])])
        self.assertEqual(ladder.gave_up, [("together", ["a1", "a2"])])

    def test_groups_with_nothing_pending_are_skipped(self):
        first, second = part("A", "a1"), part("B", "b1")
        second["anchors"][0]["state"] = "lost"
        ladder = Ladder(lambda scope, names: scope == "together")
        ladder.run([first, second])
        self.assertEqual([s for s, _ in ladder.steps], ["together", "set"])

    def test_shown_scope_names_a_single_edge(self):
        self.assertEqual(et.shown_scope("loop", [part("A", "a1")]), "edge")
        self.assertEqual(et.shown_scope("loop", [part("A", "a1", "a2")]), "loop")


# -- Whole runs ------------------------------------------------------------------


class TestBuild(unittest.TestCase):
    def test_equal_chamfer_on_four_edges(self):
        world = World()
        payload = run(world)
        self.assertEqual(payload["status"], "built")
        self.assertTrue(payload["ok"])
        self.assertEqual(scopes(payload), [("together", 4, "built")])
        self.assertEqual(payload["edges"]["requested"], 4)
        self.assertEqual(payload["edges"]["built"], 4)
        self.assertEqual(payload["measured"][0]["verdict"], "ok")
        self.assertEqual(payload["measured"][0]["found"], [0.1, 0.1])
        self.assertEqual(len(payload["features"]), 1)
        self.assertNotIn("feature", payload)

    def test_measures_the_whole_design_and_stores_a_baseline(self):
        with mock.patch.object(et.fo, "store_baseline", return_value="kept") as store:
            payload = run(World())
        store.assert_called_once()
        self.assertEqual(payload["baseline_token"], "kept")
        self.assertEqual(payload["bodies"]["touched"][0]["role"], "target")

    def test_by_token(self):
        world = World()
        refs = [{"token": "edge-south", "kind": "edge", "point": MIDPOINTS["south"]}]
        payload = run(world, sets=[{"edges": refs, "size": 0.1}])
        self.assertEqual(payload["status"], "built")
        self.assertEqual(payload["resolved"], [{"role": "edge", "kind": "edge", "resolved_by": "token"}])
        self.assertEqual(world.adds, [["south"]])

    def test_points_and_edges_are_counted_apart(self):
        world = World()
        points = [MIDPOINTS["south"], [0.5, 0, TOP_Z], [1.5, 0, TOP_Z]]
        payload = run(world, sets=[{"points": points, "body": BLOCK, "size": 0.1}])
        edges = payload["edges"]
        self.assertEqual((edges["requested"], edges["points_named"], edges["points_matched"], edges["searched"]),
                         (1, 3, 3, 4))

    def test_all_sets_go_to_one_feature(self):
        world = World()
        payload = run(world, sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.1},
                                   {"points": [MIDPOINTS["north"]], "body": BLOCK, "size": 0.2}])
        self.assertEqual(world.adds, [["north", "south"]])
        self.assertEqual(payload["status"], "built")

    def test_feature_name(self):
        world = World(refuse=lambda names, located: len(names) == 4)
        payload = run(world, feature_name="Top", sets=[
            {"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.1},
            {"points": [MIDPOINTS["east"], MIDPOINTS["north"], MIDPOINTS["west"]], "body": BLOCK, "size": 0.1}])
        self.assertEqual([row["name"] for row in payload["features"]], ["Top", "Top 2"])

    def test_expression_is_measured_by_its_value(self):
        world = World(sizes={"p + 1 mm": 0.1})
        payload = run(world, sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size_expression": "p + 1 mm"}])
        self.assertEqual(payload["measured"][0]["verdict"], "ok")


class TestRefusals(unittest.TestCase):
    def assert_refused(self, payload, text):
        self.assertEqual(payload["status"], "refused")
        self.assertFalse(payload["ok"])
        self.assertIn(text, " ".join(payload["refusals"]))

    def test_nothing_found(self):
        world = World()
        payload = run(world, sets=[{"points": [[9, 9, 9]], "body": BLOCK, "size": 0.1}])
        self.assert_refused(payload, "no edges resolved")
        self.assertEqual(world.adds, [])
        self.assertEqual(len(payload["edges"]["lost"]), 1)

    def test_unknown_body(self):
        other = {"occurrence_path": "root", "body_name": "Nope"}
        self.assert_refused(run(World(), sets=[{"points": [[1, 0, 1]], "body": other, "size": 0.1}]), "no body")

    def test_expression_at_or_below_zero(self):
        world = World(sizes={"p - p": 0.0})
        payload = run(world, sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size_expression": "p - p"}])
        self.assert_refused(payload, "not above zero")

    def test_expression_fusion_cannot_read(self):
        world = World()
        world.length_cm = mock.Mock(side_effect=ValueError("'x y' is not a valid expression"))
        payload = run(world, sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size_expression": "x y"}])
        self.assert_refused(payload, "not a valid expression")

    def test_point_within_tolerance_of_two_edges(self):
        payload = run(World(), sets=[{"points": [[0, 0, TOP_Z]], "body": BLOCK, "size": 0.1}])
        self.assert_refused(payload, "within tolerance of 2 edges")

    def test_an_edge_in_two_sets(self):
        payload = run(World(), sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.1},
                                     {"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.2}])
        self.assert_refused(payload, "one treatment")

    def test_size_on_face_not_adjacent(self):
        payload = run(World(), sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.1, "size_two": 0.2,
                                      "size_on_face": {"token": "face-north-side", "kind": "face"}}])
        self.assert_refused(payload, "not adjacent")

    def test_size_on_face_unresolved(self):
        payload = run(World(), sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.1, "size_two": 0.2,
                                      "size_on_face": {"token": "nothing", "kind": "face"}}])
        self.assert_refused(payload, "size_on_face")

    def test_all_problems_are_listed_together(self):
        world = World()
        world.design.designType = 0
        payload = run(world, sets=[{"points": [[9, 9, 9]], "body": BLOCK, "size": 0.1}])
        self.assertEqual(len(payload["refusals"]), 2)

    def test_preflight_stops_a_write(self):
        world = World()
        with mock.patch.object(et.fo, "preflight", return_value=(["timeline broken"], [])):
            payload = run(world)
        self.assert_refused(payload, "timeline broken")
        self.assertEqual(world.adds, [])

    def test_ambiguous_ref_is_a_refusal_not_a_loss(self):
        world = World()
        answer = Resolution(None, None, None, [], {"code": "ambiguous_split", "error": "two of them"})
        with mock.patch.object(et, "resolve", return_value=answer):
            payload = run(world, sets=[{"edges": [{"token": "t"}], "size": 0.1}])
        self.assert_refused(payload, "two of them")


class TestRetries(unittest.TestCase):
    def test_together_only_stops_at_the_first_refusal(self):
        world = World(refuse=lambda names, located: True)
        payload = run(world, strategy="together_only")
        self.assertEqual(scopes(payload), [("together", 4, "refused")])
        self.assertEqual(payload["status"], "refused")
        self.assertIn("ASM_BL_CAP_COMPLEX", payload["refusals"][0])
        self.assertEqual(len(payload["edges"]["refused"]), 4)

    def test_closed_chain_refused_goes_to_edges(self):
        world = World(refuse=lambda names, located: len(names) > 1)
        payload = run(world)
        self.assertEqual(scopes(payload), [("together", 4, "refused")] + [("edge", 1, "built")] * 4)
        self.assertEqual(payload["status"], "built")
        self.assertEqual(len(payload["features"]), 4)

    def test_partial_on_one_failed_edge_keeps_fusions_words(self):
        world = World(refuse=lambda names, located: len(names) > 1 or names == ["east"])
        payload = run(world)
        self.assertEqual(payload["status"], "partial")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["edges"]["built"], 3)
        row = payload["edges"]["refused"][0]
        self.assertEqual(row["point"], MIDPOINTS["east"])
        self.assertEqual(row["attempt"], "edge")
        self.assertIn("ASM_BL_CAP_COMPLEX", row["refusal"])

    def test_nothing_builds(self):
        world = World(refuse=lambda names, located: True)
        payload = run(world)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["features"], [])
        self.assertEqual(len(world.adds), 5)
        self.assertEqual(payload["bodies"]["touched"], [])

    def test_sets_on_one_plane_stay_together_when_another_plane_refuses(self):
        world = World()
        low = [Vertex((0, 0, 0)), Vertex((2, 0, 0))]
        floor = Edge("floor", low[0], low[1], world.body)
        side = Face("floor-side", (1, 0, 0.5), world.body)
        link(world.body, side, floor)
        world.body.edges.append(floor)
        world.body.faces.append(side)
        world.refuse = lambda names, located: len(names) == 4
        payload = run(world, sets=[{"points": [MIDPOINTS["south"], MIDPOINTS["east"]], "body": BLOCK, "size": 0.1},
                                   {"points": [[1, 0, 0]], "body": BLOCK, "size": 0.1},
                                   {"points": [MIDPOINTS["north"]], "body": BLOCK, "size": 0.2}])
        self.assertEqual(world.adds[1], ["east", "north", "south"])
        self.assertEqual(payload["attempts"][1]["scope"], "plane")

    def test_type_error_stops_the_ladder_and_keeps_what_was_built(self):
        world = World()
        real = world.add

        def add(entry, located, kind, flipped):
            names = [e.name for item in located for e in item["edges"]]
            if len(names) > 1:
                raise RuntimeError("ASM_BL_CAP_COMPLEX")
            if names == ["east"]:
                raise TypeError("missing 1 required positional argument")
            return real(entry, located, kind, flipped)

        world.add = add
        payload = run(world)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["edges"]["built"], 1)
        self.assertEqual([row["outcome"] for row in payload["attempts"]], ["refused", "built"])
        self.assertIn("our call to Fusion is wrong", payload["warnings"][0])
        self.assertEqual(payload["edges"]["refused"][0]["attempt"], "stopped")

    def test_type_error_before_anything_built_is_refused(self):
        world = World()
        world.add = mock.Mock(side_effect=TypeError("bad"))
        payload = run(world)
        self.assertEqual(world.add.call_count, 1)
        self.assertEqual(payload["status"], "refused")
        self.assertIn("our call to Fusion is wrong", payload["refusals"][0])

    def test_a_feature_that_came_back_in_error_counts_as_refused(self):
        world = World(error="Compute Failed")
        payload = run(world, strategy="together_only")
        self.assertEqual(payload["status"], "refused")
        self.assertIn("Compute Failed", payload["refusals"][0])
        self.assertEqual(world.deletes, 1)


class TestAnchors(unittest.TestCase):
    def test_a_point_that_finds_nothing_is_lost_and_partial(self):
        world = World()
        payload = run(world, sets=[{"points": [MIDPOINTS["south"], [9, 9, 9]], "body": BLOCK, "size": 0.1}])
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["edges"]["built"], 1)
        self.assertEqual(payload["edges"]["points_matched"], 1)
        self.assertEqual(payload["edges"]["lost"][0]["point"], [9, 9, 9])

    def test_a_ref_that_finds_nothing_is_lost(self):
        world = World()
        sets = [{"edges": [{"token": "edge-south", "kind": "edge", "point": MIDPOINTS["south"]},
                           {"token": "edge-gone", "kind": "edge", "point": [8, 8, 8]}], "size": 0.1}]
        payload = run(world, sets=sets)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["edges"]["lost"][0]["point"], [8, 8, 8])

    def test_an_edge_an_earlier_step_removed_is_lost_not_refused(self):
        world = World(refuse=lambda names, located: len(names) > 1, lose_after_first=["east"])
        payload = run(world)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["edges"]["built"], 3)
        self.assertEqual(payload["edges"]["refused"], [])
        self.assertEqual(payload["edges"]["lost"], [{"point": MIDPOINTS["east"],
                                                     "why": "gone once an earlier step built"}])
        self.assertEqual(scopes(payload).count(("edge", 1, "lost")), 1)

    def test_edges_are_found_again_after_every_step(self):
        world = World(refuse=lambda names, located: len(names) > 1)
        first = world.body.edges[1]
        trimmed = Vertex((2, 0.1, TOP_Z))
        real = world._treat

        def treat(edge, on_top, on_side):
            real(edge, on_top, on_side)
            first.start = trimmed  # the neighbor loses its end, and still contains its anchor

        world._treat = treat
        payload = run(world)
        self.assertEqual(payload["edges"]["built"], 4)
        self.assertGreater(world.refreshes, 4)


class TestTwoDistances(unittest.TestCase):
    sets = [{"edges": [{"token": "edge-south", "kind": "edge", "point": MIDPOINTS["south"]}], "size": 0.1,
             "size_two": 0.2, "size_on_face": {"token": "face-top", "kind": "face"}}]

    def run_two(self, world, sets=None, **overrides):
        return run(world, sets=sets or self.sets, **overrides)

    def test_ok_unflipped(self):
        world = World()
        payload = self.run_two(world)
        self.assertEqual(payload["status"], "built")
        row = payload["measured"][0]
        self.assertEqual((row["verdict"], row["flipped"], row["expected"], row["found"]),
                         ("ok", False, [0.1, 0.2], [0.1, 0.2]))
        self.assertEqual(world.flips, [[]])

    def test_swapped_is_rebuilt_flipped(self):
        world = World(swap=True)
        payload = self.run_two(world)
        self.assertEqual(payload["status"], "built")
        row = payload["measured"][0]
        self.assertEqual((row["verdict"], row["flipped"]), ("flipped_fixed", True))
        self.assertEqual(world.flips, [[], [0]])
        self.assertEqual(world.deletes, 1)
        self.assertEqual(len(payload["features"]), 1)

    def test_swapped_twice_is_deleted_and_refused(self):
        world = World(swap="always")
        payload = self.run_two(world)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(world.deletes, 2)
        self.assertEqual(world.body.volume, 4.0)
        self.assertIn("swapped both ways", payload["refusals"][0])
        self.assertEqual(payload["measured"], [])

    def test_swapped_twice_goes_down_the_ladder(self):
        world = World(swap="always")
        loop = [{"edges": [{"token": f"edge-{name}", "kind": "edge", "point": MIDPOINTS[name]}
                           for name in ("south", "east")],
                 "size": 0.1, "size_two": 0.2, "size_on_face": {"token": "face-top", "kind": "face"}}]
        payload = self.run_two(world, sets=loop)
        self.assertEqual([row["scope"] for row in payload["attempts"]], ["together", "edge", "edge"])

    def test_within_two_percent_is_not_measured(self):
        sets = [{**self.sets[0], "size_two": 0.1005}]
        payload = self.run_two(World(), sets=sets)
        self.assertEqual(payload["measured"][0]["verdict"], "not_measured")
        self.assertEqual(payload["status"], "built")

    def test_neither_distance_is_suspect(self):
        world = World()
        real = world._treat
        world._treat = lambda edge, top, side: real(edge, 0.15, side)
        payload = self.run_two(world)
        self.assertEqual(payload["status"], "suspect")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["measured"][0]["verdict"], "mismatch")
        self.assertEqual(world.deletes, 0)

    def test_a_neighbor_at_size_two_is_not_called_a_swap(self):
        world = World(swap=True)
        neighbor = {"points": [MIDPOINTS["north"]], "body": BLOCK, "size": 0.2}
        payload = self.run_two(world, sets=[self.sets[0], neighbor])
        self.assertEqual(payload["measured"][0]["verdict"], "not_measured")
        self.assertEqual(world.deletes, 0)

    def test_measured_on_the_side_face_too(self):
        sets = [{**self.sets[0], "size_on_face": {"token": "face-south-side", "kind": "face"}}]
        world = World()
        real = world._treat
        world._treat = lambda edge, top, side: real(edge, side, top)
        payload = self.run_two(world, sets=sets)
        self.assertEqual(payload["measured"][0]["verdict"], "ok")


class TestEqualChecks(unittest.TestCase):
    def test_wrong_size_on_one_face_is_suspect(self):
        world = World()
        real = world._treat
        world._treat = lambda edge, top, side: real(edge, top, 0.3)
        payload = run(world)
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(payload["measured"][0]["verdict"], "mismatch")
        self.assertIn("expected", payload["warnings"][0])

    def test_built_body_that_did_not_change_is_suspect(self):
        world = World()
        real = world.add

        def add(*args):
            saved = (world.body.volume, len(world.body.faces))
            feature = real(*args)
            world.body.volume = saved[0]
            return feature

        world.add = add
        payload = run(world)
        self.assertEqual(payload["status"], "suspect")
        self.assertIn("did not change", " ".join(payload["warnings"]))

    def test_unreadable_faces_are_not_measured(self):
        world = World()
        real = world.add

        def add(*args):
            feature = real(*args)
            for face in world.body.faces:
                face.entityToken = "gone"
                face.center = (50, 50, 50)
            return feature

        world.add = add
        payload = run(world)
        self.assertEqual(payload["measured"][0]["verdict"], "not_measured")
        self.assertEqual(payload["status"], "built")


class TestFillet(unittest.TestCase):
    def fillet(self, world, size=0.1, **overrides):
        return run(world, kind="fillet", sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": size}],
                   **overrides)

    def test_cylinder_of_the_radius(self):
        payload = self.fillet(World(radii=[(1, {"radius": 0.1})]))
        self.assertEqual(payload["measured"][0], {"set": 0, "method": "radius", "expected": [0.1], "found": [0.1],
                                                  "flipped": False, "verdict": "ok"})
        self.assertEqual(payload["status"], "built")

    def test_torus_on_a_curved_edge(self):
        payload = self.fillet(World(radii=[(4, {"majorRadius": 2.0, "minorRadius": 0.1})]))
        self.assertEqual(payload["measured"][0]["verdict"], "ok")

    def test_wrong_radius_is_suspect(self):
        payload = self.fillet(World(radii=[(1, {"radius": 0.3})]))
        self.assertEqual(payload["status"], "suspect")

    def test_a_spline_fillet_is_not_measured(self):
        payload = self.fillet(World(radii=[(7, {})]))
        self.assertEqual(payload["measured"][0]["verdict"], "not_measured")
        self.assertEqual(payload["status"], "built")


class TestDryRun(unittest.TestCase):
    def test_plans_and_builds_nothing(self):
        world = World()
        payload = run(world, dry_run=True)
        self.assertEqual(payload["status"], "planned")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(world.adds, [])
        self.assertEqual(payload["features"], [])

    def test_lists_edges_faces_and_chains(self):
        plan = run(World(), dry_run=True)["plan"]
        self.assertEqual(plan["features"], 1)
        entry = plan["sets"][0]
        self.assertEqual(entry["body"], BLOCK)
        self.assertEqual(len(entry["edges"]), 4)
        self.assertEqual(len(entry["edges"][0]["faces"]), 2)
        self.assertEqual(entry["chains"], [{"edges": [0, 1, 2, 3], "closed": True}])
        self.assertAlmostEqual(entry["edges"][0]["length"], 2.0)

    def test_refusals_run_too(self):
        payload = run(World(), dry_run=True, sets=[{"points": [[9, 9, 9]], "body": BLOCK, "size": 0.1}])
        self.assertEqual(payload["status"], "refused")
        self.assertTrue(payload["dry_run"])

    def test_suspects_name_a_short_neighbor(self):
        world = World()
        south = world.body.edges[0]
        tiny = Edge("tiny", south.end, Vertex((2, 0, TOP_Z - 0.004)), world.body)
        world.body.edges.append(tiny)
        payload = run(world, dry_run=True, sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.06}])
        self.assertEqual(payload["suspects"], [{"point": MIDPOINTS["south"],
                                                "why": "neighbor edge 0.004 long is shorter than size 0.06"}])

    def test_suspects_use_the_larger_distance(self):
        sets = [{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.1, "size_two": 3.0,
                 "size_on_face": {"token": "face-top", "kind": "face"}}]
        why = [row["why"] for row in run(World(), dry_run=True, sets=sets)["suspects"]]
        self.assertIn("edge 2.0 long is shorter than size 3.0", why)

    def test_suspects_name_a_high_face_count_per_part(self):
        world = World()
        world.body.faces.extend(Face(f"extra{i}", (1, 1, 0), world.body) for i in range(et.HIGH_FACE_COUNT))
        payload = run(world, dry_run=True, sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.1},
                                                 {"points": [MIDPOINTS["north"]], "body": BLOCK, "size": 0.1}])
        rows = [row for row in payload["suspects"] if "layered unions" in row["why"]]
        self.assertEqual([row["point"] for row in rows], [MIDPOINTS["south"], MIDPOINTS["north"]])

    def test_a_refused_build_still_reports_suspects(self):
        world = World(refuse=lambda names, located: True)
        south = world.body.edges[0]
        world.body.edges.append(Edge("tiny", south.end, Vertex((2, 0, TOP_Z - 0.004)), world.body))
        payload = run(world, sets=[{"points": [MIDPOINTS["south"]], "body": BLOCK, "size": 0.06}])
        self.assertEqual(len(payload["suspects"]), 1)


class TestHandler(unittest.TestCase):
    def test_bad_arguments_never_reach_fusion(self):
        with mock.patch.object(et, "get_app") as get_app:
            result = et.edge_treatment({"kind": "bevel", "sets": []})
        get_app.assert_not_called()
        self.assertTrue(result["isError"])

    def test_no_design_is_refused(self):
        with mock.patch.object(et, "get_app"), mock.patch.object(et.adsk.fusion, "Design", create=True) as design:
            design.cast.return_value = None
            result = et.edge_treatment({"kind": "chamfer", "sets": [{"points": [[0, 0, 0]], "body": BLOCK, "size": 1}]})
        self.assertTrue(result["isError"])
        self.assertIn("not a Fusion design", result["content"][0]["text"])


class Collection(list):
    add = list.append


class FakeFeatures:
    def __init__(self, added=True):
        self.calls, self.added = [], added
        self.chamferEdgeSets = self
        self.edgeSetInputs = self

    def createInput2(self):
        return self

    createInput = createInput2

    def addEqualDistanceChamferEdgeSet(self, *args):
        self.calls.append(("equal", args))
        return self.added

    def addTwoDistancesChamferEdgeSet(self, *args):
        self.calls.append(("two", args))
        return self.added

    def addConstantRadiusEdgeSet(self, *args):
        self.calls.append(("radius", args))
        return self if self.added else None

    def add(self, entity):
        return "feature"


class Unit:
    def isValidExpression(self, text, kind):
        return text != "nonsense"

    def evaluateExpression(self, text, kind):
        return 0.5


class TestFusionLayer(unittest.TestCase):
    def setUp(self):
        self.features = FakeFeatures()
        owner = type("Features", (), {"chamferFeatures": self.features, "filletFeatures": self.features})
        body = type("B", (), {"parentComponent": type("C", (), {"features": owner})})()
        self.entry = {"occurrence_path": "root", "_body": body}
        self.session = et.FusionEdges.__new__(et.FusionEdges)  # FusionScene needs a live adsk.fusion
        self.session._units = Unit()
        collection = mock.patch.object(et.adsk.core.ObjectCollection, "create", side_effect=Collection)
        collection.start()
        self.addCleanup(collection.stop)

    def job(self, index=0, two=False, tangent=False):
        return {"index": index, "tangent": tangent, "size": {"text": "0.1 in", "number": 0.1},
                "size_two": {"text": "0.2 in", "number": 0.2} if two else None}

    def located(self, **kwargs):
        return [{"set": self.job(**kwargs), "edges": ["edge"]}]

    def test_equal_takes_three_arguments(self):
        self.assertEqual(self.session.add(self.entry, self.located(tangent=True), "chamfer", set()), "feature")
        kind, args = self.features.calls[0]
        self.assertEqual((kind, len(args), args[2]), ("equal", 3, True))

    def test_two_distances_take_five_with_the_flip_fourth(self):
        self.session.add(self.entry, self.located(two=True), "chamfer", {0})
        kind, args = self.features.calls[0]
        self.assertEqual((kind, len(args), args[3], args[4]), ("two", 5, True, False))

    def test_fillet(self):
        self.session.add(self.entry, self.located(tangent=True), "fillet", set())
        kind, args = self.features.calls[0]
        self.assertEqual((kind, len(args), args[2]), ("radius", 3, True))

    def test_an_edge_set_fusion_will_not_take_is_a_refusal(self):
        self.features.added = False
        for kind in ("chamfer", "fillet"):
            with self.assertRaises(RuntimeError):
                self.session.add(self.entry, self.located(), kind, set())

    def test_a_sub_component_takes_native_edges(self):
        edge = type("E", (), {"nativeObject": "native"})()
        self.assertEqual(et._collection({"occurrence_path": "Sub:1"}, [edge]), ["native"])
        self.assertEqual(et._collection({"occurrence_path": "root"}, [edge]), [edge])

    def test_length_of_a_number_and_an_expression(self):
        self.assertAlmostEqual(self.session.length_cm({"text": "1 in", "number": 1.0}, "in"), 2.54)
        self.assertEqual(self.session.length_cm({"text": "p", "number": None}, "in"), 0.5)

    def test_expression_fusion_cannot_read_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.session.length_cm({"text": "nonsense", "number": None}, "in")


if __name__ == "__main__":
    unittest.main()
