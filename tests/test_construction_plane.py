"""construction_plane: schema, validation, each mode's prediction, the measure and
rebuild paths on a fake that gets the sign wrong, reuse, anchors, and the Fusion
layer's occurrence handling (no Fusion needed)."""

import json
import math
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import construction_plane as cp
from fusion_bridge import tool_surface
from fusion_bridge.common import bodies, geometry
from fusion_bridge.common.entity_refs import Resolution

UP = (0.0, 0.0, 1.0)


def args(mode="offset", **overrides):
    base = {"mode": mode, "name": "Boss plane", "units": "cm"}
    if mode == "offset":
        base.update({"from": "xy", "offset": 1})
    elif mode == "angle":
        base.update({"from": "xy", "axis": {"axis": "x"}, "angle_deg": 30})
    elif mode == "three_point":
        base["points"] = [{"point": [0, 0, 0]}, {"point": [1, 0, 0]}, {"point": [0, 1, 0]}]
    else:
        base.update({"point": [0, 0, 2], "normal": [0, 0, 1]})
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def options(mode="offset", **overrides):
    opts, err = cp.validate_args(args(mode, **overrides))
    assert err is None, err
    return opts


# -- Fakes -----------------------------------------------------------------------


class Plane:
    healthState = 0
    errorOrWarningMessage = ""
    timelineObject = None
    objectType = "adsk::fusion::ConstructionPlane"

    def __init__(self, layer, origin, normal, name="Plane1"):
        self.layer, self.origin, self.normal, self.name = layer, origin, normal, name

    def deleteMe(self):
        self.layer.deleted.append(self)
        return True


class Timeline:
    count = markerPosition = 0

    def __iter__(self):
        return iter(())


class Root:
    allOccurrences = ()
    bRepBodies = ()


class Design:
    designType = 1
    activeEditObject = None
    parentDocument = None

    def __init__(self):
        self.timeline, self.rootComponent = Timeline(), Root()


