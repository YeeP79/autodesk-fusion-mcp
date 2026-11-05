"""extrude_profile: refusals, sides, targets, collateral, and both answers to EP-3 and EP-4, on fakes."""

import json
import math
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import extrude_profile as ep
from fusion_bridge import tool_surface
from fusion_bridge.common.bodies import assign_keys, body_entries

IN = 2.54
POSITIVE, NEGATIVE = 0, 1
JOIN, CUT, INTERSECT, NEW_BODY = 0, 1, 2, 3
HEALTHY, ERROR = 0, 2


def vec(p):
    return NS(x=p[0], y=p[1], z=p[2])


def box(low, high):
    return NS(minPoint=vec(low), maxPoint=vec(high))


class Face:
    objectType = "adsk::fusion::BRepFace"
    assemblyContext = None

    def __init__(self, body, point, normal=(0, 0, 1), token="t", vertices=()):
        self.body, self.pointOnFace, self.entityToken = body, vec(point), token
        self.geometry = NS(surfaceType=0, normal=vec(normal))
        self.boundingBox = box(point, point)
        self.vertices = [NS(geometry=vec(v)) for v in vertices]


class Body:
    isSolid = True
    isVisible = True
    assemblyContext = None
    nativeObject = None

    def __init__(self, name, volume=1.0, low=(0, 0, 0), high=(1, 1, 1), parent=None, faces=None):
        self.name, self.volume, self.parentComponent = name, volume, parent
        self.boundingBox = box(low, high)
        self.faces = faces if faces is not None else []

    def createForAssemblyContext(self, occurrence):
        proxy = Body(self.name, self.volume, parent=self.parentComponent)
        proxy.nativeObject, proxy.assemblyContext = self, occurrence
        return proxy


class Occurrence:
    isReferencedComponent = False
    isVisible = True

    def __init__(self, path, component, shift=(0, 0, 0)):
        self.fullPathName, self.component = path, component
        self.transform2 = NS(getAsCoordinateSystem=lambda: (vec(shift), vec((1, 0, 0)), vec((0, 1, 0)), vec((0, 0, 1))))

    @property
    def bRepBodies(self):
        return [self._proxy(b) for b in self.component.bRepBodies]

    def _proxy(self, body):
        proxy = Body(body.name, body.volume, parent=self.component, faces=body.faces)
        proxy.boundingBox = body.boundingBox
        proxy.nativeObject, proxy.assemblyContext = body, self
        return proxy


class Profile:
    def __init__(self, area=4.0, loops=1, plane_normal=(0, 0, 1), origin=(0, 0, 0)):
        self.area, self.plane = area, NS(origin=vec(origin), normal=vec(plane_normal))
        self.profileLoops = Profiles(NS(isOuter=i == 0, profileCurves=Profiles()) for i in range(loops))
        self.boundingBox = box((0, 0, 0), (2, 2, 0))
        self.entityToken = "profile"

    def areaProperties(self, _accuracy):
        return NS(area=self.area, centroid=vec((1, 1, 0)))


class Sketch:
    objectType = "adsk::fusion::Sketch"
    assemblyContext = None
    nativeObject = None

    def __init__(self, parent, profiles):
        self.parentComponent, self.profiles, self.name = parent, profiles, "S1"
        self.queries = []

    def modelToSketchSpace(self, point):
        self.queries.append((point.x, point.y, point.z))
        return point

    def sketchToModelSpace(self, point):
        return point


class Input:
    participant_getter = None
    refuse_participants = False

    def __init__(self, profile, operation):
        self.profile, self.operation = profile, operation
        self.extent, self.startExtent, self.creationOccurrence = None, None, None
        self.named = None

    @property
    def participantBodies(self):
        if isinstance(self.participant_getter, Exception):
            raise self.participant_getter
        return self.participant_getter

    @participantBodies.setter
    def participantBodies(self, bodies):
        if self.refuse_participants:
            raise RuntimeError("participants not valid here")
        self.named = bodies

    def setOneSideExtent(self, *args):
        self.extent = ("one", *args)
        return True

    def setTwoSidesExtent(self, *args):
        self.extent = ("two", *args)
        return True

    def setSymmetricExtent(self, *args):
        self.extent = ("symmetric", *args)
        return True


class Feature:
    healthState = HEALTHY
    errorOrWarningMessage = ""
    objectType = "adsk::fusion::ExtrudeFeature"
    timelineObject = NS(index=4)

    def __init__(self, world, component, saved, bodies=()):
        self.world, self.parentComponent, self.saved = world, component, saved
        self.bodies, self.name = list(bodies), "Extrude1"
        self.startFaces = self.endFaces = self.sideFaces = None
        self.deleted = False

    def deleteMe(self):
        self.deleted = True
        if self.world.undo_works:
            self.world.restore(self.saved)
        return True


class Extrudes:
    def __init__(self, world, component):
        self.world, self.component, self.inputs = world, component, []

    def createInput(self, profile, operation):
        made = Input(profile, operation)
        made.participant_getter = self.world.getter
        made.refuse_participants = self.world.refuse_participants and operation == JOIN
        self.inputs.append(made)
        return made

    def add(self, made):
        return self.world.on_add(made, self.component)


class Combines:
    def __init__(self, world, component):
        self.world, self.component = world, component

    def createInput(self, target, tools):
        return NS(target=target, tools=tools, operation=None, isKeepToolBodies=True)

    def add(self, made):
        return self.world.on_combine(made, self.component)


class Component:
    def __init__(self, world, name):
        self.name, self.bRepBodies, self.sketches_by_name = name, [], {}
        self.allOccurrences = []
        self.extrudes = Extrudes(world, self)
        self.features = NS(extrudeFeatures=self.extrudes, combineFeatures=Combines(world, self))
        self.sketches = NS(itemByName=self.sketches_by_name.get)


class Units:
    def isValidExpression(self, text, _kind):
        return "bad" not in text

    def evaluateExpression(self, text, _kind):
        return {"wall*2": 2.0, "wall-wall": 0.0}.get(text, 1.0)


class Timeline(list):
    markerPosition = 0
    count = 0


class Design:
    designType = 1
    activeEditObject = None
    parentDocument = None

    def __init__(self, root):
        self.rootComponent, self.unitsManager = root, Units()
        self.timeline = Timeline()
        self.tokens = {}
        self.allComponents = NS(itemByName=lambda name: None)

    def findEntityByToken(self, token):
        return self.tokens.get(token, [])


class Scene:
    def __init__(self, design):
        self.design = design
        self.world = design.world

    def entries(self):
        return assign_keys(list(body_entries(self.design.rootComponent)))

    def by_token(self, token):
        return self.design.tokens.get(token, [])

    def at_point(self, point, _kind, *_):
        return [f for b in self.world.all_bodies() for f in b.faces
                if (f.pointOnFace.x, f.pointOnFace.y, f.pointOnFace.z) == tuple(point)]

    def distance(self, _entity, _point):
        return 0.0


