"""pattern_features: schema, validation, positions, the inputs Fusion is given, and
whole runs against a fake design (no Fusion needed)."""

import json
import math
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import pattern_features as pf
from fusion_bridge import tool_surface

CUT, JOIN, NEW_BODY = 1, 0, 3
LINE, CIRCLE, SPLINE = 0, 2, 6
PLANE, CYLINDER = 0, 1
X, Y, Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
ORIGIN = (0.0, 0.0, 0.0)


def close(test, got, wanted, places=6):
    test.assertEqual(len(got), len(wanted))
    for a, b in zip(got, wanted):
        for x, y in zip(a, b):
            test.assertAlmostEqual(x, y, places=places)


def rectangular(**overrides):
    return {"type": "rectangular", "entities": [{"feature": "Hole"}], "direction_one": {"axis": "x"}, "count_one": 3,
            "spacing_one": 2.0, "units": "cm", **overrides}


def circular(**overrides):
    return {"type": "circular", "entities": [{"feature": "Hole"}], "axis": {"axis": "z"}, "count": 6, "units": "cm",
            **overrides}


def options(arguments):
    opts, err = pf.validate_args(arguments)
    assert err is None, err
    return opts


# -- Fakes -----------------------------------------------------------------------


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Box:
    def __init__(self, low, high):
        self.minPoint, self.maxPoint = P(*low), P(*high)


class Members(list):
    count = property(len)

    def item(self, index):
        return self[index]


class Component:
    def __init__(self, name):
        self.name = name
        self.xConstructionAxis, self.yConstructionAxis, self.zConstructionAxis = (f"{name}.{a}" for a in "xyz")
        self.features = None


class Body:
    isSolid = True
    isVisible = True
    nativeObject = None
    assemblyContext = None

    def __init__(self, name, volume, component, box=((0, 0, 0), (10, 10, 10))):
        self.name, self.volume, self.parentComponent = name, volume, component
        self.boundingBox = Box(*box)
        self.faces, self.lumps, self.shells, self.edges = Members(), Members([1]), Members([1]), Members()


class Occurrence:
    isReferencedComponent = False
    isVisible = True

    def __init__(self, path, component, bodies, frame):
        self.fullPathName, self.component, self.bRepBodies = path, component, bodies
        self.transform2 = type("M", (), {"getAsCoordinateSystem": lambda s: tuple(P(*part) for part in frame)})()


class Item:
    isGroup = False
    healthState = 0
    errorOrWarningMessage = ""

    def __init__(self, entity, name, index):
        self.entity, self.name, self.index = entity, name, index


class Timeline:
    def __init__(self):
        self.objects = []

    def __iter__(self):
        return iter(self.objects)

    count = property(lambda self: len(self.objects))
    markerPosition = property(lambda self: len(self.objects))


class Seed:
    objectType = "adsk::fusion::ExtrudeFeature"
    participantBodies = ()

    def __init__(self, name, component, bodies, operation=CUT, token=None):
        self.name, self.parentComponent, self.bodies, self.operation = name, component, bodies, operation
        self.entityToken = token


class Sketchish:
    objectType = "adsk::fusion::Sketch"
    name = "Sketch1"


class Element:
    def __init__(self, shift):
        self.transform = type("M", (), {"asArray": lambda s: [1, 0, 0, shift[0], 0, 1, 0, shift[1],
                                                              0, 0, 1, shift[2], 0, 0, 0, 1]})()


class Input:
    """Records what the logic set, in order."""

    def __init__(self, kind, args):
        self.kind, self.args, self.calls = kind, args, []
        self.directionOne, self.directionTwo = None, None
        self.two_answer = True

    def setDirectionTwo(self, *args):
        self.calls.append(("setDirectionTwo", args))
        return self.two_answer


class PatternFeature:
    objectType = "adsk::fusion::RectangularPatternFeature"
    healthState = 0
    errorOrWarningMessage = ""
    timelineObject = None
    name = "RectangularPattern1"

    def __init__(self, world, component, elements):
        self.world, self.parentComponent = world, component
        self.patternElements = Members(elements)

    def deleteMe(self):
        self.world.restore()
        return True


class Patterns:
    def __init__(self, world, kind):
        self.world, self.kind = world, kind

    def createInput(self, *args):
        if self.world.input_refused:
            raise RuntimeError(self.world.input_refused)
        made = Input(self.kind, args)
        made.directionOne = self.world.direction_one
        made.two_answer = self.world.two_answer
        return made

    def add(self, entity):
        return self.world.add(entity)


class Features:
    def __init__(self, world):
        self.rectangularPatternFeatures = Patterns(world, "rectangular")
        self.circularPatternFeatures = Patterns(world, "circular")


class Design:
    designType = 1
    activeEditObject = None
    parentDocument = None

    def __init__(self, root, timeline):
        self.rootComponent, self.timeline = root, timeline


class Root(Component):
    def __init__(self):
        super().__init__("root")
        self.bRepBodies, self.allOccurrences = [], []


class World:
    """The collaborator ``run`` drives: a root with Block and Other, and a seed cut named Hole.

    ``effect`` runs on add and returns the elements; the default takes the asked
    number of instances, each removing ``cut`` from Block.
    """

    def __init__(self):
        self.root = Root()
        self.root.features = Features(self)
        self.block = Body("Block", 100.0, self.root)
        self.other = Body("Other", 50.0, self.root, ((20, 0, 0), (30, 10, 10)))
        self.root.bRepBodies += [self.block, self.other]
        self.timeline = Timeline()
        self.design = Design(self.root, self.timeline)
        self.seed = self.add_seed("Hole", self.root, [self.block])
        self.tokens, self.resolved = {}, {}
        self.direction_one, self.two_answer, self.input_refused, self.add_refused = None, True, None, None
        self.effect = None
        self.cut = 1.0
        self.spacing = None
        self.added = []
        self.saved = None

    def add_seed(self, name, component, bodies, operation=CUT):
        seed = Seed(name, component, bodies, operation)
        self.timeline.objects.append(Item(seed, name, len(self.timeline.objects)))
        return seed

    # Session surface.
    def entries(self):
        from fusion_bridge.common import bodies
        return bodies.assign_keys(list(bodies.body_entries(self.root)))

    def by_token(self, token):
        return self.tokens.get(token, [])

    def distance(self, entity, point):
        return 0.0

    def at_point(self, point, kind, tolerance_cm=None):
        return []

    def collection(self, members):
        return list(members)

    def real(self, number):
        return ("real", number)

    def value(self, value, kind="cm"):
        return ("value", value["text"], kind)

    def evaluate(self, value, kind):
        return math.radians(90.0) if kind == "deg" else 4.0

    def length_cm(self, value, units):
        return value["number"] if value["number"] is not None else self.evaluate(value, "cm")

    def spacing_type(self):
        return "spacing"

    def compute_option(self, name):
        return f"compute:{name}"

    def construction_axis(self, component, name):
        return getattr(component, f"{name}ConstructionAxis")

    # What the add does.
    def add(self, entity):
        if self.add_refused:
            raise RuntimeError(self.add_refused)
        self.added.append(entity)
        self.saved = [(body, body.volume) for body in self.root.bRepBodies], list(self.root.bRepBodies)
        elements = self.effect(entity) if self.effect else self.default_effect(entity)
        return PatternFeature(self, self.root, elements)

    def default_effect(self, entity):
        count = entity.args[2][1] if entity.kind == "rectangular" else entity.quantity[1]
        self.block.volume -= self.cut * (count - 1)
        return [Element((0, 0, 0)) for _ in range(count)]

    def restore(self):
        volumes, members = self.saved
        self.root.bRepBodies[:] = members
        for body, volume in volumes:
            body.volume = volume