class Layer:
    """Fusion as planning and building see it.

    *wrong_sign* builds a value the other way first time; *flip_normal* gives the
    normal opposite to the one asked; *nowhere* builds somewhere unrelated.
    """

    component_name = "root"
    scene = None

    def __init__(self, wrong_sign=False, always_wrong=False, flip_normal=False, nowhere=False, refuse=None,
                 existing=(), boxes=()):
        self.wrong_sign, self.always_wrong, self.flip_normal = wrong_sign, always_wrong, flip_normal
        self.nowhere, self.refuse, self.existing, self.boxes = nowhere, refuse, list(existing), list(boxes)
        self.adds, self.deleted, self.anchored = [], [], []
        self.prediction = None

    # Planning
    def evaluate(self, value, kind):
        return float(value["number"]) if value["number"] is not None else 2.0

    def root_plane(self, key):
        normals = {"xy": UP, "xz": (0.0, -1.0, 0.0), "yz": (1.0, 0.0, 0.0)}
        return {"entity": key, "point": (0.0, 0.0, 0.0), "normal": normals[key],
                "row": {"kind": "root_plane", "name": key.upper(), "token": None, "visible": True}}

    def plane_by_token(self, token):
        if token != "known":
            raise ValueError("'from_plane_token' does not name one construction plane")
        return {"entity": token, "point": (0.0, 0.0, 5.0), "normal": UP,
                "row": {"kind": "plane", "name": "P", "token": token, "visible": True}}

    def face_parent(self, face):
        return None if face == "curved" else {
            "entity": face, "point": (4.0, 0.0, 0.0), "normal": (1.0, 0.0, 0.0),
            "row": {"kind": "face", "name": "Block", "token": "f", "visible": True}}

    def root_axis(self, key):
        return {"entity": key, "point": (0.0, 0.0, 0.0),
                "direction": {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": UP}[key]}

    def edge_line(self, edge):
        return None if edge == "arc" else {"entity": edge, "point": (0.0, 3.0, 0.0), "direction": (2.0, 0.0, 0.0)}

    def vertex_point(self, vertex):
        return vertex

    def sketch_point(self, token):
        return token, (0.0, 5.0, 0.0)

    def body_boxes(self):
        return self.boxes

    def planes(self):
        return [{"name": p.name, "origin": p.origin, "normal": p.normal, "u": (1, 0, 0), "v": (0, 1, 0), "token": "old"}
                for p in self.existing]

    def has_plane_named(self, name):
        return any(p.name == name for p in self.existing)

    # Building
    def anchors(self, points, name, show):
        if self.refuse == "anchors":
            raise RuntimeError("sketch refused")
        self.anchored.append((points, name, show))
        return {"points": [f"anchor{i}" for i in range(len(points))],
                "rows": [{"sketch": f"{name}_anchors", "token": f"t{i}", "visible": show} for i in range(len(points))],
                "unused": []}

    def add(self, definition, flipped):
        self.adds.append((definition["kind"], flipped, list(definition["entities"])))
        if self.refuse == "add" or (self.refuse == "second" and len(self.adds) == 2):
            raise RuntimeError("3 : not supported")
        plan = self.prediction
        if self.nowhere:
            return Plane(self, (9.0, 9.0, 9.0), (0.0, 1.0, 0.0))
        wrong = self.always_wrong or (self.wrong_sign and not flipped)
        origin, normal = (plan["mirror"] if wrong and plan["mirror"] else (plan["origin"], plan["normal"]))
        if self.flip_normal:
            normal = geometry.scaled(normal, -1.0)
        return Plane(self, origin, normal)

    def read(self, plane):
        return {"origin": plane.origin, "normal": plane.normal, "u": (1.0, 0.0, 0.0), "v": (0.0, 1.0, 0.0),
                "token": "tok", "name": plane.name}

    def set_name(self, plane, name):
        plane.name = name
        return name


def go(layer, mode="offset", **overrides):
    """Run with the fake building from the plan's own prediction."""
    opts = options(mode, **overrides)
    planner = cp.PLANNERS[mode]

    def capture(o, l):
        layer.prediction = planner(o, l)
        return layer.prediction

    with mock.patch.dict(cp.PLANNERS, {mode: capture}):
        return cp.run(Design(), None, opts, layer)


# -- Schema and arguments --------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "construction_plane")["inputSchema"]

    def test_required_and_strict(self):
        self.assertEqual(self.schema()["required"], ["mode", "name"])
        self.assertFalse(self.schema()["additionalProperties"])

    def test_every_key_is_validated(self):
        self.assertEqual(set(self.schema()["properties"]), cp._COMMON_KEYS | cp._ALL_MODE_KEYS)


class ValidationTests(unittest.TestCase):
    def error(self, mode="offset", **overrides):
        return cp.validate_args(args(mode, **overrides))[1]

    def test_defaults(self):
        opts = options()
        self.assertFalse(opts["dry_run"])
        self.assertTrue(opts["reuse_existing"])
        self.assertFalse(opts["show_anchors"])

    def test_every_mode_is_valid_as_built_by_these_helpers(self):
        for mode in cp.MODES:
            with self.subTest(mode=mode):
                self.assertIsNone(self.error(mode))

    def test_name_required(self):
        self.assertIn("'name'", self.error(name=None))

    def test_exactly_one_parent(self):
        self.assertIn("exactly one", self.error(**{"from": None}))
        self.assertIn("exactly one", self.error(from_plane_token="t"))

    def test_offset_needs_exactly_one_value(self):
        self.assertIn("is required", self.error(offset=None))
        self.assertIn("not both", self.error(offset_expression="1 in"))

    def test_bare_expression_refused(self):
        self.assertIn("no unit", self.error(offset=None, offset_expression="2"))
        self.assertIn("no unit", self.error("angle", angle_deg=None, angle_expression="30"))

    def test_a_key_from_another_mode(self):
        self.assertIn("does not apply", self.error(point=[0, 0, 0]))
        self.assertIn("does not apply", self.error("point_normal", offset=1))

    def test_zero_vectors(self):
        self.assertIn("zero vector", self.error("point_normal", normal=[0, 0, 0]))
        self.assertIn("zero vector", self.error(toward=[0, 0, 0]))

    def test_points_shape(self):
        self.assertIn("exactly three", self.error("three_point", points=[{"point": [0, 0, 0]}]))
        two_keys = {"point": [0, 0, 0], "sketch_point_token": "t"}
        self.assertIn("exactly one", self.error("three_point", points=[two_keys] * 3))
        self.assertIn("three finite", self.error("three_point", points=[{"point": [0, 0]}] * 3))

    def test_ref_kinds(self):
        self.assertIn("kind 'face'", self.error(**{"from": None}, from_ref={"token": "t", "kind": "edge"}))
        bad = {"ref": {"token": "t", "kind": "face"}}
        self.assertIn("kind 'vertex'", self.error("three_point", points=[bad] * 3))
        self.assertIn("kind 'edge'", self.error("angle", axis={"edge": {"token": "t", "kind": "face"}}))

    def test_a_point_ref_takes_its_kind_from_the_slot(self):
        opts = options(**{"from": None}, from_ref={"point": [1, 0, 0]})
        self.assertEqual(opts["parent"]["ref"]["kind"], "face")

    def test_axis_shape(self):
        self.assertIn("exactly one", self.error("angle", axis={}))
        self.assertIn("must be one of", self.error("angle", axis={"axis": "w"}))
        self.assertIn("'axis' is required", self.error("angle", axis=None))

    def test_a_sub_component_needs_its_occurrence(self):
        self.assertIn("occurrence_path", self.error(component="Sub"))
        self.assertIsNone(self.error(component="Sub", occurrence_path="Sub:1"))

    def test_root_occurrence_is_no_occurrence(self):
        self.assertIsNone(options(occurrence_path="root")["occurrence_path"])

    def test_units_convert_points(self):
        opts = options("point_normal", units="in", point=[1, 0, 0])
        self.assertAlmostEqual(opts["point"][0], 2.54)

    def test_bad_values(self):
        for overrides in ({"mode": "tangent"}, {"units": "ft"}, {"dry_run": "no"}, {"reuse_existing": 1}, {"nope": 1}):
            with self.subTest(overrides=overrides):
                self.assertIsNotNone(self.error(**overrides))

    def test_not_an_object(self):
        self.assertIsNotNone(cp.validate_args("x")[1])


# -- Pure helpers ----------------------------------------------------------------


class HelperTests(unittest.TestCase):
    def test_negated(self):
        self.assertEqual(cp.negated({"text": "1.5 in", "number": 1.5}), {"text": "-(1.5 in)", "number": -1.5})
        self.assertEqual(cp.negated({"text": "d1 * 2", "number": None}), {"text": "-(d1 * 2)", "number": None})

    def test_reach_floor_with_no_bodies(self):
        self.assertEqual(cp.reach_for((0, 0, 0), []), cp.REACH_FLOOR_CM)

    def test_reach_floor_for_a_tiny_body(self):
        self.assertEqual(cp.reach_for((0, 0, 0), [((0, 0, 0), (0.1, 0.1, 0.1))]), cp.REACH_FLOOR_CM)

    def test_reach_is_the_nearest_box_diagonal(self):
        near, far = ((0, 0, 0), (3, 4, 0)), ((100, 0, 0), (200, 0, 0))
        self.assertAlmostEqual(cp.reach_for((1, 1, 0), [far, near]), 5.0)

    def test_judge(self):
        plan = {"origin": (0, 0, 1), "normal": UP, "mirror": ((0, 0, -1), UP)}
        self.assertEqual(cp.judge(plan, {"origin": (3, 3, 1), "normal": (0, 0, -1)}), "ok")
        self.assertEqual(cp.judge(plan, {"origin": (0, 0, -1), "normal": UP}), "flip")
        self.assertEqual(cp.judge(plan, {"origin": (0, 0, 7), "normal": UP}), "off")
        self.assertEqual(cp.judge({**plan, "mirror": None}, {"origin": (0, 0, -1), "normal": UP}), "off")

    def test_sign_row(self):
        row = cp.sign_row((0, 0, 1), (0, 0, -1))
        self.assertTrue(row["flipped_vs_asked"])
        self.assertFalse(cp.sign_row((0, 0, 1), (0, 0, 2))["flipped_vs_asked"])

    def test_a_plane_holding_an_anchor_is_not_reused(self):
        plan = {"origin": (0, 0, 1), "normal": UP}
        scaffold = {"name": "Boss_anchors_2_plane", "origin": (0, 0, 1), "normal": UP}
        named_alike = {"name": "Boss_plane", "origin": (0, 0, 1), "normal": UP}
        self.assertIsNone(cp.find_existing([scaffold], plan))
        self.assertIs(cp.find_existing([scaffold, named_alike], plan), named_alike)


# -- Offset ----------------------------------------------------------------------


class OffsetTests(unittest.TestCase):
    def test_along_the_parent_normal_by_default(self):
        payload = go(Layer())
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertEqual(payload["plane"]["origin"], [0, 0, 1])
        self.assertEqual(payload["plane"]["name"], "Boss plane")
        self.assertEqual(payload["parent"]["name"], "XY")
        json.dumps(payload)

    def test_toward_picks_the_side(self):
        layer = Layer()
        payload = go(layer, toward=[0, 0, -1])
        self.assertEqual(payload["plane"]["origin"], [0, 0, -1])

    def test_toward_needs_only_a_component_along_the_normal(self):
        self.assertEqual(go(Layer(), toward=[5, 5, -0.1])["plane"]["origin"], [0, 0, -1])

    def test_toward_in_the_plane_is_refused(self):
        payload = go(Layer(), toward=[1, 0, 0])
        self.assertEqual(payload["status"], "refused")
        self.assertIn("neither side", payload["refusals"][0])

    def test_wrong_sign_is_rebuilt(self):
        layer = Layer(wrong_sign=True)
        payload = go(layer, toward=[0, 0, -1])
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertTrue(payload["rebuilt"])
        self.assertEqual(payload["plane"]["origin"], [0, 0, -1])
        self.assertEqual([flipped for _, flipped, _ in layer.adds], [False, True])
        self.assertEqual(len(layer.deleted), 1)

    def test_still_wrong_after_a_rebuild_is_suspect(self):
        layer = Layer(always_wrong=True)
        payload = go(layer)
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(len(layer.adds), 2)

    def test_a_failed_rebuild_is_refused(self):
        payload = go(Layer(wrong_sign=True, refuse="second"))
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusals"], ["3 : not supported"])

    def test_a_delete_that_fails_stops_the_rebuild(self):
        layer = Layer(wrong_sign=True)
        with mock.patch.object(Plane, "deleteMe", lambda self: False):
            payload = go(layer)
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(len(layer.adds), 1)

    def test_zero_offset_needs_no_rebuild(self):
        layer = Layer(wrong_sign=True)
        self.assertFalse(go(layer, offset=0)["rebuilt"])

    def test_negative_offset_runs_against_toward(self):
        self.assertEqual(go(Layer(), offset=-1, toward=[0, 0, 1])["plane"]["origin"], [0, 0, -1])

    def test_expression_is_evaluated(self):
        self.assertEqual(go(Layer(), offset=None, offset_expression="d1")["plane"]["origin"], [0, 0, 2])

    def test_from_a_construction_plane(self):
        payload = go(Layer(), **{"from": None}, from_plane_token="known")
        self.assertEqual(payload["plane"]["origin"], [0, 0, 6])
        self.assertEqual(payload["parent"]["kind"], "plane")

    def test_unknown_plane_token(self):
        self.assertIn("does not name", go(Layer(), **{"from": None}, from_plane_token="x")["refusals"][0])

    def test_from_a_face(self):
        with mock.patch.object(cp, "resolve", return_value=Resolution("flat", "face", "token", [], None)):
            payload = go(Layer(), **{"from": None}, from_ref={"token": "t"}, toward=[1, 0, 0])
        self.assertEqual(payload["plane"]["origin"], [5, 0, 0])
        self.assertEqual(payload["resolved"], [{"role": "from_ref", "kind": "face", "resolved_by": "token"}])

    def test_a_curved_face_is_refused(self):
        with mock.patch.object(cp, "resolve", return_value=Resolution("curved", "face", "token", [], None)):
            payload = go(Layer(), **{"from": None}, from_ref={"token": "t"})
        self.assertIn("not a planar face", payload["refusals"][0])

    def test_a_ref_that_will_not_resolve_is_refused(self):
        lost = Resolution(None, None, None, [], {"code": "not_found", "error": "nothing matches"})
        with mock.patch.object(cp, "resolve", return_value=lost):
            payload = go(Layer(), **{"from": None}, from_ref={"token": "t"})
        self.assertEqual(payload["refusals"], ["from_ref: nothing matches"])

    def test_no_anchors(self):
        layer = Layer()
        self.assertEqual(go(layer)["anchors"], [])
        self.assertEqual(layer.anchored, [])


# -- Angle -----------------------------------------------------------------------


class AngleTests(unittest.TestCase):
    def test_right_hand_about_x(self):
        payload = go(Layer(), "angle", angle_deg=30)
        normal = payload["sign"]["asked_normal"]
        self.assertAlmostEqual(normal[1], -0.5)
        self.assertAlmostEqual(normal[2], math.cos(math.radians(30)), places=6)

    def test_wrong_sign_is_rebuilt_at_the_negative(self):
        layer = Layer(wrong_sign=True)
        payload = go(layer, "angle", angle_deg=30)
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertTrue(payload["rebuilt"])
        self.assertAlmostEqual(payload["plane"]["normal"][1], -0.5)
        self.assertEqual(len(layer.deleted), 1)

    def test_the_built_normal_may_face_either_way(self):
        payload = go(Layer(flip_normal=True), "angle", angle_deg=30)
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertFalse(payload["rebuilt"])
        self.assertTrue(payload["sign"]["flipped_vs_asked"])

    def test_no_rebuild_where_both_signs_are_one_plane(self):
        for angle in (0, 90, 180):
            with self.subTest(angle=angle):
                self.assertFalse(go(Layer(wrong_sign=True), "angle", angle_deg=angle)["rebuilt"])

    def test_still_off_is_suspect(self):
        payload = go(Layer(nowhere=True), "angle")
        self.assertEqual(payload["status"], "suspect")
        self.assertTrue(any("not where it was asked" in w for w in payload["warnings"]))

    def test_axis_across_the_plane_is_refused(self):
        payload = go(Layer(), "angle", axis={"axis": "z"})
        self.assertIn("parallel", payload["refusals"][0])

    def test_edge_axis(self):
        with mock.patch.object(cp, "resolve", return_value=Resolution("line", "edge", "token", [], None)):
            payload = go(Layer(), "angle", axis={"edge": {"token": "t"}})
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertEqual(payload["plane"]["origin"], [0, 3, 0])
        self.assertEqual(payload["axis_direction"], [1, 0, 0])

    def test_a_curved_edge_is_refused(self):
        with mock.patch.object(cp, "resolve", return_value=Resolution("arc", "edge", "token", [], None)):
            payload = go(Layer(), "angle", axis={"edge": {"token": "t"}})
        self.assertIn("not a straight edge", payload["refusals"][0])

    def test_definition_order_is_axis_then_parent(self):
        layer = Layer()
        go(layer, "angle")
        self.assertEqual(layer.adds[0][2], ["x", "xy"])


# -- Through points --------------------------------------------------------------


class ThreePointTests(unittest.TestCase):
    def test_coordinates_become_anchors_in_place(self):
        layer = Layer()
        payload = go(layer, "three_point")
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertEqual(layer.adds[0][2], ["anchor0", "anchor1", "anchor2"])
        self.assertEqual(len(payload["anchors"]), 3)
        self.assertEqual(payload["anchors"][1], {"sketch": "Boss plane_anchors", "token": "t1", "point": [1, 0, 0],
                                                 "visible": False})
        self.assertEqual(payload["parent"]["kind"], "anchors")
        self.assertEqual(payload["parent"]["name"], "Boss plane_anchors")

    def test_a_vertex_and_a_sketch_point_are_used_directly(self):
        layer = Layer()
        spot = (2.0, 0.0, 0.0)
        with mock.patch.object(cp, "resolve", return_value=Resolution(spot, "vertex", "token", [], None)):
            payload = go(layer, "three_point", points=[{"point": [0, 0, 0]}, {"ref": {"token": "v"}},
                                                       {"sketch_point_token": "s"}])
        self.assertEqual(layer.adds[0][2], ["anchor0", spot, "s"])
        self.assertEqual(len(payload["anchors"]), 1)
        self.assertEqual(payload["anchors"][0]["point"], [0, 0, 0])

    def test_no_coordinates_no_anchors(self):
        layer = Layer()
        with mock.patch.object(cp, "resolve",
                               side_effect=[Resolution(p, "vertex", "token", [], None)
                                            for p in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))]):
            payload = go(layer, "three_point", points=[{"ref": {"token": "v"}}] * 3)
        self.assertEqual(layer.anchored, [])
        self.assertEqual(payload["status"], "built", payload["warnings"])

    def test_collinear_refused_before_any_anchor(self):
        layer = Layer()
        payload = go(layer, "three_point", points=[{"point": [0, 0, 0]}, {"point": [1, 0, 0]}, {"point": [2, 0, 0]}])
        self.assertIn("collinear", payload["refusals"][0])
        self.assertEqual(layer.anchored, [])

    def test_coincident_refused(self):
        same = {"point": [1, 1, 1]}
        self.assertIn("coincident", go(Layer(), "three_point", points=[same, same, {"point": [0, 0, 0]}])["refusals"][0])

    def test_winding_gives_the_asked_normal(self):
        self.assertEqual(go(Layer(), "three_point")["sign"]["asked_normal"], [0, 0, 1])

    def test_a_refused_add_keeps_and_lists_the_anchors(self):
        payload = go(Layer(refuse="add"), "three_point")
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(len(payload["anchors"]), 3)
        self.assertTrue(any("anchors stay" in w for w in payload["warnings"]))

    def test_anchors_that_fail_are_a_refusal(self):
        payload = go(Layer(refuse="anchors"), "three_point")
        self.assertEqual(payload["status"], "refused")
        self.assertIn("sketch refused", payload["refusals"][0])

    def test_unused_anchor_sketch_is_reported(self):
        layer = Layer()
        place = layer.anchors

        def with_spare(*a):
            return {**place(*a), "unused": ["spare"]}

        layer.anchors = with_spare
        self.assertTrue(any("spare" in w for w in go(layer, "three_point")["warnings"]))