class World:
    """A root with bodies and one sketch. ``effects`` maps a body name to the volume a feature leaves it."""

    def __init__(self, profiles=None):
        self.undo_works = True
        self.getter = None
        self.refuse_participants = False
        self.flip = False
        self.effects = {}
        self.vanish = []
        self.depth_scale = 1.0
        self.refusal = None
        self.adds = 0
        self.health = HEALTHY
        self.root = Component(self, "root")
        self.design = Design(self.root)
        self.design.world = self
        self.sketch = Sketch(self.root, Profiles(profiles or [Profile()]))
        self.design.tokens["sk"] = [self.sketch]
        self.combined = []

    def body(self, name, volume=12.0, component=None, **kwargs):
        component = component or self.root
        made = Body(name, volume, parent=component, **kwargs)
        component.bRepBodies.append(made)
        return made

    def all_bodies(self):
        return list(self.root.bRepBodies) + [b for o in self.root.allOccurrences for b in o.component.bRepBodies]

    def state(self):
        components = [self.root] + [o.component for o in self.root.allOccurrences]
        return [(c, list(c.bRepBodies), [(b, b.volume) for b in c.bRepBodies]) for c in components]

    def restore(self, saved):
        for component, bodies, volumes in saved:
            component.bRepBodies[:] = bodies
            for body, volume in volumes:
                body.volume = volume

    def _new_body(self, made, component, feature):
        kind, definition, direction = made.extent[:3]
        depth = float(definition.value.text.split()[0]) * IN * self.depth_scale
        positive = (direction == POSITIVE) != self.flip
        z = depth if positive else -depth
        body = self.body(f"Body{self.adds}", 4.0 * IN * IN * depth / IN / IN, component)
        body.faces = [Face(body, (1, 1, 0), (0, 0, -1), "start"), Face(body, (1, 1, z), (0, 0, 1), "end"),
                      Face(body, (0, 1, z / 2), (-1, 0, 0), "side")]
        feature.bodies = [body]
        feature.startFaces, feature.endFaces, feature.sideFaces = body.faces[:1], body.faces[1:2], body.faces[2:]

    def on_add(self, made, component):
        self.adds += 1
        if self.refusal:
            raise RuntimeError(self.refusal)
        feature = Feature(self, component, self.state())
        feature.healthState = self.health
        if made.operation == NEW_BODY:
            self._new_body(made, component, feature)
        for body in self.all_bodies():
            if body.name in self.effects:
                body.volume = self.effects[body.name]
        for name in self.vanish:
            for owner in [self.root] + [o.component for o in self.root.allOccurrences]:
                owner.bRepBodies[:] = [b for b in owner.bRepBodies if b.name != name]
        return feature

    def on_combine(self, made, component):
        self.combined.append(made)
        tools = list(made.tools)
        component.bRepBodies[:] = [b for b in component.bRepBodies if b not in tools]
        made.target.volume += sum(b.volume for b in tools)
        return Feature(self, component, self.state())

    def job(self, **arguments):
        request = ep.parse_request(request_args(**arguments))
        return ep.Job(self.design, ep.FusionApi(self.design), Scene, request)

    def run(self, **arguments):
        return self.job(**arguments).run()

    @property
    def inputs(self):
        return self.root.extrudes.inputs


class Profiles(list):
    @property
    def count(self):
        return len(self)

    def item(self, index):
        return self[index]


def request_args(**overrides):
    arguments = {"sketch_token": "sk", "operation": "new_body", "extent": {"type": "distance", "distance": 1.0},
                 "direction": [0, 0, 1]}
    arguments.update(overrides)
    return {k: v for k, v in arguments.items() if v is not None}


def cut_args(*names, **overrides):
    return {"operation": "cut", "target_bodies": [{"occurrence_path": "root", "body_name": n} for n in names],
            **overrides}


class FusionStubs(unittest.TestCase):
    """Stand-ins for the adsk classes the real calls build."""

    def setUp(self):
        created = lambda *args: NS(value=args[0] if args else None, args=args, isPositiveDirection=None)  # noqa: E731
        fusion, core = ep.adsk.fusion, ep.adsk.core
        stubs = [
            (core, "ValueInput", NS(createByString=lambda text: NS(text=text))),
            (core, "Point3D", NS(create=lambda *p: vec(p))),
            (core, "ObjectCollection", NS(create=lambda: Collection())),
            (fusion, "DistanceExtentDefinition", NS(create=created)),
            (fusion, "ThroughAllExtentDefinition", NS(create=created)),
            (fusion, "ToEntityExtentDefinition", NS(create=created)),
            (fusion, "OffsetStartDefinition", NS(create=created)),
            (fusion, "ExtentDirections", NS(PositiveExtentDirection=POSITIVE, NegativeExtentDirection=NEGATIVE)),
            (fusion, "FeatureOperations", NS(JoinFeatureOperation=JOIN, CutFeatureOperation=CUT,
                                             IntersectFeatureOperation=INTERSECT,
                                             NewBodyFeatureOperation=NEW_BODY)),
        ]
        for owner, name, value in stubs:
            patcher = mock.patch.object(owner, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)


class Collection(list):
    add = list.append


class ExtrudeSign(unittest.TestCase):
    def tilted(self, degrees):
        return (math.sin(math.radians(degrees)), 0.0, math.cos(math.radians(degrees)))

    def test_along_and_against_the_normal(self):
        self.assertEqual(ep.extrude_sign((0, 0, 1), (0, 0, 5)), 1)
        self.assertEqual(ep.extrude_sign((0, 0, 1), (0, 0, -5)), -1)

    def test_just_inside_the_limit(self):
        self.assertEqual(ep.extrude_sign((0, 0, 1), self.tilted(0.999)), 1)
        self.assertEqual(ep.extrude_sign((0, 0, 1), self.tilted(180 - 0.999)), -1)

    def test_just_over_the_limit(self):
        self.assertIsNone(ep.extrude_sign((0, 0, 1), self.tilted(1.001)))
        self.assertIsNone(ep.extrude_sign((0, 0, 1), self.tilted(180 - 1.001)))

    def test_no_direction_has_no_sign(self):
        self.assertIsNone(ep.extrude_sign((0, 0, 1), (0, 0, 0)))


class AbutmentGap(unittest.TestCase):
    def test_on_the_face_is_no_gap(self):
        self.assertIsNone(ep.abutment_gap(2.54, 2.54))

    def test_at_the_tolerance_is_an_abutment(self):
        self.assertAlmostEqual(ep.abutment_gap(0.0, ep.ABUTMENT_TOLERANCE_CM), ep.ABUTMENT_TOLERANCE_CM)

    def test_just_over_the_tolerance_is_not(self):
        self.assertIsNone(ep.abutment_gap(0.0, ep.ABUTMENT_TOLERANCE_CM * 1.001))

    def test_the_25_nanometer_floor_is_caught(self):
        self.assertAlmostEqual(ep.abutment_gap(2.54, 2.54 + 2.5e-6), 2.5e-6)

    def test_either_side_of_the_face(self):
        self.assertIsNotNone(ep.abutment_gap(1.001, 1.0))
        self.assertIsNotNone(ep.abutment_gap(0.999, 1.0))

    def test_abutments_skip_what_is_tilted_or_away_from_the_path(self):
        here, away = ((0, 0, 0), (2, 2, 3)), ((50, 50, 1), (60, 60, 1))
        planes = [((1, 1, 1.001), (0, 0, 1), here), ((1, 1, 1.001), (0, 1, 1), here),
                  ((55, 55, 1.001), (0, 0, 1), away)]
        found = ep.abutments(planes, (0, 0, 0), (0, 0, 1), 1.0, ((0, 0, 0), (2, 2, 1)))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["point"], (1, 1, 1.001))