def go(world, arguments):
    return pf.run(world.design, None, options(arguments), world)


class Edge:
    nativeObject = None
    assemblyContext = None
    objectType = "adsk::fusion::BRepEdge"

    def __init__(self, body, start=(0, 0, 0), end=(1, 0, 0), curve=LINE):
        self.body = body
        self.geometry = type("G", (), {"curveType": curve, "center": P(1, 2, 3), "normal": P(0, 0, 1)})()
        self.startVertex = type("V", (), {"geometry": P(*start)})()
        self.endVertex = type("V", (), {"geometry": P(*end)})()


class Face:
    nativeObject = None
    assemblyContext = None
    objectType = "adsk::fusion::BRepFace"

    def __init__(self, body, surface=CYLINDER):
        self.body = body
        self.geometry = type("G", (), {"surfaceType": surface, "origin": P(1, 2, 0), "axis": P(0, 0, 1)})()


def ref(token, kind):
    return {"token": token, "kind": kind}


# -- Pure logic ------------------------------------------------------------------


class PositionTests(unittest.TestCase):
    def test_one_direction(self):
        close(self, pf.rectangular_positions((1, 0, 0), X, 3, 2.0), [(1, 0, 0), (3, 0, 0), (5, 0, 0)])

    def test_grid(self):
        close(self, pf.rectangular_positions(ORIGIN, X, 2, 1.0, Y, 2, 3.0),
              [(0, 0, 0), (1, 0, 0), (0, 3, 0), (1, 3, 0)])

    def test_negative_spacing_reverses(self):
        close(self, pf.rectangular_positions(ORIGIN, X, 3, -2.0), [(0, 0, 0), (-2, 0, 0), (-4, 0, 0)])

    def test_one_instance(self):
        close(self, pf.rectangular_positions((1, 2, 3), X, 1, 5.0), [(1, 2, 3)])

    def test_full_turn_divides_by_n(self):
        found = pf.circular_positions((1, 0, 0), ORIGIN, Z, 6, 360)
        half = math.sqrt(3) / 2
        close(self, found, [(1, 0, 0), (.5, half, 0), (-.5, half, 0), (-1, 0, 0), (-.5, -half, 0), (.5, -half, 0)])

    def test_partial_divides_by_n_minus_one(self):
        self.assertEqual(pf.circular_angles(4, 90), [0.0, 30.0, 60.0, 90.0])
        close(self, pf.circular_positions((1, 0, 0), ORIGIN, Z, 2, 90), [(1, 0, 0), (0, 1, 0)])

    def test_negative_total_runs_the_other_way(self):
        close(self, pf.circular_positions((1, 0, 0), ORIGIN, Z, 2, -90), [(1, 0, 0), (0, -1, 0)])

    def test_a_negative_full_turn_is_a_full_turn(self):
        self.assertEqual(pf.circular_angles(4, -360), [0.0, -90.0, -180.0, -270.0])

    def test_symmetric_centers_a_partial_total(self):
        self.assertEqual(pf.circular_angles(3, 90, symmetric=True), [-45.0, 0.0, 45.0])

    def test_one_instance_has_no_step(self):
        self.assertEqual(pf.circular_angles(1, 90), [0.0])

    def test_axis_off_the_origin(self):
        close(self, pf.circular_positions((3, 0, 0), (2, 0, 0), Z, 2, 360), [(3, 0, 0), (1, 0, 0)])


class UnusedAxisTests(unittest.TestCase):
    def test_never_the_direction_itself(self):
        self.assertEqual(pf.unused_axis(X), "y")
        self.assertEqual(pf.unused_axis(Y), "x")
        self.assertEqual(pf.unused_axis(Z), "x")

    def test_either_sense(self):
        self.assertEqual(pf.unused_axis((0, 0, -3)), "x")

    def test_oblique_takes_the_furthest(self):
        self.assertEqual(pf.unused_axis((1, 1, 0.1)), "z")
        self.assertEqual(pf.unused_axis((0.1, 1, 1)), "x")
        self.assertEqual(pf.unused_axis((1, 0.1, 1)), "y")


class CountTests(unittest.TestCase):
    def test_instances_reached(self):
        self.assertEqual(pf.instances_reached(-1.0, -0.2), 6)
        self.assertIsInstance(pf.instances_reached(-1.0, -0.2), int)

    def test_rounding(self):
        self.assertEqual(pf.instances_reached(-1.00001, -0.2), 6)
        self.assertEqual(pf.instances_reached(-0.9, -0.2), 5.5)

    def test_null_without_a_delta(self):
        self.assertIsNone(pf.instances_reached(-1.0, None))
        self.assertIsNone(pf.instances_reached(None, -0.2))

    def test_clean(self):
        self.assertEqual(pf.count_problems(3, 3, 3, 0, None), [])
        self.assertEqual(pf.count_problems(3, 3, None, 2, 2), [])

    def test_build_six_for_two(self):
        self.assertIn("6 elements", pf.count_problems(2, 6, None, 5, 1)[0])
        self.assertIn("made 5", pf.count_problems(2, 6, None, 5, 1)[1])

    def test_a_short_pattern_by_volume(self):
        self.assertIn("2 reached", pf.count_problems(3, 3, 2, 0, None)[0])

    def test_tolerance(self):
        self.assertEqual(pf.count_problems(3, 3, 2.96, 0, None), [])
        self.assertTrue(pf.count_problems(3, 3, 2.9, 0, None))

    def test_no_element_count_proves_nothing(self):
        self.assertIn("no element count", pf.count_problems(3, None, None, 0, None)[0])