class PointNormalTests(unittest.TestCase):
    NORMAL = [0, 0.5, 0.866]

    def test_builds_through_the_point(self):
        layer = Layer()
        payload = go(layer, "point_normal", point=[1, 2, 3], normal=self.NORMAL)
        self.assertEqual(payload["status"], "built", payload["warnings"])
        points = layer.anchored[0][0]
        self.assertEqual(points[0], (1.0, 2.0, 3.0))
        normal = geometry.unit(tuple(self.NORMAL))
        for point in points:
            self.assertAlmostEqual(geometry.dot(geometry.sub(point, points[0]), normal), 0.0)

    def test_winding_matches_the_normal(self):
        layer = Layer()
        go(layer, "point_normal", normal=self.NORMAL)
        a, b, c = layer.anchored[0][0]
        made = geometry.unit(geometry.cross(geometry.sub(b, a), geometry.sub(c, a)))
        for x, y in zip(made, geometry.unit(tuple(self.NORMAL))):
            self.assertAlmostEqual(x, y)

    def test_reach_comes_from_the_nearest_body(self):
        layer = Layer(boxes=[((0, 0, 0), (30, 40, 0))])
        go(layer, "point_normal")
        a, b, _ = layer.anchored[0][0]
        self.assertAlmostEqual(math.dist(a, b), 50.0)

    def test_a_flipped_normal_is_reported_not_rebuilt(self):
        layer = Layer(flip_normal=True)
        payload = go(layer, "point_normal", normal=self.NORMAL)
        self.assertEqual(payload["status"], "built", payload["warnings"])
        self.assertTrue(payload["sign"]["flipped_vs_asked"])
        self.assertEqual(len(layer.adds), 1)
        self.assertEqual(layer.deleted, [])

    def test_a_plane_elsewhere_is_suspect_and_kept(self):
        layer = Layer(nowhere=True)
        payload = go(layer, "point_normal")
        self.assertEqual(payload["status"], "suspect")
        self.assertEqual(layer.deleted, [])

    def test_show_anchors(self):
        layer = Layer()
        payload = go(layer, "point_normal", show_anchors=True)
        self.assertTrue(payload["anchors"][0]["visible"])

    def test_anchors_named_from_the_plane_and_off_by_default(self):
        layer = Layer()
        payload = go(layer, "point_normal")
        self.assertEqual(layer.anchored[0][1], "Boss plane")
        self.assertFalse(any(row["visible"] for row in payload["anchors"]))