class Sweeps(unittest.TestCase):
    def test_a_box_swept_up(self):
        self.assertEqual(ep.swept_box([(0, 0, 0), (2, 2, 0)], (0, 0, 1), 0.0, 3.0), ((0, 0, 0), (2, 2, 3)))

    def test_a_span_with_no_end_runs_to_the_model(self):
        self.assertEqual(ep.sweep_span(0.0, None, 0.0, (-5.0, 9.0)), (0.0, 9.0))
        self.assertEqual(ep.sweep_span(0.0, None, None, (-5.0, 9.0)), (-5.0, 9.0))

    def test_a_model_all_behind_the_start_leaves_an_empty_span(self):
        self.assertEqual(ep.sweep_span(1.0, None, 0.0, (-5.0, -1.0)), (1.0, 1.0))

    def test_a_fixed_span_ignores_the_model(self):
        self.assertEqual(ep.sweep_span(1.0, 2.0, 0.5, (-50.0, 50.0)), (0.5, 3.0))

    def test_side_verdict(self):
        self.assertEqual(ep.side_verdict(0.0, 2.0, 0.0), "ok")
        self.assertEqual(ep.side_verdict(-2.0, 0.0, 0.0), "wrong_side")
        self.assertEqual(ep.side_verdict(-3.0, -1.0, -1.0), "wrong_side")
        self.assertEqual(ep.side_verdict(0.0, 0.0, 0.0), "ok")

    def test_volume_verdict(self):
        self.assertEqual(ep.volume_verdict(4.0, 4.001), "ok")
        self.assertEqual(ep.volume_verdict(4.0, 3.0), "mismatch")
        self.assertEqual(ep.volume_verdict(None, 3.0), "not_applicable")
        self.assertEqual(ep.volume_verdict(4.0, None), "not_applicable")


def row(index, area, loops=1, contains=None):
    return {"index": index, "area": area, "centroid": [0.0, 0.0], "loop_count": loops, "contains_point": contains}


class PickProfiles(unittest.TestCase):
    # Smallest first on purpose: any choice by position fails here.
    ROWS = [row(0, 1.0), row(1, 2.0, loops=3), row(2, 9.0, loops=1)]

    def test_only_with_one(self):
        self.assertEqual(ep.pick_profiles("only", [row(4, 1.0)]), [4])

    def test_only_with_several_lists_them(self):
        with self.assertRaises(ep.Refused) as caught:
            ep.pick_profiles("only", self.ROWS)
        self.assertEqual(len(caught.exception.extra["profiles"]), 3)
        self.assertEqual(caught.exception.extra["profiles"][1], {"area": 2.0, "centroid": [0.0, 0.0], "loops": 3})

    def test_no_profiles(self):
        with self.assertRaises(ep.Refused):
            ep.pick_profiles("largest", [])

    def test_largest_and_most_loops(self):
        self.assertEqual(ep.pick_profiles("largest", self.ROWS), [2])
        self.assertEqual(ep.pick_profiles("most_loops", self.ROWS), [1])

    def test_most_loops_ties_go_to_area(self):
        self.assertEqual(ep.pick_profiles("most_loops", [row(0, 1.0, 2), row(1, 5.0, 2)]), [1])

    def test_containing_points_keep_one_of_each(self):
        hit = lambda i: [row(k, 1.0, contains=k == i) for k in range(3)]  # noqa: E731
        self.assertEqual(ep.pick_profiles("containing_points", self.ROWS, [hit(2), hit(0), hit(2)]), [2, 0])

    def test_a_point_in_nothing_is_refused(self):
        with self.assertRaisesRegex(ep.Refused, "point 0 is in no profile"):
            ep.pick_profiles("containing_point", self.ROWS, [[row(0, 1.0, contains=False)]])

    def test_a_point_that_cannot_be_told(self):
        with self.assertRaisesRegex(ep.Refused, "could not be determined"):
            ep.pick_profiles("containing_point", self.ROWS, [[row(0, 1.0, contains=None)]])


class Refusals(unittest.TestCase):
    """Refusal matrix: nothing here reaches Fusion."""

    def assertRefused(self, pattern, **overrides):
        with self.assertRaisesRegex(ValueError, pattern):
            ep.parse_request(request_args(**overrides))

    def test_operations_needing_targets(self):
        for operation in ("cut", "join", "intersect"):
            self.assertRefused("every body the feature passes through", operation=operation)
            self.assertRefused("every body the feature passes through", operation=operation, target_bodies=[])

    def test_new_body_with_targets(self):
        self.assertRefused("drop 'target_bodies'", target_bodies=[{"occurrence_path": "root", "body_name": "B"}])

    def test_taper_with_through_all_and_two_sides(self):
        self.assertRefused("taper", extent={"type": "through_all"}, taper_deg=5)
        self.assertRefused("taper", extent={"type": "two_sides", "distance": 1, "side_two_distance": 1}, taper_deg=5)
        self.assertRefused("taper", extent={"type": "through_all"}, taper_expression="draft + 1 deg")

    def test_zero_taper_is_no_taper(self):
        self.assertIsNone(ep.parse_request(request_args(extent={"type": "through_all"}, taper_deg=0))["taper"])

    def test_direction_and_side_together(self):
        self.assertRefused("not both", side="positive")

    def test_one_sided_needs_a_way(self):
        self.assertRefused("needs 'direction' or 'side'", direction=None)
        self.assertRefused("needs 'direction' or 'side'", direction=None, extent={"type": "through_all"})

    def test_both_ways_takes_no_way(self):
        self.assertRefused("drop 'direction'", extent={"type": "symmetric", "distance": 1})
        self.assertRefused("drop 'direction'", extent={"type": "through_all", "both_sides": True})

    def test_distance_and_expression_together(self):
        self.assertRefused("not both", extent={"type": "distance", "distance": 1, "distance_expression": "1 in"})

    def test_bare_number_expression(self):
        self.assertRefused("no unit", extent={"type": "distance", "distance_expression": "2"})

    def test_a_distance_is_positive(self):
        self.assertRefused("greater than 0", extent={"type": "distance", "distance": -1})
        self.assertRefused("greater than 0", extent={"type": "distance", "distance": 0})

    def test_integer_profile_index_is_not_a_select(self):
        self.assertRefused("profile_select", profile_select=0)
        self.assertRefused("unknown", profile_index=0)

    def test_point_keys_go_with_their_select(self):
        self.assertRefused("needs profile_select", containing_point=[0, 0, 0])
        self.assertRefused("needs 'containing_point'", profile_select="containing_point")
        self.assertRefused("non-empty", profile_select="containing_points", containing_points=[])

    def test_sketch_named_once(self):
        self.assertRefused("exactly one", sketch_name="S1")
        self.assertRefused("exactly one", sketch_token=None)
        self.assertRefused("'component' only goes", component="Sub")

    def test_extent_shapes(self):
        self.assertRefused("'extent' is required", extent=None)
        self.assertRefused("extent.type", extent={"type": []})
        self.assertRefused("not used by a distance", extent={"type": "distance", "distance": 1, "full_length": True})
        self.assertRefused("exactly one", extent={"type": "to_entity"})
        self.assertRefused("not an edge", extent={"type": "to_entity", "to": {"token": "t", "kind": "edge"}})

    def test_bad_scalars(self):
        self.assertRefused("'units'", units=["in"])
        self.assertRefused("'direction' has no length", direction=[0, 0, 0])
        self.assertRefused("three finite", direction=[0, 0, float("nan")])
        self.assertRefused("'dry_run'", dry_run="yes")
        self.assertRefused("'on_collateral'", on_collateral="ignore")
        self.assertRefused("taper_deg", taper_deg=90)

    def test_body_name_is_for_a_new_body(self):
        self.assertRefused("only a new_body", body_name="Boss", **cut_args("B"))

    def test_a_refused_argument_never_reaches_fusion(self):
        with mock.patch.object(ep, "get_app") as app:
            result = ep.extrude_profile(request_args(operation="cut"))
        app.assert_not_called()
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertEqual(payload["status"], "refused")
        self.assertFalse(payload["ok"])

    def test_dry_run_defaults_false(self):
        self.assertFalse(ep.parse_request(request_args())["dry_run"])


