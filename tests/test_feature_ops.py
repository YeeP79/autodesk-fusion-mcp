"""feature_ops: preflight, values, add, snapshots, undo and the envelope, on fakes."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import solid_check
from fusion_bridge.common import feature_ops as fo
from fusion_bridge.common import timeline

HEALTHY, WARNING, ERROR = 0, 1, 2


class Item:
    isGroup = False
    errorOrWarningMessage = ""
    entity = None

    def __init__(self, name, health=HEALTHY, index=0):
        self.name, self.healthState, self.index = name, health, index


class Timeline:
    def __init__(self, *items, marker=None):
        self.items = list(items)
        self.count = len(self.items)
        self.markerPosition = self.count if marker is None else marker

    def __iter__(self):
        return iter(self.items)


class Occurrence:
    def __init__(self, path, linked=False):
        self.fullPathName = path
        self.isReferencedComponent = linked


class Root:
    bRepBodies = ()

    def __init__(self, occurrences=()):
        self.allOccurrences = list(occurrences)


class Design:
    designType = 1
    activeEditObject = None
    parentDocument = None

    def __init__(self, items=(), root=None, marker=None):
        self.timeline = Timeline(*items, marker=marker)
        self.rootComponent = root or Root()


class Document:
    def __init__(self, read_only):
        self.dataFile = type("F", (), {"isReadOnly": read_only})()


class PreflightTests(unittest.TestCase):
    def test_a_clean_design_passes(self):
        self.assertEqual(fo.preflight(Design([Item("Sketch1")])), ([], []))

    def test_not_a_design(self):
        refusals, _ = fo.preflight(None)
        self.assertEqual(len(refusals), 1)

    def test_direct_design(self):
        design = Design()
        design.designType = 0
        self.assertIn("direct", fo.preflight(design)[0][0])

    def test_all_refusals_come_together(self):
        design = Design([Item("Bad", ERROR)], marker=0, root=Root([Occurrence("Link:1", True)]))
        design.activeEditObject = type("B", (), {"objectType": "adsk::fusion::BaseFeature", "name": "Base1"})()
        refusals, _ = fo.preflight(design, targets=[{"occurrence_path": "Link:1"}])
        self.assertEqual(len(refusals), 4)
        text = " ".join(refusals)
        for word in ("'Bad'", "marker", "Base1", "Link:1"):
            self.assertIn(word, text)

    def test_marker_rule_relaxes_inside_a_rolled_back_window(self):
        design = Design([Item("A"), Item("B")], marker=1)
        self.assertEqual(len(fo.preflight(design)[0]), 1)
        with timeline.rolled_back_window():
            self.assertEqual(fo.preflight(design)[0], [])
        self.assertEqual(len(fo.preflight(design)[0]), 1)

    def test_broken_timeline_still_refuses_in_the_window(self):
        design = Design([Item("A", ERROR)], marker=0)
        with timeline.rolled_back_window():
            self.assertEqual(len(fo.preflight(design)[0]), 1)

    def test_warning_health_is_no_refusal(self):
        self.assertEqual(fo.preflight(Design([Item("A", WARNING)]))[0], [])

    def test_editing_the_root_component_is_fine(self):
        design = Design()
        design.activeEditObject = type("C", (), {"objectType": "adsk::fusion::Component", "name": "root"})()
        self.assertEqual(fo.preflight(design)[0], [])

    def test_child_of_a_linked_occurrence_is_linked(self):
        design = Design(root=Root([Occurrence("Link:1", True), Occurrence("Link:1+Child:1")]))
        self.assertEqual(len(fo.preflight(design, targets=[{"occurrence_path": "Link:1+Child:1"}])[0]), 1)

    def test_similar_path_is_not_linked(self):
        design = Design(root=Root([Occurrence("Link:1", True), Occurrence("Link:10")]))
        self.assertEqual(fo.preflight(design, targets=[{"occurrence_path": "Link:10"}])[0], [])

    def test_read_only_is_a_warning(self):
        refusals, warnings = fo.preflight(Design(), Document(True))
        self.assertEqual((refusals, warnings), ([], [fo.READ_ONLY_WARNING]))
        self.assertEqual(fo.preflight(Design(), Document(False))[1], [])


class Units:
    def __init__(self, valid=True):
        self.valid = valid
        self.asked = []

    def isValidExpression(self, text, kind):
        self.asked.append((text, kind))
        return self.valid


class ValueTests(unittest.TestCase):
    def test_number_carries_its_unit(self):
        self.assertEqual(fo.read_value({"distance": 1.5}, "distance", "in"), {"text": "1.5 in", "number": 1.5})
        self.assertEqual(fo.read_value({"distance": 2}, "distance", "mm")["text"], "2 mm")

    def test_float_noise_does_not_reach_the_dialog(self):
        self.assertEqual(fo.read_value({"d": 0.1 + 0.2}, "d", "in")["text"], "0.3 in")

    def test_expression(self):
        value = fo.read_value({"distance_expression": " wall*2 "}, "distance", "in")
        self.assertEqual(value, {"text": "wall*2", "number": None})

    def test_named_expression_key(self):
        value = fo.read_value({"taper_expression": "5 deg"}, "taper_deg", "deg", "taper_expression")
        self.assertEqual(value["text"], "5 deg")
        self.assertEqual(fo.read_value({"taper_deg": 5}, "taper_deg", "deg", "taper_expression")["text"], "5 deg")

    def test_exactly_one(self):
        with self.assertRaisesRegex(ValueError, "not both"):
            fo.read_value({"distance": 1, "distance_expression": "2 in"}, "distance", "in")
        with self.assertRaisesRegex(ValueError, "required"):
            fo.read_value({}, "distance", "in")

    def test_optional(self):
        self.assertIsNone(fo.read_value({}, "distance", "in", required=False))

    def test_blank_expression_is_absent(self):
        self.assertEqual(fo.read_value({"size": 1, "size_expression": ""}, "size", "in")["number"], 1.0)

    def test_bare_numbers_are_refused(self):
        for text in ("2", "2.5", ".5", "-1", "2*3", "(1+2)/3", "1e3", "2.5E-2"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "default units"):
                fo.read_value({"d_expression": text}, "d", "in")

    def test_expressions_with_a_unit_or_parameter_pass(self):
        for text in ("2 in", "2in", "wall", "wall*2", "e", "E + 1 mm", "abs(x)"):
            with self.subTest(text=text):
                self.assertEqual(fo.read_value({"d_expression": text}, "d", "in")["text"], text)

    def test_bad_numbers(self):
        for value in (float("nan"), float("inf"), "1", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fo.read_value({"d": value}, "d", "in")
        with self.assertRaises(ValueError):
            fo.read_value({"d_expression": 3}, "d", "in")

    def test_minimum_is_exclusive(self):
        with self.assertRaises(ValueError):
            fo.read_value({"size": 0}, "size", "in", minimum=0)
        self.assertEqual(fo.read_value({"size": 0.001}, "size", "in", minimum=0)["number"], 0.001)

    def test_value_input_goes_through_create_by_string(self):
        units = Units()
        with mock.patch.object(fo.adsk.core.ValueInput, "createByString", side_effect=lambda s: ("input", s)):
            made = fo.value_input({"text": "1.5 in", "number": 1.5}, units)
        self.assertEqual(made, ("input", "1.5 in"))
        self.assertEqual(units.asked, [("1.5 in", "cm")])

    def test_value_input_refuses_what_fusion_cannot_read(self):
        with self.assertRaisesRegex(ValueError, "not a valid expression"):
            fo.value_input({"text": "nope", "number": None}, Units(False))

    def test_angle_kind(self):
        units = Units()
        with mock.patch.object(fo.adsk.core.ValueInput, "createByString", side_effect=lambda s: s):
            fo.value_input({"text": "30 deg", "number": 30.0}, units, "deg")
        self.assertEqual(units.asked, [("30 deg", "deg")])


class Feature:
    objectType = "adsk::fusion::ExtrudeFeature"
    name = "Extrude1"
    errorOrWarningMessage = ""

    def __init__(self, health=HEALTHY, message="", delete=True):
        self.healthState = health
        self.errorOrWarningMessage = message
        self.delete = delete
        self.deleted = False
        self.parentComponent = type("C", (), {"name": "root"})()
        self.timelineObject = type("T", (), {"index": 12})()

    def deleteMe(self):
        if isinstance(self.delete, Exception):
            raise self.delete
        self.deleted = self.delete
        return self.delete


class AddFeatureTests(unittest.TestCase):
    def test_built(self):
        feature = Feature()
        outcome = fo.add_feature(lambda: feature)
        self.assertIs(outcome["feature"], feature)
        self.assertEqual((outcome["status"], outcome["health"], outcome["refusal"]), ("built", "healthy", None))

    def test_type_error_raises(self):
        def add():
            raise TypeError("wrong arguments")

        with self.assertRaises(TypeError):
            fo.add_feature(add)

    def test_runtime_error_is_a_refusal_verbatim(self):
        text = "3 : ASM_BL_CAP_COMPLEX - could not cap"

        def add():
            raise RuntimeError(text)

        outcome = fo.add_feature(add)
        self.assertEqual((outcome["status"], outcome["refusal"], outcome["feature"]), ("refused", text, None))

    def test_none_is_a_refusal(self):
        self.assertEqual(fo.add_feature(lambda: None)["status"], "refused")

    def test_feature_in_error_is_deleted_and_refused(self):
        feature = Feature(ERROR, "No target body")
        outcome = fo.add_feature(lambda: feature)
        self.assertTrue(feature.deleted)
        self.assertEqual((outcome["status"], outcome["refusal"], outcome["feature"]),
                         ("refused", "No target body", None))
        self.assertEqual(outcome["warnings"], [])

    def test_failed_delete_is_said(self):
        for delete in (False, RuntimeError("locked")):
            with self.subTest(delete=delete):
                outcome = fo.add_feature(lambda: Feature(ERROR, "bad", delete=delete))
                self.assertEqual(outcome["status"], "refused")
                self.assertIn("still in the timeline", outcome["warnings"][0])

    def test_warning_health_is_built_with_its_message(self):
        outcome = fo.add_feature(lambda: Feature(WARNING, "close to self-intersecting"))
        self.assertEqual((outcome["status"], outcome["health"]), ("built", "warning"))
        self.assertEqual(outcome["warnings"], ["close to self-intersecting"])


def rec(volume=10.0, faces=6, path="root", body="B", component="root", index=0):
    return {"volume": volume, "faces": faces, "bbox": None, "component": component,
            "occurrence_path": path, "body": body, "index": index}


class Box:
    class P:
        def __init__(self, c):
            self.x, self.y, self.z = c

    def __init__(self, low, high):
        self.minPoint, self.maxPoint = self.P(low), self.P(high)


class Faces:
    def __init__(self, count):
        self.count = count


class Solid:
    isSolid = True
    lumps = shells = edges = None

    def __init__(self, name, volume, faces=6, visible=True):
        self.name, self.volume, self.faces, self.isVisible = name, volume, Faces(faces), visible
        self.boundingBox = Box((0, 0, 0), (1, 1, 1))


class SnapshotTests(unittest.TestCase):
    def design(self, *bodies):
        root = Root()
        root.bRepBodies = list(bodies)
        design = Design(root=root)
        design.parentDocument = type("D", (), {"name": "Doc v1"})()
        return design

    def test_hidden_bodies_are_measured(self):
        records = fo.snapshot(self.design(Solid("A", 1.0), Solid("Hidden", 2.0, visible=False)))
        self.assertEqual(sorted(records), ["root/A", "root/Hidden"])
        self.assertEqual(records["root/Hidden"]["volume"], 2.0)

    def test_repeated_names_get_keys(self):
        records = fo.snapshot(self.design(Solid("A", 1.0), Solid("A", 2.0)))
        self.assertEqual(sorted(records), ["root/A#0", "root/A#1"])

    def test_baseline_is_stored_for_check_solids(self):
        records, token = fo.baseline(self.design(Solid("A", 1.0)))
        stored = solid_check.get_baseline(token)
        self.assertEqual(stored["records"], records)
        self.assertEqual(stored["document"], "Doc v1")


class BodyChangesTests(unittest.TestCase):
    def test_target_row(self):
        before = {"root/B": rec(12.0, 6)}
        after = {"root/B": rec(11.2, 7)}
        changes = fo.body_changes(before, after, ["root/B"], "cm")
        self.assertEqual(changes["touched"], [{
            "ref": {"occurrence_path": "root", "body_name": "B"}, "role": "target", "volume_before": 12.0,
            "volume_after": 11.2, "delta": -0.8, "percent": -6.67, "faces_before": 6, "faces_after": 7}])
        self.assertEqual(changes["outside_target"], [])

    def test_units(self):
        before, after = {"root/B": rec(2.54 ** 3)}, {"root/B": rec(2 * 2.54 ** 3)}
        row = fo.body_changes(before, after, ["root/B"], "in")["touched"][0]
        self.assertEqual((row["volume_before"], row["delta"]), (1.0, 1.0))

    def test_unchanged_target_is_not_touched(self):
        changes = fo.body_changes({"root/B": rec()}, {"root/B": rec()}, ["root/B"], "cm")
        self.assertEqual(changes["touched"], [])
        self.assertEqual(fo.unchanged({"root/B": rec()}, {"root/B": rec()}, ["root/B"]), ["root/B"])

    def test_face_change_alone_counts(self):
        self.assertEqual(fo.unchanged({"k": rec()}, {"k": rec(faces=7)}, ["k"]), [])

    def test_created_and_consumed(self):
        changes = fo.body_changes({"root/Old": rec(body="Old")}, {"root/New": rec(4.0, body="New")}, [], "cm")
        self.assertEqual(changes["created"], [{"ref": {"occurrence_path": "root", "body_name": "New"},
                                               "volume": 4.0, "faces": 6}])
        self.assertEqual(changes["consumed"][0]["ref"]["body_name"], "Old")

    def test_repeated_name_gets_an_index(self):
        changes = fo.body_changes({}, {"root/B#1": rec(index=1)}, [], "cm")
        self.assertEqual(changes["created"][0]["ref"]["body_index"], 1)

    def test_outside_loss_is_outside_target_and_lost(self):
        before = {"root/T": rec(body="T"), "root/X": rec(body="X")}
        after = {"root/T": rec(5.0, body="T"), "root/X": rec(9.0, body="X")}
        changes = fo.body_changes(before, after, ["root/T"], "cm")
        self.assertEqual(changes["outside_target"][0]["role"], "outside")
        self.assertEqual(fo.lost_outside(before, after, ["root/T"]), ["root/X"])

    def test_outside_gain_is_reported_but_not_lost(self):
        before, after = {"root/X": rec(body="X")}, {"root/X": rec(11.0, body="X")}
        self.assertEqual(len(fo.body_changes(before, after, [], "cm")["outside_target"]), 1)
        self.assertEqual(fo.lost_outside(before, after, []), [])

    def test_vanished_body_is_lost(self):
        self.assertEqual(fo.lost_outside({"root/X": rec(body="X")}, {}, []), ["root/X"])

    def test_target_loss_is_not_collateral(self):
        self.assertEqual(fo.lost_outside({"root/T": rec()}, {"root/T": rec(1.0)}, ["root/T"]), [])

    def test_other_instance_is_not_collateral(self):
        one = rec(path="Sub:1", body="S", component="Sub")
        two = rec(path="Sub:2", body="S", component="Sub")
        before = {"Sub:1/S": one, "Sub:2/S": two}
        after = {"Sub:1/S": {**one, "volume": 8.0}, "Sub:2/S": {**two, "volume": 8.0}}
        changes = fo.body_changes(before, after, ["Sub:1/S"], "cm")
        self.assertEqual([row["role"] for row in changes["touched"]], ["target", "other_instance"])
        self.assertEqual(changes["outside_target"], [])
        self.assertEqual(fo.lost_outside(before, after, ["Sub:1/S"]), [])

    def test_same_name_in_the_same_occurrence_is_no_instance(self):
        before = {"root/B#0": rec(), "root/B#1": rec(index=1)}
        after = {"root/B#0": rec(5.0), "root/B#1": rec(5.0, index=1)}
        self.assertEqual(fo.lost_outside(before, after, ["root/B#0"]), ["root/B#1"])


class UndoTests(unittest.TestCase):
    def run_undo(self, feature, before, after):
        with mock.patch.object(fo, "snapshot", return_value=after):
            return fo.undo(object(), feature, before)

    def test_volumes_returned(self):
        feature = Feature()
        self.assertEqual(self.run_undo(feature, {"k": rec()}, {"k": rec()}), {"undone": True, "warnings": []})
        self.assertTrue(feature.deleted)

    def test_volume_that_did_not_return(self):
        outcome = self.run_undo(Feature(), {"k": rec()}, {"k": rec(5.0)})
        self.assertFalse(outcome["undone"])
        self.assertIn("'k'", outcome["warnings"][0])

    def test_body_that_did_not_return(self):
        self.assertFalse(self.run_undo(Feature(), {"k": rec()}, {})["undone"])

    def test_delete_refused(self):
        for delete in (False, RuntimeError("locked")):
            with self.subTest(delete=delete):
                outcome = self.run_undo(Feature(delete=delete), {}, {})
                self.assertFalse(outcome["undone"])
                self.assertIn("could not be deleted", outcome["warnings"][0])


class EnvelopeTests(unittest.TestCase):
    def test_shape(self):
        payload = fo.envelope("built", "in", feature=fo.feature_row(Feature()), baseline_token="a1", extra_key=1)
        self.assertEqual(payload["feature"], {"name": "Extrude1", "type": "ExtrudeFeature", "component": "root",
                                              "timeline_index": 12, "health": "healthy"})
        self.assertEqual(payload["units"], {"length": "in", "area": "in^2", "volume": "in^3"})
        self.assertEqual(payload["bodies"], {"touched": [], "created": [], "consumed": [], "outside_target": []})
        self.assertEqual(payload["extra_key"], 1)
        self.assertFalse(payload["dry_run"])
        json.dumps(payload)

    def test_only_built_and_planned_are_ok(self):
        for status in fo.STATUSES:
            with self.subTest(status=status):
                payload = fo.envelope(status, "in")
                self.assertEqual(payload["ok"], status in ("built", "planned"))
                self.assertEqual(fo.write_result(payload)["isError"], not payload["ok"])

    def test_unknown_status(self):
        with self.assertRaises(ValueError):
            fo.envelope("done", "in")

    def test_refused_keeps_every_reason(self):
        payload = fo.refused("in", ["one", "two"])
        self.assertEqual((payload["status"], payload["refusals"]), ("refused", ["one", "two"]))


class Spot:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class SketchPoint:
    def __init__(self, geometry, token):
        self.geometry, self.entityToken = geometry, token


class Sketch:
    isLightBulbOn = True
    name = "Sketch1"

    def __init__(self, flatten, plane):
        self.flatten, self.plane = flatten, plane
        self.sketchPoints = self
        self.added = []

    def modelToSketchSpace(self, point):
        return point

    def sketchToModelSpace(self, point):
        return point

    def add(self, point):
        made = SketchPoint(Spot(point.x, point.y, 0.0 if self.flatten else point.z), f"t{len(self.added)}")
        self.added.append(made)
        return made

    def deleteMe(self):
        raise AssertionError("an anchor must never be deleted")


class Sketches:
    def __init__(self, flatten):
        self.flatten, self.made = flatten, []

    def add(self, plane):
        sketch = Sketch(self.flatten and not self.made, plane)
        self.made.append(sketch)
        return sketch


class PlaneInput:
    def setByOffset(self, parent, value):
        self.parent = parent


class AnchorPlane:
    isLightBulbOn = True
    name = "Plane1"


class Planes:
    def __init__(self):
        self.made = []

    def createInput(self):
        return PlaneInput()

    def add(self, entry):
        self.made.append(AnchorPlane())
        return self.made[-1]


class Component:
    xYConstructionPlane = "XY"

    def __init__(self, flatten=False):
        self.sketches, self.constructionPlanes = Sketches(flatten), Planes()


class AnchorPointTests(unittest.TestCase):
    POINTS = [(0, 0, 1), (1, 0, 2), (0, 1, 3)]

    def anchor(self, component, show=False):
        return fo.anchor_points(component, self.POINTS, "Boss", show, make=Spot)

    def test_one_sketch_keeps_z(self):
        component = Component()
        made = self.anchor(component)
        self.assertEqual(len(component.sketches.made), 1)
        self.assertEqual(component.constructionPlanes.made, [])
        self.assertEqual([p.geometry.z for p in made["points"]], [1, 2, 3])
        self.assertEqual(made["rows"][0], {"sketch": "Boss_anchors", "token": "t0", "visible": False})
        self.assertEqual(made["unused"], [])

    def test_light_bulb_off_by_default_and_on_when_asked(self):
        component = Component()
        self.anchor(component)
        self.assertFalse(component.sketches.made[0].isLightBulbOn)
        component = Component()
        self.assertTrue(self.anchor(component, show=True)["rows"][0]["visible"])

    def test_a_sketch_that_flattens_falls_back_to_one_each(self):
        component = Component(flatten=True)
        made = self.anchor(component)
        self.assertEqual(len(component.sketches.made), 4)
        self.assertEqual(len(component.constructionPlanes.made), 3)
        self.assertEqual([r["sketch"] for r in made["rows"]], ["Boss_anchors_1", "Boss_anchors_2", "Boss_anchors_3"])
        self.assertEqual(made["rows"][1]["plane"], "Boss_anchors_2_plane")
        self.assertFalse(any(p.isLightBulbOn for p in component.constructionPlanes.made))
        self.assertFalse(any(s.isLightBulbOn for s in component.sketches.made))

    def test_the_flattened_sketch_is_reported_not_deleted(self):
        self.assertEqual(self.anchor(Component(flatten=True))["unused"], ["Boss_anchors"])


if __name__ == "__main__":
    unittest.main()