# -- Reuse, names, dry run, preflight --------------------------------------------


class ReuseTests(unittest.TestCase):
    def layer(self, name="Old", origin=(7.0, 7.0, 1.0), normal=(0.0, 0.0, -1.0), **kwargs):
        layer = Layer(**kwargs)
        layer.existing = [Plane(layer, origin, normal, name)]
        return layer

    def test_hit_builds_nothing(self):
        layer = self.layer()
        payload = go(layer)
        self.assertEqual(payload["status"], "built")
        self.assertTrue(payload["reused"])
        self.assertEqual(layer.adds, [])
        self.assertEqual(payload["plane"]["name"], "Old")
        self.assertTrue(payload["sign"]["flipped_vs_asked"])
        self.assertTrue(any("keeps its name" in w for w in payload["warnings"]))

    def test_hit_adds_no_anchors(self):
        layer = self.layer(origin=(0.0, 0.0, 2.0), normal=UP)
        payload = go(layer, "point_normal")
        self.assertTrue(payload["reused"])
        self.assertEqual(layer.anchored, [])
        self.assertEqual(payload["anchors"], [])

    def test_miss_builds(self):
        layer = self.layer(origin=(0.0, 0.0, 9.0))
        self.assertFalse(go(layer)["reused"])
        self.assertEqual(len(layer.adds), 1)

    def test_off_builds_a_second(self):
        layer = self.layer()
        self.assertFalse(go(layer, reuse_existing=False)["reused"])

    def test_a_used_name_elsewhere_is_refused(self):
        layer = self.layer(name="Boss plane", origin=(0.0, 0.0, 9.0))
        payload = go(layer)
        self.assertEqual(payload["status"], "refused")
        self.assertIn("already exists", payload["refusals"][0])
        self.assertEqual(layer.adds, [])

    def test_a_used_name_on_the_same_plane_is_reused(self):
        payload = go(self.layer(name="Boss plane"))
        self.assertTrue(payload["reused"])
        self.assertFalse(any("keeps its name" in w for w in payload["warnings"]))

    def test_a_used_name_without_reuse_is_refused(self):
        self.assertEqual(go(self.layer(name="Boss plane"), reuse_existing=False)["status"], "refused")