class NewBody(FusionStubs):
    def test_along_and_against_the_normal(self):
        for direction, enum, side in (([0, 0, 1], POSITIVE, "positive"), ([0, 0, -1], NEGATIVE, "negative")):
            world = World()
            result = world.run(direction=direction)
            self.assertEqual(result["status"], "built", result["warnings"])
            self.assertEqual(world.inputs[0].extent[2], enum)
            self.assertEqual(result["extrude"]["side_used"], side)
            self.assertEqual(result["extrude"]["direction_world"], [0, 0, direction[2]])
            self.assertAlmostEqual(result["extrude"]["measured_depth"], 1.0)
            self.assertEqual(result["extrude"]["requested_depth"], 1.0)

    def test_side_is_relative_to_the_normal(self):
        world = World()
        world.run(direction=None, side="negative")
        self.assertEqual(world.inputs[0].extent[2], NEGATIVE)

    def test_sign_is_asked_of_the_plane(self):
        world = World([Profile(plane_normal=(0, 0, -1))])
        world.flip = True  # Fusion's positive runs along the plane's -z.
        result = world.run(direction=[0, 0, 1])
        self.assertEqual(world.inputs[0].extent[2], NEGATIVE)
        self.assertEqual(result["extrude"]["direction_world"], [0, 0, 1])

    def test_direction_off_the_normal(self):
        result = World().run(direction=[0, 1, 1])
        self.assertEqual(result["status"], "refused")
        self.assertIn("45.000 deg", result["refusals"][0])

    def test_volume_checked(self):
        world = World()
        result = world.run()
        self.assertEqual(result["extrude"]["expected_volume"], round(4.0 / IN ** 2 * 1.0, 5))
        self.assertEqual(result["profile"]["count"], 1)

    def test_faces_on_the_result(self):
        result = World().run()
        self.assertEqual(result["faces"]["start"][0]["token"], "start")
        self.assertEqual(result["faces"]["end"][0]["token"], "end")
        self.assertEqual(result["faces"]["end"][0]["kind"], "face")
        self.assertEqual(result["faces"]["end"][0]["body"]["occurrence_path"], "root")
        self.assertEqual(len(result["faces"]["side"]), 1)

    def test_names_are_read_back_in_the_snapshot(self):
        result = World().run(body_name="Boss", feature_name="Boss extrude")
        self.assertEqual(result["feature"]["name"], "Boss extrude")
        self.assertEqual(result["bodies"]["created"][0]["ref"]["body_name"], "Boss")

    def test_a_short_build_is_suspect(self):
        world = World()
        world.depth_scale = 0.5
        result = world.run()
        self.assertEqual(result["status"], "suspect")
        self.assertFalse(result["ok"])
        self.assertTrue(any("measured depth" in w for w in result["warnings"]))

    def test_wrong_side_rebuilds_once(self):
        world = World()
        world.flip = True
        original = world.on_add

        def once(made, component):
            feature = original(made, component)
            world.flip = False
            return feature

        world.on_add = once
        result = world.run()
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertTrue(result["extrude"]["rebuilt_other_side"])
        self.assertEqual(world.adds, 2)
        self.assertEqual(result["extrude"]["side_used"], "negative")
        self.assertEqual(len(world.root.bRepBodies), 1)
        self.assertTrue(any("rebuilt" in w for w in result["warnings"]))

    def test_wrong_side_twice_stops_and_says_so(self):
        world = World()
        world.flip = True
        result = world.run()
        self.assertEqual(world.adds, 2)
        self.assertEqual(result["status"], "suspect")

    def test_wrong_side_that_will_not_undo(self):
        world = World()
        world.flip = True
        world.undo_works = False
        result = world.run()
        self.assertEqual(result["status"], "refused")
        self.assertEqual(world.adds, 1)

    def test_it_touches_nothing_else(self):
        world = World()
        world.body("Block")
        world.effects = {"Block": 3.0}
        result = world.run()
        self.assertEqual(result["status"], "undone")
        self.assertEqual(result["collateral"], ["root/Block"])

    def test_fusion_refusal_is_kept_word_for_word(self):
        world = World()
        world.refusal = "No target body found to cut or intersect!"
        result = world.run()
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["refusals"], ["No target body found to cut or intersect!"])
        self.assertIsNotNone(result["baseline_token"])

    def test_a_feature_in_error_is_deleted_and_refused(self):
        world = World()
        world.health = ERROR
        result = world.run()
        self.assertEqual(result["status"], "refused")
        self.assertEqual(world.root.bRepBodies, [])

    def test_type_error_reaches_the_handler_as_an_error(self):
        world = World()
        world.on_add = mock.Mock(side_effect=TypeError("wrong call"))
        app = NS(activeProduct=world.design)
        with mock.patch.object(ep, "get_app", return_value=app), \
                mock.patch.object(ep.adsk.fusion, "Design", NS(cast=lambda p: p), create=True), \
                mock.patch.object(ep, "FusionScene", Scene), \
                mock.patch.object(ep.adsk.core, "LogLevels", NS(ErrorLogLevel=0), create=True):
            result = ep.extrude_profile(request_args())
        self.assertTrue(result["isError"])
        self.assertIn("wrong call", result["content"][0]["text"])