class HelperTests(unittest.TestCase):
    def test_parallel(self):
        self.assertTrue(pf.parallel(X, (-2, 0, 0)))
        self.assertFalse(pf.parallel(X, Y))
        self.assertFalse(pf.parallel(X, ORIGIN))

    def test_transformed(self):
        turn = [0, -1, 0, 5, 1, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 1]
        self.assertEqual(pf.transformed((1, 0, 0), turn), (5, 1, 2))

    def test_same_positions_in_any_order(self):
        self.assertTrue(pf.same_positions([(0, 0, 0), (1, 0, 0)], [(1, 0, 0), (0, 0, 0)]))
        self.assertFalse(pf.same_positions([(0, 0, 0), (1, 0, 0)], [(0, 0, 0)]))
        self.assertFalse(pf.same_positions([(0, 0, 0)], [(1, 0, 0)]))

    def test_outside_boxes(self):
        boxes = [((0, 0, 0), (1, 1, 1)), None, ((5, 0, 0), (6, 1, 1))]
        self.assertEqual(pf.outside_boxes([(.5, .5, .5), (5.5, .5, .5), (3, 0, 0)], boxes), [(3, 0, 0)])

    def test_no_box_warns_of_nothing(self):
        self.assertEqual(pf.outside_boxes([(3, 0, 0)], [None]), [])

    def test_occurrence_volume_counts_one_instance(self):
        records = {"a": {"volume": 1.0, "occurrence_path": "S:1"}, "b": {"volume": 2.0, "occurrence_path": "S:2"},
                   "c": {"volume": None, "occurrence_path": "S:1"}}
        self.assertEqual(pf.occurrence_volume(records, "S:1"), 1.0)

    def test_is_feature(self):
        self.assertTrue(pf.is_feature(Seed("a", None, [])))
        self.assertFalse(pf.is_feature(Sketchish()))
        self.assertFalse(pf.is_feature(None))

    def test_edge_axis(self):
        edge = Edge(None, (1, 0, 0), (1, 0, 2))
        self.assertEqual(pf.edge_axis(edge), ((1, 0, 0), (0, 0, 2)))

    def test_a_circle_is_an_axis_only_for_rotation(self):
        edge = Edge(None, curve=CIRCLE)
        self.assertIsNone(pf.edge_axis(edge))
        self.assertEqual(pf.edge_axis(edge, round_ok=True), ((1, 2, 3), (0, 0, 1)))

    def test_a_spline_is_never_an_axis(self):
        self.assertIsNone(pf.edge_axis(Edge(None, curve=SPLINE), round_ok=True))

    def test_face_axis(self):
        self.assertEqual(pf.face_axis(Face(None)), ((1, 2, 0), (0, 0, 1)))
        self.assertIsNone(pf.face_axis(Face(None, PLANE)))


# -- Arguments -------------------------------------------------------------------


class ArgumentTests(unittest.TestCase):
    def refused(self, arguments, needle):
        opts, err = pf.validate_args(arguments)
        self.assertIsNone(opts)
        self.assertIn(needle, err)

    def test_defaults(self):
        opts = options(rectangular())
        self.assertEqual((opts["compute"], opts["on_collateral"], opts["dry_run"]), ("adjust", "undo", False))
        self.assertEqual(options(circular())["total_angle"]["number"], 360.0)

    def test_dimensions_become_text_with_units(self):
        self.assertEqual(options(rectangular(units="in", spacing_one=-1.5))["spacing_one"]["text"], "-1.5 in")
        self.assertEqual(options(circular(total_angle_deg=90))["total_angle"]["text"], "90 deg")

    def test_expression(self):
        opts = options(rectangular(spacing_one=None, spacing_one_expression="pitch * 2"))
        self.assertEqual(opts["spacing_one"], {"text": "pitch * 2", "number": None})

    def test_a_bare_expression_is_refused(self):
        self.refused(rectangular(spacing_one=None, spacing_one_expression="2"), "no unit")

    def test_both_keys_of_a_dimension(self):
        self.refused(rectangular(spacing_one_expression="1 in"), "not both")

    def test_type(self):
        self.refused({"entities": []}, "'type'")
        self.refused(rectangular(type="linear"), "'type'")

    def test_unknown_key(self):
        self.refused(rectangular(colour=1), "colour")

    def test_keys_from_the_other_type(self):
        self.refused(rectangular(count=6), "does not take ['count']")
        self.refused(rectangular(symmetric=True), "symmetric")
        self.refused(circular(spacing_one=1), "spacing_one")
        self.refused(circular(direction_two={"axis": "y"}), "direction_two")

    def test_entities(self):
        self.refused(rectangular(entities=[]), "'entities'")
        self.refused(rectangular(entities="Hole"), "'entities'")
        self.refused(rectangular(entities=[{"sketch": "a"}]), "exactly one")
        self.refused(rectangular(entities=[{"feature": "a", "feature_token": "b"}]), "exactly one")
        self.refused(rectangular(entities=[{"feature": ""}]), "non-empty")

    def test_mixed_kinds(self):
        body = {"occurrence_path": "root", "body_name": "Block"}
        self.refused(rectangular(entities=[{"feature": "Hole"}, {"body": body}]), "one kind")

    def test_token_and_name_are_one_kind(self):
        options(rectangular(entities=[{"feature": "Hole"}, {"feature_token": "t"}]))

    def test_counts(self):
        for value in (0, -1, 1.5, True, None, "3"):
            self.refused(rectangular(count_one=value), "'count_one'")
            self.refused(circular(count=value), "'count'")
        self.refused(rectangular(count_one=pf.MAX_COUNT + 1), "'count_one'")

    def test_required(self):
        arguments = rectangular()
        del arguments["count_one"]
        self.refused(arguments, "'count_one'")
        self.refused({k: v for k, v in rectangular().items() if k != "direction_one"}, "'direction_one'")
        self.refused({k: v for k, v in rectangular().items() if k != "spacing_one"}, "spacing_one")
        self.refused({k: v for k, v in circular().items() if k != "axis"}, "'axis'")

    def test_direction_two_comes_whole(self):
        self.refused(rectangular(count_two=2), "need 'direction_two'")
        self.refused(rectangular(spacing_two=2), "need 'direction_two'")
        self.refused(rectangular(direction_two={"axis": "y"}), "'count_two'")
        self.refused(rectangular(direction_two={"axis": "y"}, count_two=2), "spacing_two")

    def test_directions(self):
        self.refused(rectangular(direction_one={"axis": "w"}), "'direction_one.axis'")
        self.refused(rectangular(direction_one={"face": ref("t", "face")}), "exactly one")
        self.refused(rectangular(direction_one={"axis": "x", "edge": ref("t", "edge")}), "exactly one")
        self.refused(rectangular(direction_one={"edge": ref("t", "face")}), "must be a edge")
        self.refused(rectangular(direction_one={"sketch_line_token": ""}), "non-empty")

    def test_a_ref_without_a_kind_takes_the_slot_s(self):
        opts = options(rectangular(direction_one={"edge": {"token": "t"}}))
        self.assertEqual(opts["direction_one"]["edge"]["kind"], "edge")

    def test_total_angle(self):
        self.refused(circular(total_angle_deg=0), "'total_angle_deg'")
        self.refused(circular(total_angle_deg=361), "'total_angle_deg'")
        self.assertEqual(options(circular(total_angle_deg=-360))["total_angle"]["number"], -360.0)

    def test_seed_point_and_delta(self):
        self.refused(rectangular(seed_point=[1, 2]), "'seed_point'")
        self.refused(rectangular(seed_point=[1, 2, "3"]), "'seed_point'")
        self.refused(rectangular(seed_volume_delta=0), "'seed_volume_delta'")
        opts = options(rectangular(units="in", seed_point=[1, 0, 0], seed_volume_delta=-1))
        self.assertEqual(opts["seed_point"], (2.54, 0, 0))
        self.assertAlmostEqual(opts["seed_delta_cm3"], -2.54 ** 3)

    def test_choices(self):
        self.refused(rectangular(compute="fast"), "'compute'")
        self.refused(rectangular(on_collateral="ignore"), "'on_collateral'")
        self.refused(rectangular(units="ft"), "'units'")
        self.refused(rectangular(dry_run="no"), "'dry_run'")