class DryRunTests(unittest.TestCase):
    def test_plans_and_touches_nothing(self):
        layer = Layer()
        payload = go(layer, "point_normal", dry_run=True, point=[0, 0, 2])
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(layer.adds, [])
        self.assertEqual(layer.anchored, [])
        plan = payload["plan"]
        self.assertEqual(plan["origin"], [0, 0, 2])
        self.assertEqual(plan["normal"], [0, 0, 1])
        self.assertEqual(len(plan["anchors"]), 3)
        self.assertFalse(plan["reused"])
        self.assertIsNone(payload["baseline_token"])

    def test_reports_a_reuse(self):
        layer = Layer()
        layer.existing = [Plane(layer, (0.0, 0.0, 1.0), UP, "Old")]
        plan = go(layer, dry_run=True)["plan"]
        self.assertTrue(plan["reused"])
        self.assertEqual(plan["anchors"], [])

    def test_refusals_still_apply(self):
        self.assertEqual(go(Layer(), toward=[1, 0, 0], dry_run=True)["status"], "refused")


class PreflightTests(unittest.TestCase):
    def test_direct_design_refused(self):
        design = Design()
        design.designType = 0
        payload = cp.run(design, None, options(), Layer())
        self.assertEqual(payload["status"], "refused")
        self.assertIn("direct design", payload["refusals"][0])

    def test_listed_with_a_planning_refusal(self):
        design = Design()
        design.activeEditObject = type("E", (), {"objectType": "adsk::fusion::BaseFeature", "name": "B"})()
        payload = cp.run(design, None, options(toward=[1, 0, 0]), Layer())
        self.assertEqual(len(payload["refusals"]), 2)

    def test_a_baseline_is_stored_when_built(self):
        self.assertIsNotNone(bodies.get_baseline(go(Layer())["baseline_token"]))