class Extents(FusionStubs):
    def extent_of(self, **overrides):
        world = World()
        world.on_add = lambda made, component: None
        job = world.job(dry_run=True, **overrides)
        result = job.run()
        self.assertEqual(result["status"], "planned", result.get("refusals"))
        return world.inputs[0], result

    def test_symmetric_defaults_to_the_whole_length(self):
        made, result = self.extent_of(direction=None, extent={"type": "symmetric", "distance": 2.0})
        self.assertEqual(made.extent[0], "symmetric")
        self.assertEqual(made.extent[1].text, "2 in")
        self.assertIs(made.extent[2], True)
        self.assertEqual(result["extrude"]["requested_depth"], 2.0)
        self.assertEqual(result["extrude"]["side_used"], "symmetric")

    def test_symmetric_half_length_doubles(self):
        _, result = self.extent_of(direction=None, extent={"type": "symmetric", "distance": 2.0, "full_length": False})
        self.assertEqual(result["extrude"]["requested_depth"], 4.0)

    def test_two_sides_follow_the_direction(self):
        extent = {"type": "two_sides", "distance": 2.0, "side_two_distance": 0.5}
        forward, _ = self.extent_of(extent=extent)
        self.assertEqual([forward.extent[1].value.text, forward.extent[2].value.text], ["2 in", "0.5 in"])
        behind, _ = self.extent_of(extent=extent, direction=[0, 0, -1])
        self.assertEqual([behind.extent[1].value.text, behind.extent[2].value.text], ["0.5 in", "2 in"])

    def test_through_all(self):
        made, result = self.extent_of(extent={"type": "through_all"}, direction=[0, 0, -1])
        self.assertEqual(made.extent[0], "one")
        self.assertIs(made.extent[1].isPositiveDirection, False)
        self.assertEqual(made.extent[2], NEGATIVE)
        self.assertIsNone(result["extrude"]["requested_depth"])

    def test_through_all_both_sides(self):
        made, result = self.extent_of(direction=None, extent={"type": "through_all", "both_sides": True})
        self.assertEqual(made.extent[0], "two")
        self.assertEqual(result["extrude"]["side_used"], "both")

    def test_taper_and_expression(self):
        made, result = self.extent_of(extent={"type": "distance", "distance_expression": "wall*2"},
                                      taper_expression="draft + 1 deg")
        self.assertEqual(made.extent[1].value.text, "wall*2")
        self.assertEqual(made.extent[3].text, "draft + 1 deg")
        self.assertIsNone(result["extrude"]["expected_volume"])
        self.assertAlmostEqual(result["extrude"]["requested_depth"], 2.0 / IN, places=4)

    def test_number_taper_goes_in_degrees(self):
        made, _ = self.extent_of(taper_deg=5)
        self.assertEqual(made.extent[3].text, "5 deg")

    def test_start_offset(self):
        made, _ = self.extent_of(start_offset=0.25)
        self.assertEqual(made.startExtent.value.text, "0.25 in")

    def test_an_expression_that_comes_to_nothing(self):
        result = World().run(extent={"type": "distance", "distance_expression": "wall-wall"})
        self.assertEqual(result["status"], "refused")
        self.assertIn("must be positive", result["refusals"][0])

    def test_an_expression_fusion_cannot_read(self):
        result = World().run(extent={"type": "distance", "distance_expression": "bad + 1 in"})
        self.assertEqual(result["status"], "refused")
        self.assertIn("not a valid expression", result["refusals"][0])

    def test_no_set_distance_extent_anywhere(self):
        self.assertFalse(hasattr(Input, "setDistanceExtent"))  # So a call to it would fail here.


class ToEntity(FusionStubs):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.block = self.world.body("Block")
        self.face = Face(self.block, (0, 0, 5), token="to")

    def extent(self, **extra):
        return {"extent": {"type": "to_entity", **extra}}

    def test_a_face(self):
        self.world.design.tokens["to"] = [self.face]
        result = self.world.run(dry_run=True, **self.extent(to={"token": "to", "kind": "face"}))
        self.assertEqual(result["status"], "planned", result.get("refusals"))
        definition = self.world.inputs[0].extent[1]
        self.assertIs(definition.args[0], self.face)
        self.assertEqual(definition.args[1], False)
        self.assertEqual(len(definition.args), 2)
        self.assertEqual(result["resolved"], [{"role": "to", "kind": "face", "resolved_by": "token"}])

    def test_offset_and_chained(self):
        self.world.design.tokens["to"] = [self.face]
        self.world.run(dry_run=True, **self.extent(to={"token": "to", "kind": "face"}, to_offset=-0.1, chained=True))
        definition = self.world.inputs[0].extent[1]
        self.assertIs(definition.args[1], True)
        self.assertEqual(definition.args[2].text, "-0.1 in")

    def test_a_construction_plane_by_token_alone(self):
        plane = NS(objectType="adsk::fusion::ConstructionPlane")
        self.world.design.tokens["plane"] = [plane]
        self.world.run(dry_run=True, **self.extent(to={"token": "plane"}))
        self.assertIs(self.world.inputs[0].extent[1].args[0], plane)

    def test_a_body(self):
        self.world.run(dry_run=True, **self.extent(to_body={"occurrence_path": "root", "body_name": "Block"}))
        self.assertIs(self.world.inputs[0].extent[1].args[0], self.block)

    def test_a_missing_entity_refuses_with_no_fallback(self):
        result = self.world.run(**self.extent(to={"token": "gone", "kind": "face"}))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.world.adds, 0)

    def test_a_token_that_turned_out_an_edge(self):
        self.world.design.tokens["edge"] = [NS(objectType="adsk::fusion::BRepEdge")]
        result = self.world.run(**self.extent(to={"token": "edge"}))
        self.assertEqual(result["status"], "refused")
        self.assertIn("names an edge", result["refusals"][0])