# -- The inputs ------------------------------------------------------------------


class InputTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def input(self, arguments):
        go(self.world, arguments)
        return self.world.added[-1]

    def test_direction_two_is_set_with_no_second_direction(self):
        made = self.input(rectangular())
        self.assertEqual(made.calls, [("setDirectionTwo", ("root.y", ("real", 1), ("real", 0.0)))])

    def test_the_placeholder_is_not_direction_one(self):
        made = self.input(rectangular(direction_one={"axis": "z"}))
        self.assertEqual(made.calls[0][1][0], "root.x")

    def test_an_explicit_second_direction(self):
        made = self.input(rectangular(direction_two={"axis": "y"}, count_two=2, spacing_two=1.0))
        self.assertEqual(made.calls, [("setDirectionTwo", ("root.y", ("real", 2), ("value", "1 cm", "cm")))])

    def test_direction_one_goes_in_with_spacing_type(self):
        made = self.input(rectangular())
        self.assertEqual(made.args[1:], ("root.x", ("real", 3), ("value", "2 cm", "cm"), "spacing"))

    def test_compute_is_always_set(self):
        self.assertEqual(self.input(rectangular()).patternComputeOption, "compute:adjust")
        self.assertEqual(self.input(rectangular(compute="identical")).patternComputeOption, "compute:identical")
        self.assertEqual(self.input(circular(compute="optimized")).patternComputeOption, "compute:optimized")

    def test_symmetry_is_stated(self):
        made = self.input(rectangular())
        self.assertIs(made.isSymmetricInDirectionOne, False)
        self.assertIs(made.isSymmetricInDirectionTwo, False)

    def test_circular_states_everything(self):
        made = self.input(circular(total_angle_deg=90, count=4, symmetric=True))
        self.assertEqual(made.args[1], "root.z")
        self.assertEqual(made.quantity, ("real", 4))
        self.assertEqual(made.totalAngle, ("value", "90 deg", "deg"))
        self.assertIs(made.isSymmetric, True)

    def test_a_refused_direction_two_stops_the_add(self):
        self.world.two_answer = False
        payload = go(self.world, rectangular())
        self.assertEqual(payload["status"], "refused")
        self.assertIn("direction two", payload["refusals"][0])
        self.assertEqual(self.world.added, [])

    def test_seeds_go_in_together(self):
        boss = self.world.add_seed("Boss", self.world.root, [self.world.block], JOIN)
        made = self.input(rectangular(entities=[{"feature": "Boss"}, {"feature": "Hole"}]))
        self.assertEqual(made.args[0], [boss, self.world.seed])


# -- Whole runs ------------------------------------------------------------------


class RunTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def refused(self, arguments, needle):
        payload = go(self.world, arguments)
        self.assertEqual(payload["status"], "refused", payload)
        self.assertFalse(payload["ok"])
        self.assertIn(needle, " ".join(payload["refusals"]))
        self.assertEqual(self.world.added, [])
        return payload

    def test_a_cut_proves_its_count(self):
        payload = go(self.world, rectangular(seed_volume_delta=-1.0))
        self.assertEqual(payload["status"], "built")
        pattern = payload["pattern"]
        self.assertEqual((pattern["asked_for"], pattern["elements"], pattern["bodies_made"]), (3, 3, 0))
        self.assertEqual((pattern["volume_change"], pattern["instances_reached"]), (-2.0, 3))
        self.assertEqual(pattern["direction_two_set"], "placeholder")
        self.assertEqual(pattern["direction_world"], [[1.0, 0.0, 0.0]])
        self.assertEqual(payload["bodies"]["touched"][0]["delta"], -2.0)
        self.assertTrue(payload["baseline_token"])

    def test_a_new_body_pattern_makes_exactly_what_was_asked(self):
        def effect(entity):
            self.world.root.bRepBodies.append(Body("Copy", 5.0, self.world.root))
            return [Element((0, 0, 0)), Element((2, 0, 0))]

        self.world.add_seed("Boss", self.world.root, [self.world.other], NEW_BODY)
        self.world.effect = effect
        payload = go(self.world, rectangular(entities=[{"feature": "Boss"}], count_one=2))
        self.assertEqual(payload["status"], "built", payload)
        self.assertEqual((payload["pattern"]["elements"], payload["pattern"]["bodies_made"]), (2, 1))
        self.assertEqual(payload["bodies"]["created"][0]["ref"]["body_name"], "Copy")

    def test_six_for_two_is_suspect(self):
        def effect(entity):
            self.world.root.bRepBodies.extend(Body(f"Copy{i}", 5.0, self.world.root) for i in range(5))
            return [Element((0, 0, 0)) for _ in range(6)]

        self.world.add_seed("Boss", self.world.root, [self.world.other], NEW_BODY)
        self.world.effect = effect
        payload = go(self.world, rectangular(entities=[{"feature": "Boss"}], count_one=2))
        self.assertEqual(payload["status"], "suspect")
        self.assertFalse(payload["ok"])
        self.assertIn("6 elements", payload["warnings"][0])

    def test_an_instance_that_cut_nothing_is_suspect(self):
        def effect(entity):
            self.world.block.volume -= 1.0
            return [Element((0, 0, 0)) for _ in range(3)]

        self.world.effect = effect
        payload = go(self.world, rectangular(seed_volume_delta=-1.0))
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(payload["pattern"]["instances_reached"], 2)

    def test_collateral_is_undone(self):
        def effect(entity):
            self.world.other.volume -= 1.0
            return [Element((0, 0, 0)) for _ in range(3)]

        self.world.effect = effect
        payload = go(self.world, rectangular())
        self.assertEqual(payload["status"], "undone")
        self.assertEqual(payload["collateral"], ["root/Other"])
        self.assertEqual(self.world.other.volume, 50.0)

    def test_collateral_can_be_kept(self):
        def effect(entity):
            self.world.other.volume -= 1.0
            return [Element((0, 0, 0)) for _ in range(3)]

        self.world.effect = effect
        payload = go(self.world, rectangular(on_collateral="keep"))
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(self.world.other.volume, 49.0)

    def test_a_join_reaching_past_its_target_is_collateral(self):
        def effect(entity):
            self.world.other.volume += 1.0
            return [Element((0, 0, 0)) for _ in range(3)]

        self.world.effect = effect
        self.assertEqual(go(self.world, rectangular())["status"], "undone")

    def test_participants_are_targets(self):
        def effect(entity):
            self.world.other.volume -= 1.0
            return [Element((0, 0, 0)) for _ in range(3)]

        self.world.seed.participantBodies = [self.world.other]
        self.world.effect = effect
        self.assertEqual(go(self.world, rectangular())["status"], "built")

    def test_dry_run_adds_nothing(self):
        payload = go(self.world, rectangular(dry_run=True, seed_point=[1, 1, 1]))
        self.assertEqual((payload["status"], payload["ok"], payload["dry_run"]), ("planned", True, True))
        self.assertEqual(self.world.added, [])
        self.assertEqual(self.world.block.volume, 100.0)
        self.assertEqual(payload["positions"], [[1.0, 1.0, 1.0], [3.0, 1.0, 1.0], [5.0, 1.0, 1.0]])
        self.assertEqual(payload["positions_basis"], "predicted")
        self.assertEqual(payload["plan"]["seeds"][0], {"kind": "feature", "name": "Hole", "type": "ExtrudeFeature"})
        self.assertEqual(payload["plan"]["targets"], [{"occurrence_path": "root", "body_name": "Block"}])
        self.assertIsNone(payload["pattern"]["elements"])

    def test_dry_run_warns_of_a_position_outside_the_target(self):
        payload = go(self.world, rectangular(dry_run=True, seed_point=[9, 1, 1]))
        self.assertIn("2 of 3 positions", " ".join(payload["warnings"]))

    def test_a_new_body_may_land_anywhere(self):
        self.world.add_seed("Boss", self.world.root, [self.world.other], NEW_BODY)
        payload = go(self.world, rectangular(entities=[{"feature": "Boss"}], dry_run=True, seed_point=[50, 0, 0]))
        self.assertNotIn("positions are outside", " ".join(payload["warnings"]))

    def test_a_join_may_land_outside(self):
        self.world.add_seed("Boss", self.world.root, [self.world.block], JOIN)
        payload = go(self.world, rectangular(entities=[{"feature": "Boss"}], dry_run=True, seed_point=[50, 0, 0]))
        self.assertNotIn("positions are outside", " ".join(payload["warnings"]))

    def test_no_seed_point_no_positions(self):
        self.assertIsNone(go(self.world, rectangular())["positions"])

    def test_built_positions_come_from_the_elements(self):
        self.world.effect = lambda e: [Element((0, 0, 0)), Element((2, 0, 0)), Element((4, 0, 0))]
        payload = go(self.world, rectangular(seed_point=[1, 0, 0]))
        self.assertEqual(payload["positions_basis"], "elements")
        self.assertEqual(payload["positions"], [[1.0, 0.0, 0.0], [3.0, 0.0, 0.0], [5.0, 0.0, 0.0]])
        self.assertEqual(payload["status"], "built")

    def test_built_positions_that_disagree_are_suspect(self):
        self.world.effect = lambda e: [Element((0, 0, 0)), Element((-2, 0, 0)), Element((-4, 0, 0))]
        payload = go(self.world, rectangular(seed_point=[5, 0, 0]))
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(payload["positions"][2], [1.0, 0.0, 0.0])

    def test_a_negative_spacing_predicts_backward(self):
        payload = go(self.world, rectangular(spacing_one=-2.0, seed_point=[5, 0, 0], dry_run=True))
        self.assertEqual(payload["positions"][2], [1.0, 0.0, 0.0])

    def test_expression_spacing_is_evaluated_for_positions(self):
        arguments = rectangular(spacing_one=None, spacing_one_expression="pitch", seed_point=[0, 0, 0], dry_run=True)
        self.assertEqual(go(self.world, arguments)["positions"][1], [4.0, 0.0, 0.0])

    def test_fusion_refusing_keeps_its_words(self):
        self.world.add_refused = "No target body found to cut"
        payload = go(self.world, rectangular())
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusals"], ["No target body found to cut"])
        self.assertTrue(payload["baseline_token"])

    def test_fusion_refusing_the_input(self):
        self.world.input_refused = "bad direction"
        self.assertIn("bad direction", go(self.world, rectangular())["refusals"][0])

    def test_feature_name(self):
        built = []
        original = self.world.add

        def add(entity):
            built.append(original(entity))
            return built[-1]

        self.world.add = add
        go(self.world, rectangular(feature_name="Bolts"))
        self.assertEqual(built[0].name, "Bolts")

    def test_a_failed_post_check_still_says_the_feature_exists(self):
        with mock.patch.object(pf.fo, "body_changes", side_effect=KeyError("boom")):
            payload = go(self.world, rectangular())
        self.assertEqual(payload["status"], "suspect")
        self.assertIn("was added", payload["warnings"][-1])

    # -- Refusals ---------------------------------------------------------------

    def test_unknown_feature(self):
        self.refused(rectangular(entities=[{"feature": "Hol"}]), "Close matches")

    def test_duplicate_feature_names(self):
        self.world.add_seed("Hole", self.world.root, [self.world.block])
        self.refused(rectangular(), "2 timeline items are named 'Hole'")

    def test_a_sketch_is_not_a_seed(self):
        self.world.timeline.objects.append(Item(Sketchish(), "Sketch1", 1))
        self.refused(rectangular(entities=[{"feature": "Sketch1"}]), "not a feature")

    def test_feature_token(self):
        self.world.tokens["t"] = [self.world.seed]
        self.assertEqual(go(self.world, rectangular(entities=[{"feature_token": "t"}]))["status"], "built")

    def test_feature_token_naming_nothing(self):
        self.refused(rectangular(entities=[{"feature_token": "t"}]), "names 0 features")

    def test_a_seed_listed_twice(self):
        self.world.tokens["t"] = [self.world.seed]
        self.refused(rectangular(entities=[{"feature": "Hole"}, {"feature_token": "t"}]), "listed twice")

    def test_seeds_in_different_components(self):
        other = Component("Sub")
        self.world.add_seed("Far", other, [])
        self.refused(rectangular(entities=[{"feature": "Hole"}, {"feature": "Far"}]), "different components")

    def test_direction_two_parallel(self):
        self.refused(rectangular(direction_two={"axis": "x"}, count_two=2, spacing_two=1.0), "parallel")

    def test_direction_two_parallel_through_an_edge(self):
        self.world.tokens["e"] = [Edge(self.world.block, (0, 1, 0), (-3, 1, 0))]
        two = {"direction_two": {"edge": ref("e", "edge")}, "count_two": 2, "spacing_two": 1.0}
        self.refused(rectangular(**two), "parallel")

    def test_zero_spacing(self):
        self.refused(rectangular(spacing_one=0), "land on the seed")

    def test_zero_spacing_is_fine_with_one_instance(self):
        two = {"direction_two": {"axis": "y"}, "count_two": 3, "spacing_two": 1.0}
        self.assertNotEqual(go(self.world, rectangular(count_one=1, spacing_one=0, **two))["status"], "refused")

    def test_a_curved_direction(self):
        self.world.tokens["e"] = [Edge(self.world.block, curve=CIRCLE)]
        self.refused(rectangular(direction_one={"edge": ref("e", "edge")}), "not straight")

    def test_a_token_that_is_not_a_sketch_line(self):
        self.world.tokens["s"] = [type("Arc", (), {"objectType": "adsk::fusion::SketchArc"})()]
        self.refused(rectangular(direction_one={"sketch_line_token": "s"}), "must be straight")

    def test_a_direction_from_another_component(self):
        stranger = Body("Far", 1.0, Component("Sub"))
        self.world.tokens["e"] = [Edge(stranger)]
        self.refused(rectangular(direction_one={"edge": ref("e", "edge")}), "seeds' component")

    def test_a_ref_that_will_not_resolve(self):
        self.refused(rectangular(direction_one={"edge": ref("gone", "edge")}), "direction_one")

    def test_preflight_runs(self):
        self.world.design.activeEditObject = type("B", (), {"objectType": "adsk::fusion::BaseFeature", "name": "B"})()
        self.refused(rectangular(), "being edited")

    def test_a_circular_axis_needs_an_axis(self):
        self.world.tokens["f"] = [Face(self.world.block, PLANE)]
        self.refused(circular(axis={"face": ref("f", "face")}), "no axis")

    def test_expression_total_of_zero(self):
        self.world.evaluate = lambda value, kind: 0.0
        self.refused(circular(total_angle_deg=None, total_angle_expression="spread"), "total angle")

    # -- Directions and axes ----------------------------------------------------

    def test_edge_sense_comes_from_the_input(self):
        self.world.tokens["e"] = [Edge(self.world.block, (0, 0, 0), (3, 0, 0))]
        self.world.direction_one = P(-1, 0, 0)
        payload = go(self.world, rectangular(direction_one={"edge": ref("e", "edge")}, dry_run=True,
                                             seed_point=[5, 0, 0]))
        self.assertEqual(payload["pattern"]["direction_world"], [[-1.0, 0.0, 0.0]])
        self.assertEqual(payload["positions"][1], [3.0, 0.0, 0.0])
        self.assertNotIn("assumed", " ".join(payload["warnings"]))

    def test_edge_sense_assumed_says_so(self):
        self.world.tokens["e"] = [Edge(self.world.block, (0, 0, 0), (3, 0, 0))]
        payload = go(self.world, rectangular(direction_one={"edge": ref("e", "edge")}, dry_run=True))
        self.assertEqual(payload["pattern"]["direction_world"], [[1.0, 0.0, 0.0]])
        self.assertIn("assumed", " ".join(payload["warnings"]))
        self.assertEqual(payload["resolved"][0]["role"], "direction_one")

    def test_sketch_line_direction(self):
        line = type("L", (), {"objectType": "adsk::fusion::SketchLine", "nativeObject": None,
                              "parentSketch": type("S", (), {"parentComponent": self.world.root})(),
                              "worldGeometry": type("G", (), {"startPoint": P(0, 0, 0), "endPoint": P(0, 4, 0)})()})()
        self.world.tokens["s"] = [line]
        payload = go(self.world, rectangular(direction_one={"sketch_line_token": "s"}))
        self.assertEqual(payload["pattern"]["direction_world"], [[0.0, 1.0, 0.0]])
        self.assertEqual(self.world.added[0].args[1], line)
        self.assertEqual(self.world.added[0].calls[0][1][0], "root.x")

    def test_circular_on_a_named_axis(self):
        payload = go(self.world, circular(count=4, seed_point=[2, 0, 0], dry_run=True, total_angle_deg=90))
        close(self, payload["positions"], [(2, 0, 0), (2 * math.cos(math.pi / 6), 1, 0),
                                           (1, 2 * math.cos(math.pi / 6), 0), (0, 2, 0)], places=4)
        self.assertIsNone(payload["pattern"]["direction_two_set"])

    def test_circular_about_a_face_runs_through_its_axis(self):
        self.world.tokens["f"] = [Face(self.world.block)]
        payload = go(self.world, circular(axis={"face": ref("f", "face")}, count=2, seed_point=[3, 2, 0], dry_run=True))
        self.assertEqual(payload["positions"], [[3.0, 2.0, 0.0], [-1.0, 2.0, 0.0]])
        self.assertEqual(self.world.added, [])

    def test_a_partial_turn_about_an_entity_warns_of_the_sense(self):
        self.world.tokens["f"] = [Face(self.world.block)]
        payload = go(self.world, circular(axis={"face": ref("f", "face")}, count=2, total_angle_deg=90,
                                          seed_point=[3, 2, 0], dry_run=True))
        self.assertIn("rotation sense", " ".join(payload["warnings"]))

    def test_an_untrusted_prediction_is_only_a_warning(self):
        self.world.tokens["f"] = [Face(self.world.block)]
        self.world.effect = lambda e: [Element((0, 0, 0)), Element((1, 1, 0))]
        payload = go(self.world, circular(axis={"face": ref("f", "face")}, count=2, total_angle_deg=90,
                                          seed_point=[3, 2, 0]))
        self.assertEqual(payload["status"], "built")
        self.assertIn("differ from the predicted", " ".join(payload["warnings"]))

    def test_expression_total_is_read_in_degrees(self):
        arguments = circular(total_angle_deg=None, total_angle_expression="spread", count=2, seed_point=[1, 0, 0],
                             dry_run=True)
        close(self, go(self.world, arguments)["positions"], [(1, 0, 0), (0, 1, 0)])

    def test_body_seeds(self):
        def effect(entity):
            self.world.root.bRepBodies.extend(Body(f"Copy{i}", 100.0, self.world.root) for i in range(2))
            return [Element((0, 0, 0)) for _ in range(3)]

        self.world.effect = effect
        body = {"occurrence_path": "root", "body_name": "Block"}
        payload = go(self.world, rectangular(entities=[{"body": body}], seed_volume_delta=100.0))
        self.assertEqual(payload["status"], "built", payload)
        self.assertEqual(self.world.added[0].args[0], [self.world.block])
        self.assertEqual(payload["pattern"]["instances_reached"], 3)

    def test_a_body_that_is_not_there(self):
        body = {"occurrence_path": "root", "body_name": "Nope"}
        self.refused(rectangular(entities=[{"body": body}]), "Nope")

    def test_face_seeds(self):
        face = Face(self.world.block, PLANE)
        face.entityToken, face.pointOnFace = "f", P(0, 0, 0)
        self.world.tokens["f"] = [face]
        payload = go(self.world, rectangular(entities=[{"face": ref("f", "face")}], dry_run=True))
        self.assertEqual(payload["plan"]["seeds"][0]["kind"], "face")
        self.assertEqual(payload["plan"]["targets"], [{"occurrence_path": "root", "body_name": "Block"}])

    def test_a_hidden_target_is_mentioned(self):
        self.world.block.isVisible = False
        self.assertIn("hidden", " ".join(go(self.world, rectangular(dry_run=True))["warnings"]))

    def test_a_seed_with_no_bodies_is_mentioned(self):
        self.world.seed.bodies = []
        self.assertIn("reports no bodies", " ".join(go(self.world, rectangular(dry_run=True))["warnings"]))