class HandlerTests(unittest.TestCase):
    def test_validation_error_touches_nothing(self):
        result = cp.construction_plane({"mode": "offset"})
        self.assertTrue(result["isError"])


# -- Fusion layer ----------------------------------------------------------------


class P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Component:
    name = "Sub"


class Occurrence:
    fullPathName = "Sub:1"

    def __init__(self, component=None):
        self.component = component or Component()


class Entity:
    assemblyContext = None
    nativeObject = None
    body = None
    parentSketch = None
    component = None


class PrepareTests(unittest.TestCase):
    def test_root_leaves_entities_alone(self):
        entity = Entity()
        self.assertEqual(cp.prepare_entities([entity], "root", None), ([entity], None))

    def test_own_geometry_goes_in_natively(self):
        occurrence = Occurrence()
        core, proxy = Entity(), Entity()
        proxy.assemblyContext, proxy.nativeObject = occurrence, core
        entities, creation = cp.prepare_entities([proxy], occurrence.component, occurrence)
        self.assertEqual(entities, [core])
        self.assertIsNone(creation)

    def test_a_sketch_point_finds_its_component_through_its_sketch(self):
        occurrence = Occurrence()
        point = Entity()
        point.parentSketch = type("S", (), {"parentComponent": occurrence.component})()
        self.assertEqual(cp.prepare_entities([point], occurrence.component, occurrence)[1], None)

    def test_another_components_geometry_needs_the_creation_occurrence(self):
        occurrence = Occurrence()
        foreign = Entity()
        foreign.assemblyContext = Occurrence(Component())
        entities, creation = cp.prepare_entities([foreign], occurrence.component, occurrence)
        self.assertEqual(entities, [foreign])
        self.assertIs(creation, occurrence)