class Targets(FusionStubs):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.block = self.world.body("Block", low=(0, 0, 0), high=(10, 7, 2))
        self.other = self.world.body("Other", low=(0, 0, 0), high=(10, 7, 2))

    def test_only_the_named_body_participates(self):
        self.world.effects = {"Block": 10.0}
        result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertEqual(self.world.inputs[-1].named, [self.block])
        row = result["bodies"]["touched"][0]
        self.assertEqual((row["ref"]["body_name"], row["role"]), ("Block", "target"))
        self.assertAlmostEqual(row["delta"], -2.0 / IN ** 3, places=4)
        self.assertEqual(result["bodies"]["outside_target"], [])

    def test_bodies_in_the_path_are_reported(self):
        self.world.effects = {"Block": 10.0}
        result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["in_path_not_targeted"], [{"occurrence_path": "root", "body_name": "Other"}])
        self.assertTrue(any("not named as targets" in w for w in result["warnings"]))

    def test_outside_loss_is_undone_and_named(self):
        self.world.effects = {"Block": 10.0, "Other": 9.0}
        result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["status"], "undone")
        self.assertFalse(result["ok"])
        self.assertEqual(result["collateral"], ["root/Other"])
        self.assertEqual((self.block.volume, self.other.volume), (12.0, 12.0))
        self.assertEqual(result["bodies"]["outside_target"][0]["ref"]["body_name"], "Other")

    def test_outside_body_vanishing_is_collateral(self):
        self.world.effects = {"Block": 10.0}
        self.world.vanish = ["Other"]
        result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["status"], "undone")
        self.assertIn(self.other, self.world.root.bRepBodies)

    def test_undo_that_does_not_verify_stays_refused(self):
        self.world.effects = {"Block": 10.0, "Other": 9.0}
        self.world.undo_works = False
        result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["status"], "refused")
        self.assertTrue(any("did not return" in w for w in result["warnings"]))
        self.assertEqual(result["collateral"], ["root/Other"])

    def test_keep_leaves_it_and_flags_it(self):
        self.world.effects = {"Block": 10.0, "Other": 9.0}
        result = self.world.run(on_collateral="keep", **cut_args("Block"))
        self.assertEqual(result["status"], "suspect")
        self.assertEqual(self.other.volume, 9.0)
        self.assertEqual(result["bodies"]["outside_target"][0]["role"], "outside")

    def test_target_unchanged_is_suspect(self):
        result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["status"], "suspect")
        self.assertEqual(result["targets_unchanged"], ["root/Block"])

    def test_missing_target_lists_candidates(self):
        result = self.world.run(**cut_args("Nope"))
        self.assertEqual(result["status"], "refused")
        self.assertTrue(result["candidates"] or result["refusals"])
        self.assertEqual(self.world.adds, 0)

    def test_surface_target(self):
        self.other.isSolid = False
        result = self.world.run(**cut_args("Other"))
        self.assertIn("surface", result["refusals"][0])

    def test_linked_target(self):
        sub = Component(self.world, "Sub")
        self.world.body("Linked", component=sub)
        occurrence = Occurrence("Sub:1", sub)
        occurrence.isReferencedComponent = True
        self.world.root.allOccurrences.append(occurrence)
        result = self.world.run(operation="cut",
                                target_bodies=[{"occurrence_path": "Sub:1", "body_name": "Linked"}])
        self.assertTrue(any("linked" in text for text in result["refusals"]))

    def test_every_refusal_comes_at_once(self):
        self.world.design.activeEditObject = NS(objectType="adsk::fusion::BaseFeature", name="Base1")
        result = self.world.run(sketch_token="nope", **cut_args("Nope"))
        self.assertEqual(len(result["refusals"]), 3)

    def test_a_check_that_fails_after_the_add_still_names_the_feature(self):
        self.world.effects = {"Block": 10.0}
        with mock.patch.object(ep, "body_changes", side_effect=ValueError("bad read")):
            result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["status"], "suspect")
        self.assertEqual(result["feature"]["name"], "Extrude1")
        self.assertTrue(any("bad read" in w for w in result["warnings"]))

    def test_fusion_refusing_targets_on_a_cut_is_never_an_implicit_cut(self):
        original = self.world.root.extrudes.createInput

        def refusing(profile, operation):
            made = original(profile, operation)
            made.refuse_participants = True
            return made

        self.world.root.extrudes.createInput = refusing
        result = self.world.run(**cut_args("Block"))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.world.adds, 0)