class SubComponentTests(unittest.TestCase):
    """Sub:1 moved +10 in x and turned 90 degrees about z; Sub:2 left at the origin."""

    FRAME = ((10.0, 0.0, 0.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    HOME = (ORIGIN, X, Y, Z)

    def setUp(self):
        self.world = World()
        self.sub = Component("Sub")
        self.sub.features = Features(self.world)
        self.native = Body("Part", 20.0, self.sub)
        proxy = Body("Part", 20.0, self.sub, ((0, 0, 0), (100, 100, 100)))
        proxy.nativeObject = self.native
        self.proxy = proxy
        self.occurrence = Occurrence("Sub:1", self.sub, [proxy], self.FRAME)
        self.world.root.allOccurrences.append(self.occurrence)
        self.world.add_seed("Pocket", self.sub, [self.native])

    def arguments(self, **overrides):
        return rectangular(entities=[{"feature": "Pocket"}], **overrides)

    def test_lands_in_the_seeds_component(self):
        payload = go(self.world, self.arguments(dry_run=True))
        self.assertEqual(payload["plan"]["component"], "Sub")
        self.assertEqual(payload["plan"]["occurrence_path"], "Sub:1")
        self.assertEqual(payload["plan"]["targets"], [{"occurrence_path": "Sub:1", "body_name": "Part"}])

    def test_the_axis_is_the_components_and_the_result_is_world(self):
        go(self.world, self.arguments())
        self.assertEqual(self.world.added[0].args[1], "Sub.x")
        payload = go(self.world, self.arguments(dry_run=True, seed_point=[10, 1, 0]))
        self.assertEqual(payload["pattern"]["direction_world"], [[0.0, 1.0, 0.0]])
        self.assertEqual(payload["positions"], [[10.0, 1.0, 0.0], [10.0, 3.0, 0.0], [10.0, 5.0, 0.0]])

    def test_element_transforms_go_through_the_frame(self):
        self.world.effect = lambda e: [Element((0, 0, 0)), Element((2, 0, 0)), Element((4, 0, 0))]
        payload = go(self.world, self.arguments(seed_point=[10, 1, 0]))
        self.assertEqual(payload["positions_basis"], "elements")
        self.assertEqual(payload["positions"], [[10.0, 1.0, 0.0], [10.0, 3.0, 0.0], [10.0, 5.0, 0.0]])
        self.assertEqual(payload["status"], "built", payload)

    def test_a_circular_axis_origin_goes_through_the_frame(self):
        payload = go(self.world, circular(entities=[{"feature": "Pocket"}], count=2, seed_point=[12, 0, 0],
                                          dry_run=True))
        close(self, payload["positions"], [(12, 0, 0), (8, 0, 0)])

    def test_two_instances_say_which_one_world_values_use(self):
        second = Body("Part", 20.0, self.sub)
        second.nativeObject = self.native
        self.world.root.allOccurrences.append(Occurrence("Sub:2", self.sub, [second], self.HOME))
        payload = go(self.world, self.arguments(dry_run=True))
        self.assertIn("2 instances", " ".join(payload["warnings"]))
        self.assertEqual(payload["plan"]["occurrence_path"], "Sub:1")

    def test_instances_count_once(self):
        second = Body("Part", 20.0, self.sub)
        second.nativeObject = self.native
        self.world.root.allOccurrences.append(Occurrence("Sub:2", self.sub, [second], self.HOME))

        def effect(entity):
            self.proxy.volume -= 2.0
            second.volume -= 2.0
            return [Element((0, 0, 0)) for _ in range(3)]

        self.world.effect = effect
        payload = go(self.world, self.arguments(seed_volume_delta=-1.0))
        self.assertEqual(payload["status"], "built", payload)
        self.assertEqual(payload["pattern"]["volume_change"], -2.0)
        self.assertEqual(payload["pattern"]["instances_reached"], 3)

    def test_body_seed_goes_in_native_and_names_its_occurrence(self):
        body = {"occurrence_path": "Sub:1", "body_name": "Part"}
        self.world.effect = lambda e: [Element((0, 0, 0)) for _ in range(3)]
        go(self.world, rectangular(entities=[{"body": body}]))
        self.assertEqual(self.world.added[0].args[0], [self.native])

    def test_body_seeds_through_two_instances(self):
        second = Body("Other", 20.0, self.sub)
        self.world.root.allOccurrences.append(Occurrence("Sub:2", self.sub, [second], self.HOME))
        bodies = [{"body": {"occurrence_path": "Sub:1", "body_name": "Part"}},
                  {"body": {"occurrence_path": "Sub:2", "body_name": "Other"}}]
        payload = go(self.world, rectangular(entities=bodies))
        self.assertIn("different instances", payload["refusals"][0])

    def test_no_occurrence_at_all(self):
        self.world.root.allOccurrences.clear()
        payload = go(self.world, self.arguments())
        self.assertIn("no occurrence", payload["refusals"][0])


class HandlerTests(unittest.TestCase):
    def test_bad_arguments_never_reach_fusion(self):
        with mock.patch.object(pf, "get_app") as app:
            result = pf.pattern_features({"type": "rectangular"})
        self.assertTrue(result["isError"])
        app.assert_not_called()

    def test_no_design(self):
        with mock.patch.object(pf, "get_app") as app, \
                mock.patch.object(pf.adsk.fusion, "Design", create=True) as design:
            design.cast.return_value = None
            app.return_value.activeProduct = None
            result = pf.pattern_features(rectangular())
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["status"], "refused")

    def test_a_built_pattern_is_not_an_error(self):
        world = World()
        with mock.patch.object(pf, "get_app"), mock.patch.object(pf.adsk.fusion, "Design", create=True) as design, \
                mock.patch.object(pf, "FusionPattern", return_value=world):
            design.cast.return_value = world.design
            result = pf.pattern_features(rectangular())
        self.assertFalse(result["isError"])

    def test_a_suspect_pattern_is_an_error(self):
        world = World()
        world.effect = lambda e: [Element((0, 0, 0))]
        with mock.patch.object(pf, "get_app"), mock.patch.object(pf.adsk.fusion, "Design", create=True) as design, \
                mock.patch.object(pf, "FusionPattern", return_value=world):
            design.cast.return_value = world.design
            result = pf.pattern_features(rectangular())
        self.assertTrue(result["isError"])

    def test_a_crash_is_an_error(self):
        with mock.patch.object(pf, "get_app", side_effect=RuntimeError("boom")):
            result = pf.pattern_features(rectangular())
        self.assertTrue(result["isError"])
        self.assertIn("boom", result["content"][0]["text"])


class FusionLayerTests(unittest.TestCase):
    def session(self):
        design = mock.Mock()
        design.unitsManager.isValidExpression.return_value = True
        with mock.patch.object(pf.FusionScene, "__init__", return_value=None):
            session = pf.FusionPattern(design)
        return session, design.unitsManager

    def test_length_of_a_number(self):
        session, _ = self.session()
        self.assertEqual(session.length_cm({"text": "1 in", "number": 1.0}, "in"), 2.54)

    def test_length_of_an_expression(self):
        session, units = self.session()
        units.evaluateExpression.return_value = 7.0
        self.assertEqual(session.length_cm({"text": "pitch", "number": None}, "in"), 7.0)
        units.evaluateExpression.assert_called_once_with("pitch", "cm")

    def test_an_invalid_expression_is_a_value_error(self):
        session, units = self.session()
        units.isValidExpression.return_value = False
        with self.assertRaises(ValueError):
            session.length_cm({"text": "nope", "number": None}, "in")

    def test_compute_names_exist_in_fusion(self):
        with mock.patch.object(pf.adsk.fusion, "PatternComputeOptions", create=True) as options_enum:
            session, _ = self.session()
            self.assertEqual(session.compute_option("identical"), options_enum.IdenticalPatternCompute)

    def test_construction_axis(self):
        session, _ = self.session()
        self.assertEqual(session.construction_axis(Component("c"), "y"), "c.y")


class SchemaTests(unittest.TestCase):
    def _tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == tool_surface.PATTERN_FEATURES)

    def test_schema_matches_the_handler(self):
        schema = self._tool()["inputSchema"]
        self.assertEqual(set(schema["properties"]), pf._SHARED | pf._RECTANGULAR_ONLY | pf._CIRCULAR_ONLY)
        self.assertFalse(schema["additionalProperties"])

    def test_seed_keys(self):
        items = self._tool()["inputSchema"]["properties"]["entities"]["items"]
        self.assertEqual(set(items["properties"]), set(pf._SEED_KEYS))

    def test_direction_keys(self):
        properties = self._tool()["inputSchema"]["properties"]
        self.assertEqual(set(properties["direction_one"]["properties"]), set(pf._DIRECTION_KEYS))
        self.assertEqual(set(properties["axis"]["properties"]), set(pf._ROTATION_KEYS))

    def test_dry_run_defaults_false_in_the_text(self):
        self.assertIn("default false", self._tool()["description"])


if __name__ == "__main__":
    unittest.main()