class Units:
    def isValidExpression(self, text, kind):
        return text != "bad"

    def evaluateExpression(self, text, kind):
        return math.pi / 6 if kind == "deg" else 2.54


class Geometry:
    def __init__(self, origin):
        self.origin, self.normal = P(*origin), P(0, 0, 1)
        self.uDirection, self.vDirection = P(1, 0, 0), P(0, 1, 0)


class FusionPlane:
    assemblyContext = None
    name = "Plane1"
    entityToken = "native"

    def __init__(self):
        self.geometry = Geometry((0, 0, 0))

    def createForAssemblyContext(self, occurrence):
        view = FusionPlane()
        view.assemblyContext, view.entityToken = occurrence, "proxy"
        view.geometry = Geometry((10, 0, 0))
        return view


class FusionLayerTests(unittest.TestCase):
    def layer(self, occurrence=None):
        root = type("R", (), {"allOccurrences": [occurrence] if occurrence else []})()
        design = type("D", (), {"rootComponent": root, "unitsManager": Units()})()
        with mock.patch.object(cp, "FusionScene", lambda d: None):
            return cp.FusionPlanes(design, {"occurrence_path": safe_path(occurrence), "component": "root"})

    def test_evaluate_gives_degrees(self):
        self.assertAlmostEqual(self.layer().evaluate({"text": "30 deg"}, "deg"), 30.0)
        self.assertEqual(self.layer().evaluate({"text": "1 in"}, "cm"), 2.54)

    def test_evaluate_refuses_what_fusion_cannot_read(self):
        with self.assertRaises(ValueError):
            self.layer().evaluate({"text": "bad"}, "cm")

    def test_root_reads_directly(self):
        self.assertEqual(self.layer().read(FusionPlane())["origin"], (0, 0, 0))

    def test_a_sub_component_reads_through_its_occurrence(self):
        seen = self.layer(Occurrence()).read(FusionPlane())
        self.assertEqual(seen["origin"], (10, 0, 0))
        self.assertEqual(seen["token"], "proxy")

    def test_anchors_arrive_in_component_space(self):
        occurrence = Occurrence()
        frame = [P(10, 0, 0), P(1, 0, 0), P(0, 1, 0), P(0, 0, 1)]
        occurrence.transform2 = type("T", (), {"getAsCoordinateSystem": lambda self: frame})()
        layer = self.layer(occurrence)
        with mock.patch.object(cp.fo, "anchor_points", return_value="made") as place:
            self.assertEqual(layer.anchors([(11.0, 2.0, 3.0)], "N", False), "made")
        self.assertEqual(place.call_args.args[1], [(1.0, 2.0, 3.0)])

    def test_unknown_occurrence(self):
        root = type("R", (), {"allOccurrences": []})()
        design = type("D", (), {"rootComponent": root, "unitsManager": Units()})()
        with mock.patch.object(cp, "FusionScene", lambda d: None), self.assertRaises(ValueError):
            cp.FusionPlanes(design, {"occurrence_path": "Sub:9", "component": "root"})

    def test_the_wrong_component_for_the_occurrence(self):
        occurrence = Occurrence()
        root = type("R", (), {"allOccurrences": [occurrence]})()
        design = type("D", (), {"rootComponent": root, "unitsManager": Units()})()
        with mock.patch.object(cp, "FusionScene", lambda d: None), self.assertRaises(ValueError):
            cp.FusionPlanes(design, {"occurrence_path": "Sub:1", "component": "Other"})


def safe_path(occurrence):
    return occurrence.fullPathName if occurrence else None


class FreeNameTests(unittest.TestCase):
    def test_a_free_name_is_kept(self):
        self.assertEqual(cp.free_name("P", lambda name: False), "P")

    def test_a_taken_name_gets_the_next_number(self):
        taken = {"P", "P 2"}
        self.assertEqual(cp.free_name("P", taken.__contains__), "P 3")


if __name__ == "__main__":
    unittest.main()