class DryRun(FusionStubs):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.block = self.world.body("Block", low=(0, 0, 0), high=(10, 7, 2))
        self.far = self.world.body("Far", low=(50, 50, 0), high=(60, 60, 2))

    def plan(self, **overrides):
        result = self.world.run(dry_run=True, **cut_args("Block", extent={"type": "through_all"}, **overrides))
        self.assertEqual(result["status"], "planned", result.get("refusals"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.world.adds, 0)
        return result["plan"]

    def test_the_getter_answers(self):
        self.world.getter = [self.block, self.far]
        plan = self.plan()
        self.assertEqual(plan["plan_basis"], "participants")
        self.assertEqual(len(plan["would_intersect"]), 2)
        self.assertEqual(plan["in_path_not_targeted"], [{"occurrence_path": "root", "body_name": "Far"}])

    def test_a_silent_getter_falls_back_to_boxes(self):
        for silent in (None, []):
            self.world.getter = silent
            plan = self.plan()
            self.assertEqual(plan["plan_basis"], "bbox")
            self.assertEqual([r["body_name"] for r in plan["would_intersect"]], ["Block"])

    def test_a_raising_getter_falls_back_to_boxes(self):
        self.world.getter = RuntimeError("not fully defined")
        self.assertEqual(self.plan()["plan_basis"], "bbox")

    def test_a_target_out_of_the_path_warns(self):
        result = self.world.run(dry_run=True, **cut_args("Far", extent={"type": "through_all"}))
        self.assertTrue(any("do not look to be in" in w for w in result["warnings"]))

    def test_expected_volume_and_side(self):
        result = self.world.run(dry_run=True, **cut_args("Block"))
        self.assertEqual(result["plan"]["side"], "positive")
        self.assertEqual(result["plan"]["expected_volume"], result["extrude"]["expected_volume"])
        self.assertEqual(result["plan"]["abutment"], "clear")

    def test_a_dry_run_refuses_what_the_build_would(self):
        self.block.faces = [Face(self.block, (1, 1, 2.54 + 0.001), token="floor")]
        result = self.world.run(dry_run=True, **cut_args("Block"))
        self.assertEqual(result["status"], "refused")
        self.assertTrue(result["dry_run"])


class Abutment(FusionStubs):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.block = self.world.body("Block", low=(0, 0, 0), high=(10, 7, 3))

    def with_floor(self, z):
        self.block.faces = [Face(self.block, (1, 1, z), token="floor")]
        self.world.effects = {"Block": 10.0}
        return self.world.run(**cut_args("Block"))

    def test_a_skin_at_the_far_face_is_refused(self):
        result = self.with_floor(2.54 + 0.002)
        self.assertEqual(result["status"], "refused")
        self.assertIn("through_all or to_entity", result["refusals"][0])
        self.assertEqual(len(result["abutment"]), 1)
        self.assertEqual(self.world.adds, 0)

    def test_ending_on_the_face_is_allowed(self):
        self.assertEqual(self.with_floor(2.54)["status"], "built")

    def test_a_real_floor_is_allowed(self):
        self.assertEqual(self.with_floor(2.54 + 0.1)["status"], "built")

    def test_through_all_is_not_checked(self):
        self.block.faces = [Face(self.block, (1, 1, 2.54 + 0.002), token="floor")]
        self.world.effects = {"Block": 10.0}
        self.assertEqual(self.world.run(**cut_args("Block", extent={"type": "through_all"}))["status"], "built")

    def test_the_end_counts_the_start_offset(self):
        self.block.faces = [Face(self.block, (1, 1, 2.54 + 0.002 + 1.0), token="floor")]
        self.world.design.unitsManager = Units()
        result = self.world.run(start_offset=1.0, units="cm", **cut_args("Block", extent={"type": "distance",
                                                                                          "distance": 2.54}))
        self.assertEqual(result["status"], "refused")


class Join(FusionStubs):
    """EP-4 either way. ``join`` must work whether or not Fusion honors participantBodies."""

    def setUp(self):
        super().setUp()
        self.world = World()
        self.block = self.world.body("Block", low=(0, 0, 0), high=(10, 7, 2))
        self.other = self.world.body("Other", low=(0, 0, 0), high=(10, 7, 2))

    def join(self, **overrides):
        return self.world.run(operation="join", target_bodies=[{"occurrence_path": "root", "body_name": "Block"}],
                              **overrides)

    def test_fusion_honors_participants(self):
        self.world.effects = {"Block": 14.0}
        result = self.join()
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertEqual(result["join_route"], "participants")
        self.assertEqual(self.world.inputs[-1].named, [self.block])
        self.assertEqual(self.world.combined, [])

    def ignored(self):
        """The first add swallows Other, as an implicit join would; the new body touches nothing."""
        original = self.world.on_add

        def add(made, component):
            if made.operation == JOIN:
                self.world.effects, self.world.vanish = {"Block": 26.0}, ["Other"]
            else:
                self.world.effects, self.world.vanish = {}, []
            return original(made, component)

        self.world.on_add = add

    def test_fusion_ignores_participants(self):
        self.ignored()
        result = self.join()
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertEqual(result["join_route"], "new_body_combine")
        self.assertEqual(self.world.adds, 2)
        self.assertEqual(self.world.inputs[-1].operation, NEW_BODY)
        made = self.world.combined[0]
        self.assertEqual(made.operation, JOIN)
        self.assertIs(made.isKeepToolBodies, False)
        self.assertEqual(made.target.name, "Block")
        self.assertEqual(self.other.volume, 12.0)
        self.assertIn(self.other, self.world.root.bRepBodies)
        self.assertEqual(result["bodies"]["touched"][0]["ref"]["body_name"], "Block")
        self.assertEqual(result["bodies"]["created"], [])
        self.assertEqual(result["bodies"]["consumed"], [])

    def test_the_same_shape_either_way(self):
        keys = {"status", "ok", "feature", "bodies", "profile", "extrude", "faces", "join_route",
                "targets_unchanged", "in_path_not_targeted", "baseline_token"}
        self.world.effects = {"Block": 14.0}
        self.assertLessEqual(keys, set(self.join()))
        other = Join("test_fusion_ignores_participants")
        other.setUp()
        self.addCleanup(other.doCleanups)
        other.ignored()
        self.assertLessEqual(keys, set(other.join()))

    def test_a_join_that_grew_an_outside_body_is_caught(self):
        original = self.world.on_add

        def add(made, component):
            if made.operation == JOIN:
                self.world.effects, self.world.vanish = {"Other": 26.0}, ["Block"]
            else:
                self.world.effects, self.world.vanish = {}, []
            return original(made, component)

        self.world.on_add = add
        result = self.join()
        self.assertEqual(result["join_route"], "new_body_combine")
        self.assertEqual(self.other.volume, 12.0)

    def test_fusion_refuses_the_setter(self):
        self.world.refuse_participants = True
        result = self.join()
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertEqual(result["join_route"], "new_body_combine")
        self.assertEqual(self.world.adds, 1)
        self.assertTrue(any("would not take participantBodies" in w for w in result["warnings"]))

    def test_dry_run_reports_the_route(self):
        self.world.refuse_participants = True
        result = self.join(dry_run=True)
        self.assertEqual(result["plan"]["join_route"], "new_body_combine")
        self.assertEqual(self.world.adds, 0)

    def test_several_targets_all_go_into_the_combine(self):
        self.world.refuse_participants = True
        result = self.world.run(operation="join", target_bodies=[
            {"occurrence_path": "root", "body_name": "Block"}, {"occurrence_path": "root", "body_name": "Other"}])
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertEqual(len(self.world.combined[0].tools), 2)

    def test_faces_survive_the_combine_by_point(self):
        original = self.world.on_combine

        def combine(made, component):
            feature = original(made, component)
            self.block.faces = [Face(self.block, (1, 1, IN), token="merged-end")]
            return feature

        self.world.on_combine = combine
        self.world.refuse_participants = True
        result = self.join()
        self.assertEqual([f["token"] for f in result["faces"]["end"]], ["merged-end"])
        self.assertEqual(result["faces"]["end"][0]["body"]["body_name"], "Block")
        self.assertEqual(result["faces"]["start"], [])

    def test_a_failed_combine_takes_the_new_body_back(self):
        self.world.refuse_participants = True
        self.world.on_combine = mock.Mock(side_effect=RuntimeError("Combine failed"))
        result = self.join()
        self.assertEqual(result["status"], "refused")
        self.assertIn("Combine failed", result["refusals"][0])
        self.assertEqual([b.name for b in self.world.root.bRepBodies], ["Block", "Other"])

    def test_a_combine_cannot_cross_components(self):
        sub = Component(self.world, "Sub")
        self.world.body("SubBlock", component=sub)
        self.world.root.allOccurrences.append(Occurrence("Sub:1", sub))
        self.world.refuse_participants = True
        result = self.world.run(operation="join",
                                target_bodies=[{"occurrence_path": "Sub:1", "body_name": "SubBlock"}])
        self.assertEqual(result["status"], "refused")
        self.assertIn("cannot cross components", result["refusals"][0])
        self.assertEqual(self.world.adds, 0)

    def test_collateral_on_the_combine_route_deletes_both_features(self):
        self.world.refuse_participants = True
        original = self.world.on_combine
        made = {}

        def combine(combine_input, component):
            feature = original(combine_input, component)
            self.other.volume = 5.0
            made["feature"] = feature
            return feature

        self.world.on_combine = combine
        result = self.join()
        self.assertEqual(result["status"], "undone")
        self.assertEqual(result["collateral"], ["root/Other"])
        self.assertTrue(made["feature"].deleted)
        self.assertEqual(self.other.volume, 12.0)

    def test_an_add_that_loses_its_participants_is_refused(self):
        job = self.world.job(operation="join", target_bodies=[{"occurrence_path": "root", "body_name": "Block"}])
        job._open()
        job._plan()
        self.world.refuse_participants = True
        outcome = job._add("join", job.sign)
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(self.world.adds, 0)

    def test_keep_does_not_retry(self):
        self.ignored()
        result = self.join(on_collateral="keep")
        self.assertEqual(result["status"], "suspect")
        self.assertEqual(result["join_route"], "participants")


class Occurrences(FusionStubs):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.sub = Component(self.world, "Sub")
        self.sub_block = self.world.body("SubBlock", component=self.sub, low=(0, 0, 0), high=(5, 5, 2))
        self.one = Occurrence("Sub:1", self.sub, shift=(25.4, 0, 0))
        self.two = Occurrence("Sub:2", self.sub, shift=(-25.4, 0, 0))
        self.world.root.allOccurrences.extend([self.one, self.two])
        self.sub_ref = {"occurrence_path": "Sub:1", "body_name": "SubBlock"}

    def sketch_in_sub(self):
        self.world.sketch.parentComponent = self.sub
        self.world.design.tokens["sk"] = [self.world.sketch]

    def test_a_root_cut_reaches_through_the_proxy(self):
        self.world.effects = {"SubBlock": 10.0}
        result = self.world.run(operation="cut", target_bodies=[self.sub_ref])
        named = self.world.inputs[-1].named[0]
        self.assertIs(named.assemblyContext, self.one)
        self.assertIsNone(self.world.inputs[-1].creationOccurrence)
        self.assertEqual(result["bodies"]["outside_target"], [])

    def test_the_other_instance_is_not_collateral_but_warns(self):
        self.world.effects = {"SubBlock": 10.0}
        result = self.world.run(operation="cut", target_bodies=[self.sub_ref])
        self.assertEqual(result["status"], "built", result["warnings"])
        roles = {r["ref"]["occurrence_path"]: r["role"] for r in result["bodies"]["touched"]}
        self.assertEqual(roles, {"Sub:1": "target", "Sub:2": "other_instance"})
        self.assertTrue(any("other instances" in w for w in result["warnings"]))

    def test_a_root_sketch_needs_no_occurrence(self):
        result = self.world.run(occurrence_path="Sub:1")
        self.assertIn("this sketch is in root", result["refusals"][0])

    def test_a_sub_sketch_with_two_instances_must_say_which(self):
        self.sketch_in_sub()
        result = self.world.run()
        self.assertIn("2 instances", result["refusals"][0])

    def test_a_sub_sketch_feature_lives_in_the_component(self):
        self.sketch_in_sub()
        result = self.world.run(occurrence_path="Sub:1", body_name="Boss")
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertIs(self.sub.extrudes.inputs[0].creationOccurrence, self.one)
        self.assertEqual(self.world.root.extrudes.inputs, [])
        self.assertEqual(result["feature"]["component"], "Sub")

    def test_plane_and_measure_go_through_the_transform(self):
        self.sketch_in_sub()
        job = self.world.job(occurrence_path="Sub:1")
        result = job.run()
        self.assertEqual(job.origin, (25.4, 0.0, 0.0))
        self.assertAlmostEqual(result["extrude"]["measured_depth"], 1.0)

    def test_the_plane_is_read_in_model_space_not_sketch_space(self):
        def on_a_side_plane(_sketch, point):  # sketch z is model +y, 5 cm from the origin
            return vec((point.x, point.z + 5.0, -point.y))

        with mock.patch.object(Sketch, "sketchToModelSpace", on_a_side_plane):
            job = World([Profile()]).job(direction=[0, 1, 0])
            result = job.run()
        self.assertNotIn("off the sketch normal", str(result.get("refusals")))
        self.assertEqual(job.origin, (0.0, 5.0, 0.0))
        self.assertEqual(tuple(round(c, 9) for c in job.normal), (0.0, 1.0, 0.0))

    def test_a_volume_counts_one_instance(self):
        self.sketch_in_sub()
        result = self.world.run(occurrence_path="Sub:1")
        self.assertEqual(len(result["bodies"]["created"]), 2)
        self.assertEqual(result["extrude"]["volume_check"], "ok")

    def test_a_sub_feature_names_native_bodies(self):
        self.sketch_in_sub()
        self.world.effects = {"SubBlock": 10.0}
        self.world.run(operation="cut", target_bodies=[self.sub_ref])
        self.assertIs(self.sub.extrudes.inputs[-1].named[0], self.sub_block)

    def test_owner_rule_refuses_another_component(self):
        self.sketch_in_sub()
        self.world.body("Block")
        result = self.world.run(operation="cut", occurrence_path="Sub:1",
                                target_bodies=[{"occurrence_path": "root", "body_name": "Block"}])
        self.assertEqual(result["status"], "refused")
        self.assertIn("cannot reach", " ".join(result["refusals"]))

    def test_targets_go_through_the_sketchs_instance(self):
        self.sketch_in_sub()
        result = self.world.run(operation="cut", occurrence_path="Sub:2", target_bodies=[self.sub_ref])
        self.assertIn("name the targets through 'Sub:2'", result["refusals"][0])

    def test_an_occurrence_of_something_else(self):
        self.sketch_in_sub()
        other = Occurrence("Other:1", Component(self.world, "Other"))
        self.world.root.allOccurrences.append(other)
        result = self.world.run(occurrence_path="Other:1")
        self.assertIn("is not an instance of", result["refusals"][0])

    def test_a_containing_point_is_world(self):
        self.sketch_in_sub()
        with mock.patch.object(ep, "describe_profiles",
                               return_value=[row(0, 4.0 / IN ** 2, contains=True)]) as described:
            result = self.world.run(occurrence_path="Sub:1", profile_select="containing_point",
                                    containing_point=[11.0, 1.0, 0.0])
        self.assertEqual(result["status"], "built", result["warnings"])
        self.assertAlmostEqual(self.world.sketch.queries[0][0], 1.0 * IN)
        self.assertEqual(len(described.call_args_list[1].args), 3)


class Profiles_(FusionStubs):
    def test_several_profiles_go_in_a_collection(self):
        world = World([Profile(1.0), Profile(2.0)])
        rows = [row(0, 1.0, contains=True), row(1, 2.0, contains=False)]
        swapped = [row(0, 1.0, contains=False), row(1, 2.0, contains=True)]
        answers = iter([rows, rows, swapped, rows, rows, swapped])
        with mock.patch.object(ep, "describe_profiles", side_effect=lambda *a: next(answers)):
            result = world.run(profile_select="containing_points", containing_points=[[0, 0, 0], [1, 1, 0]],
                               dry_run=True)
        self.assertEqual(result["profile"]["count"], 2)
        self.assertEqual(result["profile"]["area"], 3.0)
        self.assertEqual(len(world.inputs[0].profile), 2)

    def test_only_with_several_refuses_without_adding(self):
        world = World([Profile(1.0), Profile(2.0)])
        result = world.run()
        self.assertEqual(result["status"], "refused")
        self.assertEqual(len(result["profiles"]), 2)
        self.assertEqual(world.adds, 0)

    def test_most_loops_takes_the_plate_not_the_hole(self):
        small, plate = Profile(1.0), Profile(50.0, loops=3)
        world = World([small, plate])
        world.run(profile_select="most_loops", dry_run=True)
        self.assertIs(world.inputs[0].profile, plate)


class Schema(unittest.TestCase):
    def definition(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "extrude_profile")

    def test_strict(self):
        schema = self.definition()["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["extent"]["additionalProperties"])
        self.assertEqual(schema["required"], ["operation", "extent"])

    def test_schema_and_parser_take_the_same_keys(self):
        schema = self.definition()["inputSchema"]
        self.assertEqual(set(schema["properties"]), ep._KNOWN)
        extent_keys = set().union(*ep._EXTENT_KEYS.values()) | {"type"}
        self.assertEqual(set(schema["properties"]["extent"]["properties"]), extent_keys)

    def test_no_keywords_clients_reject(self):
        text = json.dumps(self.definition())
        for keyword in ("minItems", "minLength", "uniqueItems", "anyOf", "oneOf"):
            self.assertNotIn(keyword, text)

    def test_dry_run_default_is_stated_false(self):
        self.assertIn("default false", self.definition()["inputSchema"]["properties"]["dry_run"]["description"])


if __name__ == "__main__":
    unittest.main()
