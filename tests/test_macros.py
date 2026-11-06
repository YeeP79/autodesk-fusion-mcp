"""macros: argument refusals, step order, stopping, what a partial failure reports, and the math.

``FakeTools`` answers from a script and puts every call through the real tool's
schema keys and argument parser, so a macro cannot pass a test with a call the
tool would refuse.
"""

import math
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401

from fusion_bridge import (
    apply_thread,
    combine_bodies,
    construction_plane,
    edge_treatment,
    extrude_profile,
    find_entities,
    macros,
    pattern_features,
    primitives,
    python_exec,
    sketch_geometry,
    sketch_profile,
    solid_check,
)
from fusion_bridge.common.geometry import cross, dot, sub
from fusion_bridge.common.results import ok_result, tool_result

bored_boss, drilled_hole, flanged_plate = macros.bored_boss, macros.drilled_hole, macros.flanged_plate
gusset, steps = macros.gusset, macros.steps

BODY = {"occurrence_path": "root", "body_name": "Plate"}
SUB_BODY = {"occurrence_path": "Sub:1", "body_name": "Plate"}
TUBE_A = {"occurrence_path": "root", "body_name": "Tube A"}
TUBE_B = {"occurrence_path": "root", "body_name": "Tube B"}


def _error_pair(validate):
    def check(arguments):
        _, error = validate(arguments)
        if error:
            raise ValueError(error)
    return check


def _sketch(arguments):
    try:
        sketch_profile.parse_request(arguments)
    except sketch_geometry.GeometryError as exc:
        raise ValueError(exc.problems) from exc


VALIDATORS = {
    "find_entities": _error_pair(find_entities.validate_args),
    "sketch_profile": _sketch,
    "extrude_profile": extrude_profile.parse_request,
    "edge_treatment": _error_pair(edge_treatment.validate_args),
    "pattern_features": _error_pair(pattern_features.validate_args),
    "combine_bodies": _error_pair(combine_bodies.validate_args),
    "construction_plane": _error_pair(construction_plane.validate_args),
    "apply_thread": _error_pair(apply_thread.validate_args),
    "check_solids": _error_pair(solid_check.validate_args),
}


class FakeTools:
    """Answers each call from a script of ``(tool, payload)``, in order."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.mistakes = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            try:
                VALIDATORS[name](primitives.tool_arguments(name, kwargs))
            except Exception as exc:
                self.mistakes.append(f"{name}: {exc}")
            if not self.script:
                self.mistakes.append(f"{name} called past the end of the script")
                return {"status": "refused", "ok": False, "error": "script ended"}
            tool, payload = self.script.pop(0)
            if tool != name:
                self.mistakes.append(f"expected {tool}, got {name}")
            return payload
        return call

    def named(self, name):
        return [kwargs for tool, kwargs in self.calls if tool == name]

    @property
    def order(self):
        return [tool for tool, _ in self.calls]


def face_row(point, normal, token="face"):
    return {"ref": {"token": token, "kind": "face", "point": list(point), "body": BODY}, "type": "plane",
            "normal": list(normal), "label": f"plane {token}"}


def tube_row(origin, axis, token="tube"):
    return {"ref": {"token": token, "kind": "face", "point": list(origin), "body": BODY}, "type": "cylinder",
            "axis_origin": list(origin), "axis_direction": list(axis), "diameter": 1.0, "label": f"tube {token}"}


def edge_row(point=(0.5, 0.0, 1.0), token="edge"):
    return {"ref": {"token": token, "kind": "edge", "point": list(point), "body": BODY}, "type": "circle",
            "label": f"circle {token}", "faces": []}


def found(*rows, **extra):
    return ("find_entities", {"status": "done", "ok": True, "matched": len(rows), "returned": len(rows),
                              "truncated": False, "scan_capped": False, "entities": list(rows), **extra})


def sketched(token="sk", whole=True, **extra):
    return ("sketch_profile", {"status": "done", "ok": True, "profile_count": 2, "warnings": [],
                               "sketch": {"name": token, "entity_token": token, "component": "root"},
                               "circles": [{"index": 0, "whole": whole}], **extra})


def extruded(status="built", name="Extrude", end=("end",), delta=-0.5, created=None, normal=(0, 0, 1), **extra):
    touched = [{"ref": BODY, "role": "target", "delta": delta}] if created is None else []
    return ("extrude_profile", {
        "status": status, "ok": status == "built", "feature": {"name": name, "type": "ExtrudeFeature"},
        "bodies": {"touched": touched, "created": created or [], "consumed": [], "outside_target": []},
        "baseline_token": f"base-{name}", "extrude": {"direction_world": list(normal)},
        "faces": {"start": [], "end": [{"token": t, "kind": "face"} for t in end], "side": []}, "warnings": [],
        **extra})


def refused(tool, text="Fusion said no", **extra):
    return (tool, {"status": "refused", "ok": False, "refusals": [text], "feature": None, "warnings": [], **extra})


def treated(status="built", measured=(), **extra):
    return ("edge_treatment", {"status": status, "ok": status == "built", "features": [{"name": "Chamfer1"}],
                               "bodies": {"touched": [], "created": []}, "edges": {"requested": 1, "built": 1},
                               "measured": list(measured), "baseline_token": "base-edge", "warnings": [], **extra})


def checked(ok=True):
    return ("check_solids", {"status": "done", "ok": ok, "summary": {"problems": 0 if ok else 1}, "problems": []})


class MacroCase(unittest.TestCase):
    def run_macro(self, function, *script, **arguments):
        tools = FakeTools(*script)
        with mock.patch.object(steps, "log", lambda *a, **k: None):
            result = function(tools, **arguments)
        self.assertEqual(tools.mistakes, [])
        self.assertEqual(tools.script, [], "the macro stopped before the script did")
        return result, tools

    def refuses(self, function, text, **arguments):
        tools = FakeTools()
        result = function(tools, **arguments)
        self.assertEqual(result["status"], "refused")
        self.assertIs(result["ok"], False)
        self.assertEqual(tools.calls, [], "a refused argument must not reach a tool")
        self.assertIn(text, result["refusals"][0])
        self.assertEqual(result["steps"], [])


# -- bored_boss -----------------------------------------------------------------


BOSS = {"body": BODY, "point": [1.0, 2.0, 1.0], "outer_diameter": 2.0, "height": 0.5, "bore_diameter": 1.0}
BOSS_FACE = face_row([0, 0, 1], [0, 0, 1])


def boss_script(chamfer=True):
    script = [found(BOSS_FACE), sketched("boss"), extruded(name="Boss"), sketched("bore"),
              extruded(name="Bore", delta=-1.0, end=())]
    if chamfer:
        script += [found(edge_row()), treated()]
    return script + [checked()]


class BoredBossTests(MacroCase):
    def test_every_step_in_order(self):
        result, tools = self.run_macro(bored_boss.bored_boss, *boss_script(), **BOSS, rim_chamfer=0.03)
        self.assertEqual(tools.order, ["find_entities", "sketch_profile", "extrude_profile", "sketch_profile",
                                       "extrude_profile", "find_entities", "edge_treatment", "check_solids"])
        self.assertEqual(result["status"], "built")
        self.assertIs(result["ok"], True)
        self.assertEqual([row["step"] for row in result["steps"]],
                         ["face", "boss sketch", "boss", "bore sketch", "bore", "rim", "rim chamfer", "check"])
        self.assertEqual(result["not_run"], [])
        self.assertIsNone(result["stopped_at"])

    def test_the_boss_joins_and_the_bore_cuts_back_from_its_end(self):
        _, tools = self.run_macro(bored_boss.bored_boss, *boss_script(False), **BOSS)
        boss, bore = tools.named("extrude_profile")
        self.assertEqual(boss["operation"], "join")
        self.assertEqual(boss["target_bodies"], [BODY])
        self.assertEqual(boss["direction"], [0, 0, 1])
        self.assertEqual(boss["extent"], {"type": "distance", "distance": 0.5})
        self.assertEqual(bore["operation"], "cut")
        self.assertEqual(bore["direction"], [0.0, 0.0, -1.0])
        self.assertEqual(bore["extent"], {"type": "through_all"})
        self.assertEqual(bore["containing_point"], [1.0, 2.0, 1.5])
        self.assertEqual(tools.named("sketch_profile")[1]["plane_entity_token"], "end")
        self.assertEqual(tools.named("sketch_profile")[1]["circles"], [{"center": [1.0, 2.0, 1.5], "diameter": 1.0}])

    def test_a_sketch_on_a_face_is_never_read_as_one_profile(self):
        _, tools = self.run_macro(bored_boss.bored_boss, *boss_script(False), **BOSS)
        self.assertEqual({call["profile_select"] for call in tools.named("extrude_profile")}, {"containing_point"})

    def test_a_blind_bore(self):
        _, tools = self.run_macro(bored_boss.bored_boss, *boss_script(False), **BOSS, bore_depth=0.75)
        self.assertEqual(tools.named("extrude_profile")[1]["extent"], {"type": "distance", "distance": 0.75})

    def test_rim_is_found_on_the_end_by_a_band_not_an_exact_diameter(self):
        _, tools = self.run_macro(bored_boss.bored_boss, *boss_script(), **BOSS, rim_chamfer=0.03, tolerance=0.002)
        rim = tools.named("find_entities")[1]
        self.assertEqual(rim["diameter"], {"min": 0.998, "max": 1.002})
        self.assertEqual(rim["on_plane"], {"point": [1.0, 2.0, 1.5], "normal": [0, 0, 1], "tolerance": 0.002})
        self.assertEqual(rim["axis"]["through"], [1.0, 2.0, 1.5])
        self.assertEqual(tools.named("edge_treatment")[0]["sets"], [{"edges": [edge_row()["ref"]], "size": 0.03}])

    def test_the_check_uses_the_first_baseline_and_the_body_read_back(self):
        moved = {**BODY, "body_index": 1}
        boss = extruded(name="Boss")
        boss[1]["bodies"]["touched"][0]["ref"] = moved
        script = boss_script(False)
        script[2] = boss
        result, tools = self.run_macro(bored_boss.bored_boss, *script, **BOSS)
        self.assertEqual(tools.named("extrude_profile")[1]["target_bodies"], [moved])
        check = tools.named("check_solids")[0]
        self.assertEqual(check["baseline_token"], "base-Boss")
        self.assertEqual(check["edited_component"], "root")
        self.assertEqual(check["bodies"], [BODY], "check_solids takes no body_index")
        self.assertEqual(result["body"], moved)

    def test_a_check_with_problems_is_suspect_not_built(self):
        script = boss_script(False)
        script[-1] = checked(ok=False)
        result, _ = self.run_macro(bored_boss.bored_boss, *script, **BOSS)
        self.assertEqual(result["status"], "suspect")
        self.assertIs(result["ok"], False)
        self.assertIn("check_solids found problems", result["warnings"][0])
        self.assertEqual(len(result["built"]["features"]), 2)

    def test_a_refused_check_is_not_a_finished_check(self):
        script = boss_script(False)
        script[-1] = ("check_solids", {"status": "refused", "ok": False, "error": "Error: unknown baseline_token"})
        result, _ = self.run_macro(bored_boss.bored_boss, *script, **BOSS)
        self.assertEqual(result["status"], "partial")
        self.assertIn("unknown baseline_token", result["stopped_at"]["why"])

    def test_in_a_sub_component_every_step_goes_through_the_instance(self):
        script = boss_script(False)
        script[2][1]["bodies"]["touched"][0]["ref"] = SUB_BODY
        _, tools = self.run_macro(bored_boss.bored_boss, *script, **{**BOSS, "body": SUB_BODY})
        for call in tools.named("sketch_profile") + tools.named("extrude_profile"):
            self.assertEqual(call["occurrence_path"], "Sub:1")
        self.assertEqual(tools.named("check_solids")[0]["edited_component"], "Sub:1")

    def test_refusals(self):
        cases = [("'body' is required", {"body": None}),
                 ("occurrence_path", {"body": {"body_name": "x"}}),
                 ("'point' is required", {"point": None}),
                 ("'point'", {"point": [1, 2]}),
                 ("above zero", {"outer_diameter": 0}),
                 ("above zero", {"height": -1}),
                 ("above zero", {"height": True}),
                 ("above zero", {"height": float("nan")}),
                 ("below 'outer_diameter'", {"bore_diameter": 2.0}),
                 ("below the boss's wall", {"rim_chamfer": 0.5}),
                 ("no length", {"normal": [0, 0, 0]}),
                 ("unknown argument", {"color": "red"}),
                 ("'units'", {"units": "ft"}),
                 ("'units'", {"units": ["in"]}),
                 ("above zero", {"tolerance": 0})]
        for text, change in cases:
            with self.subTest(change=change):
                self.refuses(bored_boss.bored_boss, text, **{**BOSS, **change})

    def test_no_arguments_at_all(self):
        self.refuses(bored_boss.bored_boss, "is required")


class StoppingTests(MacroCase):
    def test_a_read_that_stops_leaves_nothing_so_it_is_refused(self):
        result, tools = self.run_macro(bored_boss.bored_boss, found(), **BOSS)
        self.assertEqual(tools.order, ["find_entities"])
        self.assertEqual(result["status"], "refused")
        self.assertIn("expected 1 match(es), found 0", result["refusals"][0])
        self.assertNotIn("kept", result)
        self.assertEqual(result["not_run"], ["boss sketch", "boss", "bore sketch", "bore", "check"])

    def test_two_faces_at_the_point_stop_it(self):
        result, _ = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE, face_row([0, 0, 1], [1, 0, 0])), **BOSS)
        self.assertEqual(result["status"], "refused")
        self.assertIn("found 2", result["stopped_at"]["why"])
        self.assertIs(result["steps"][0]["ok"], False)

    def test_an_unfinished_scan_is_not_an_answer(self):
        for flag in ("scan_capped", "truncated"):
            result, _ = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE, **{flag: True}), **BOSS)
            self.assertEqual(result["status"], "refused", flag)
            self.assertIn("did not finish", result["stopped_at"]["why"])

    def test_a_refused_sketch_stops_with_nothing_left(self):
        script = [found(BOSS_FACE), ("sketch_profile", {"status": "refused", "ok": False, "error": "refused",
                                                        "problems": ["circles[0].center is off the plane"]})]
        result, _ = self.run_macro(bored_boss.bored_boss, *script, **BOSS)
        self.assertEqual(result["status"], "refused")
        self.assertIn("off the plane", result["refusals"][0])
        self.assertEqual(result["built"]["sketches"], [])

    def test_a_refused_extrude_keeps_the_sketch_and_says_so(self):
        result, tools = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE), sketched("boss"),
                                       refused("extrude_profile"), **BOSS)
        self.assertEqual(tools.order, ["find_entities", "sketch_profile", "extrude_profile"])
        self.assertEqual(result["status"], "partial")
        self.assertIs(result["ok"], False)
        self.assertEqual(result["stopped_at"], {"step": "boss", "name": "extrude_profile", "status": "refused",
                                                "why": "Fusion said no"})
        self.assertEqual([row["name"] for row in result["built"]["sketches"]], ["boss"])
        self.assertEqual(result["built"]["features"], [])
        self.assertIn("1 sketches", result["kept"])
        self.assertEqual(result["not_run"], ["bore sketch", "bore", "check"])

    def test_a_circle_cut_by_the_face_edge_stops_before_a_clipped_extrude(self):
        result, tools = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE), sketched("boss", whole=False), **BOSS)
        self.assertEqual(tools.named("extrude_profile"), [])
        self.assertEqual(result["status"], "partial")
        self.assertIn("not whole", result["stopped_at"]["why"])

    def test_a_circle_in_no_profile_stops_too(self):
        result, _ = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE), sketched("boss", whole=None), **BOSS)
        self.assertEqual(result["status"], "partial")

    def test_suspect_midway_stops_and_is_never_success(self):
        result, tools = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE), sketched("boss"),
                                       extruded("suspect", name="Boss", warnings=["measured depth 0.4 in"]), **BOSS)
        self.assertEqual(tools.named("sketch_profile")[1:], [])
        self.assertEqual(result["status"], "partial")
        self.assertIs(result["ok"], False)
        self.assertEqual(result["stopped_at"]["status"], "suspect")
        self.assertIn("measured depth", result["stopped_at"]["why"])
        self.assertEqual([row["name"] for row in result["built"]["features"]], ["Boss"], "suspect still stands")

    def test_partial_on_the_last_treatment_is_reported(self):
        script = boss_script()[:-2] + [treated("partial")]
        result, _ = self.run_macro(bored_boss.bored_boss, *script, **BOSS, rim_chamfer=0.03)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["built"]["features"][-1]["name"], "Chamfer1")
        self.assertEqual(result["not_run"], ["check"])

    def test_an_undone_feature_is_not_counted_as_built(self):
        result, _ = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE), sketched("boss"),
                                   extruded("undone", name="Boss", collateral=["root/Other"]), **BOSS)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["built"]["features"], [])
        self.assertEqual(result["steps"][-1]["result"]["collateral"], ["root/Other"])

    def test_a_feature_that_could_not_be_taken_back_is_listed(self):
        payload = refused("extrude_profile", "could not be fully taken back", feature={"name": "Boss"})
        result, _ = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE), sketched("boss"), payload, **BOSS)
        self.assertEqual(result["built"]["features"], [{"name": "Boss", "not_taken_back": True}])

    def test_a_failed_step_says_the_state_is_unknown(self):
        failed = ("extrude_profile", {"status": "failed", "ok": False, "error": "Error: boom", "traceback": "t"})
        result, _ = self.run_macro(bored_boss.bored_boss, found(BOSS_FACE), sketched("boss"), failed, **BOSS)
        self.assertEqual(result["status"], "failed")
        self.assertIs(result["ok"], False)
        self.assertIn("may have changed more", result["kept"])

    def test_a_macro_bug_keeps_the_steps_it_took(self):
        broken = ("extrude_profile", {"status": "built", "ok": True, "feature": {"name": "Boss"}})
        script = [found(BOSS_FACE), sketched("boss"), broken]
        tools = FakeTools(*script)
        with mock.patch.object(steps, "log", lambda *a, **k: None), \
                mock.patch.object(steps, "end_face", side_effect=KeyError("nope")):
            result = bored_boss.bored_boss(tools, **BOSS)
        self.assertEqual(result["status"], "failed")
        self.assertIn("nope", result["error"])
        self.assertIn("traceback", result)
        self.assertEqual(len(result["steps"]), 3)
        self.assertEqual(len(result["built"]["sketches"]), 1)
        self.assertEqual(len(result["built"]["features"]), 1)

    def test_step_rows_carry_each_primitives_warnings_into_the_result(self):
        script = boss_script(False)
        script[1] = sketched("boss", warnings=["world positions could not be read back"])
        result, _ = self.run_macro(bored_boss.bored_boss, *script, **BOSS)
        self.assertEqual(result["status"], "built")
        self.assertIn("boss sketch: world positions could not be read back", result["warnings"])


class UnitsTests(MacroCase):
    def test_every_step_gets_the_macros_units(self):
        _, tools = self.run_macro(bored_boss.bored_boss, *boss_script(False), **BOSS, units="mm")
        self.assertEqual({kwargs["units"] for _, kwargs in tools.calls}, {"mm"})

    def test_units_default_to_inches(self):
        result, tools = self.run_macro(bored_boss.bored_boss, *boss_script(False), **BOSS)
        self.assertEqual({kwargs["units"] for _, kwargs in tools.calls}, {"in"})
        self.assertEqual(result["units"]["length"], "in")

    def test_the_default_tolerance_is_the_same_length_in_any_units(self):
        for units, within in (("in", 0.001), ("mm", 0.0254), ("cm", 0.00254)):
            _, tools = self.run_macro(bored_boss.bored_boss, *boss_script(False), **BOSS, units=units)
            self.assertAlmostEqual(tools.named("find_entities")[0]["near"]["within"], within, 12)


# -- flanged_plate --------------------------------------------------------------


FLANGE = {"outline": [[-3, -3, 0], [3, -3, 0], [3, 3, 0], [-3, 3, 0]], "plane": "xy", "direction": [0, 0, 1],
          "thickness": 0.5, "pilot_center": [0, 0, 0], "pilot_diameter": 2.0, "first_bolt": [2, 0, 0],
          "bolt_diameter": 0.5, "bolt_count": 6, "body_name": "Flange"}
FLANGE_REF = {"occurrence_path": "root", "body_name": "Flange"}
PILOT_ROW = tube_row([0, 0, 0], [0, 0, 1], "pilot")


def plate_extrude():
    return extruded(name="Plate", end=("top",), created=[{"ref": FLANGE_REF, "volume": 16.0, "faces": 7}])


def pilot_sketch(loops=2):
    return sketched("outline", profiles=[{"loop_count": loops}, {"loop_count": 1}], added_to_existing=True)


def patterned(status="built", **extra):
    return ("pattern_features", {"status": status, "ok": status == "built", "feature": {"name": "Pattern"},
                                 "pattern": {"asked_for": 6, "elements": 6, "instances_reached": 6},
                                 "bodies": {"touched": [], "created": []}, "positions": [[2, 0, 0.5]],
                                 "positions_basis": "faces", "warnings": [], **extra})


def flange_script():
    return [sketched("outline"), pilot_sketch(), plate_extrude(), sketched("seed"),
            extruded(name="Seed", delta=-0.1, end=()), found(PILOT_ROW), patterned()]


class FlangedPlateTests(MacroCase):
    def test_every_step_in_order(self):
        script = flange_script() + [found(*[edge_row(token=str(i)) for i in range(6)]), treated()]
        result, tools = self.run_macro(flanged_plate.flanged_plate, *script, **FLANGE, bolt_chamfer=0.03)
        self.assertEqual(tools.order, ["sketch_profile", "sketch_profile", "extrude_profile", "sketch_profile",
                                       "extrude_profile", "find_entities", "pattern_features", "find_entities",
                                       "edge_treatment"])
        self.assertEqual(result["status"], "built")
        self.assertEqual(result["body"], FLANGE_REF)
        self.assertEqual(result["plate"]["bolt_centers"], [[2, 0, 0.5]])

    def test_outline_and_pilot_share_a_sketch_and_the_plate_takes_most_loops(self):
        _, tools = self.run_macro(flanged_plate.flanged_plate, *flange_script(), **FLANGE)
        outline, pilot, _ = tools.named("sketch_profile")
        self.assertEqual((outline["sketch_plane"], outline["space"]), ("xy", "world"))
        self.assertEqual(pilot["sketch_entity_token"], "outline")
        plate = tools.named("extrude_profile")[0]
        self.assertEqual(plate["profile_select"], "most_loops")
        self.assertEqual(plate["body_name"], "Flange")
        self.assertNotIn("target_bodies", plate)

    def test_seed_is_cut_from_the_top_and_patterned_about_the_pilot_bore(self):
        _, tools = self.run_macro(flanged_plate.flanged_plate, *flange_script(), **FLANGE)
        seed = tools.named("sketch_profile")[2]
        self.assertEqual(seed["plane_entity_token"], "top")
        self.assertEqual(seed["circles"], [{"center": [2.0, 0.0, 0.5], "diameter": 0.5}])
        cut = tools.named("extrude_profile")[1]
        self.assertEqual((cut["operation"], cut["target_bodies"]), ("cut", [FLANGE_REF]))
        self.assertEqual(cut["direction"], [0.0, 0.0, -1.0])
        bore = tools.named("find_entities")[0]
        self.assertEqual((bore["concavity"], bore["axis"]["through"]), ("concave", [0.0, 0.0, 0.5]))
        pattern = tools.named("pattern_features")[0]
        self.assertEqual(pattern["axis"], {"face": PILOT_ROW["ref"]})
        self.assertEqual(pattern["entities"], [{"feature": "Seed"}])
        self.assertEqual((pattern["count"], pattern["total_angle_deg"]), (6, 360))
        self.assertEqual(pattern["seed_volume_delta"], -0.1)
        self.assertEqual(pattern["seed_point"], [2.0, 0.0, 0.5])

    def test_the_top_follows_what_was_built_not_what_was_asked(self):
        script = flange_script()
        script[2] = extruded(name="Plate", end=("top",), normal=(0, 0, -1),
                             created=[{"ref": FLANGE_REF, "volume": 16.0}])
        _, tools = self.run_macro(flanged_plate.flanged_plate, *script, **{**FLANGE, "direction": [0, 0, -1]})
        self.assertEqual(tools.named("sketch_profile")[2]["circles"][0]["center"], [2.0, 0.0, -0.5])

    def test_all_the_rims_chamfer_in_one_call_from_one_find(self):
        rims = [edge_row(token=str(i)) for i in range(6)]
        script = flange_script() + [found(*rims), treated()]
        _, tools = self.run_macro(flanged_plate.flanged_plate, *script, **FLANGE, bolt_chamfer=0.03)
        call = tools.named("edge_treatment")[0]
        self.assertEqual(len(call["sets"]), 1)
        self.assertEqual(call["sets"][0]["edges"], [row["ref"] for row in rims])
        self.assertEqual(tools.named("find_entities")[1]["on_plane"]["point"], [0.0, 0.0, 0.5])

    def test_too_few_rims_stop_before_the_chamfer(self):
        script = flange_script() + [found(edge_row())]
        result, tools = self.run_macro(flanged_plate.flanged_plate, *script, **FLANGE, bolt_chamfer=0.03)
        self.assertEqual(tools.named("edge_treatment"), [])
        self.assertEqual(result["status"], "partial")
        self.assertIn("expected 6 match(es), found 1", result["stopped_at"]["why"])
        self.assertEqual(result["not_run"], ["bolt chamfer"])

    def test_a_suspect_pattern_stops_and_keeps_the_plate(self):
        script = flange_script()[:-1] + [patterned("suspect", warnings=["asked for 6 instances, Fusion made 5"])]
        result, _ = self.run_macro(flanged_plate.flanged_plate, *script, **FLANGE)
        self.assertEqual(result["status"], "suspect", "the last step ran, and a check disagrees")
        self.assertIs(result["ok"], False)
        self.assertEqual([row["name"] for row in result["built"]["features"]], ["Plate", "Seed", "Pattern"])

    def test_a_refused_pattern_keeps_the_seed(self):
        script = flange_script()[:-1] + [refused("pattern_features")]
        result, _ = self.run_macro(flanged_plate.flanged_plate, *script, **FLANGE)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([row["name"] for row in result["built"]["features"]], ["Plate", "Seed"])
        self.assertEqual(result["body"], FLANGE_REF)

    def test_a_pilot_outside_the_outline_stops_before_the_extrude(self):
        result, tools = self.run_macro(flanged_plate.flanged_plate, sketched("outline"), pilot_sketch(loops=1),
                                       **FLANGE)
        self.assertEqual(tools.named("extrude_profile"), [])
        self.assertIn("not inside the outline", result["stopped_at"]["why"])

    def test_a_seed_that_removed_nothing_stops_before_the_pattern(self):
        script = flange_script()[:4] + [extruded(name="Seed", delta=0.0, end=())]
        result, tools = self.run_macro(flanged_plate.flanged_plate, *script, **FLANGE)
        self.assertEqual(tools.named("pattern_features"), [])
        self.assertEqual(result["stopped_at"]["step"], "seed hole")
        self.assertIn("removed nothing", result["stopped_at"]["why"])

    def test_two_new_bodies_stop_it(self):
        script = flange_script()[:2] + [extruded(name="Plate", created=[{"ref": FLANGE_REF}, {"ref": FLANGE_REF}])]
        result, _ = self.run_macro(flanged_plate.flanged_plate, *script, **FLANGE)
        self.assertEqual(result["stopped_at"]["step"], "plate")

    def test_a_plane_token_and_a_sub_component(self):
        created = [{"ref": {"occurrence_path": "Sub:1", "body_name": "Flange"}, "volume": 16.0}]
        script = flange_script()
        script[2] = extruded(name="Plate", end=("top",), created=created)
        arguments = {**FLANGE, "occurrence_path": "Sub:1"}
        del arguments["plane"]
        result, tools = self.run_macro(flanged_plate.flanged_plate, *script, **arguments, plane_token="p1")
        self.assertEqual(result["status"], "built")
        self.assertEqual(tools.named("sketch_profile")[0]["plane_entity_token"], "p1")
        for call in tools.named("sketch_profile") + tools.named("extrude_profile"):
            self.assertEqual(call["occurrence_path"], "Sub:1")

    def test_arcs_reach_the_outline(self):
        arcs = [{"index": 0, "mid_point": [0, -3.5, 0]}]
        _, tools = self.run_macro(flanged_plate.flanged_plate, *flange_script(), **FLANGE, arcs=arcs)
        self.assertEqual(tools.named("sketch_profile")[0]["arcs"], arcs)

    def test_refusals(self):
        cases = [("at least three", {"outline": [[0, 0, 0], [1, 0, 0]]}),
                 ("'outline'", {"outline": [[0, 0], [1, 0], [1, 1]]}),
                 ("exactly one of", {"plane": None}),
                 ("exactly one of", {"plane_token": "p"}),
                 ("'plane' must be", {"plane": "top"}),
                 ("'plane' must be", {"plane": ["xy"]}),
                 ("does not run along", {"direction": [1, 0, 0]}),
                 ("no length", {"direction": [0, 0, 0]}),
                 ("'bolt_count'", {"bolt_count": 1}),
                 ("'bolt_count'", {"bolt_count": 2.0}),
                 ("'bolt_count'", {"bolt_count": True}),
                 ("not on one plane", {"first_bolt": [2, 0, 0.1]}),
                 ("break into the pilot", {"first_bolt": [1.2, 0, 0]}),
                 ("break into the pilot", {"first_bolt": [0, 0, 0]}),
                 ("overlap", {"bolt_count": 40}),
                 ("above zero", {"thickness": 0}),
                 ("non-empty string", {"body_name": ""})]
        for text, change in cases:
            with self.subTest(change=change):
                self.refuses(flanged_plate.flanged_plate, text, **{**FLANGE, **change})

    def test_a_chamfer_counts_toward_the_clearances(self):
        self.refuses(flanged_plate.flanged_plate, "overlap", **{**FLANGE, "bolt_count": 12, "bolt_diameter": 1.0,
                                                                "pilot_diameter": 0.5, "bolt_chamfer": 0.05})
        self.assertIsNone(flanged_plate.bolt_problem({"first_bolt": (2, 0, 0), "pilot_center": (0, 0, 0),
                                                      "direction": (0, 0, 1), "tolerance": 0.001, "bolt_chamfer": None,
                                                      "bolt_diameter": 1.0, "pilot_diameter": 0.5, "bolt_count": 12}))

    def test_bolt_clearance_at_its_limits(self):
        args = {"first_bolt": (1.0, 0, 0), "pilot_center": (0, 0, 0), "direction": (0, 0, 1), "tolerance": 0.001,
                "bolt_chamfer": None, "bolt_diameter": 1.0, "pilot_diameter": 1.0, "bolt_count": 3}
        self.assertIn("pilot", flanged_plate.bolt_problem(args), "holes tangent to the pilot")
        self.assertIsNone(flanged_plate.bolt_problem({**args, "pilot_diameter": 0.999}))
        self.assertIn("overlap", flanged_plate.bolt_problem({**args, "pilot_diameter": 0.5, "bolt_count": 6}),
                      "at six on this circle the chord equals the diameter")


# -- gusset ---------------------------------------------------------------------


def plane(point, normal):
    return {"type": "plane", "point": tuple(point), "normal": tuple(normal)}


def cylinder(origin, axis):
    return {"type": "cylinder", "origin": tuple(origin), "axis": tuple(axis)}


FLOOR, WALL = plane((5, 5, 0), (0, 0, 1)), plane((0, 5, 5), (1, 0, 0))
GROUND, POST = plane((0, 0, 0), (0, 0, 1)), cylinder((2, 3, 9), (0, 0, 1))
RAIL = cylinder((0, 0, 0), (1, 0, 0))
TOL = 0.001


class CornerFrameTests(unittest.TestCase):
    def assert_vector(self, found, wanted):
        for a, b in zip(found, wanted):
            self.assertAlmostEqual(a, b, 9)

    def test_two_square_planes(self):
        frame = gusset.corner_frame(FLOOR, WALL, (3, 2, 0), (0, 2, 4), TOL)
        self.assert_vector(frame["corner"], (0, 2, 0))
        self.assert_vector(frame["dir_a"], (1, 0, 0))
        self.assert_vector(frame["dir_b"], (0, 0, 1))
        self.assert_vector(frame["normal"], cross(frame["dir_a"], frame["dir_b"]))

    def test_directions_run_toward_the_points_whichever_way_the_cross_products_fall(self):
        frame = gusset.corner_frame(WALL, FLOOR, (0, 2, 4), (3, 2, 0), TOL)
        self.assert_vector(frame["dir_a"], (0, 0, 1))
        self.assert_vector(frame["dir_b"], (1, 0, 0))

    def test_the_corner_lies_on_both_planes_at_an_oblique_angle(self):
        """A floor and a wall leaning 60 degrees off it, the corner line not through the origin."""
        s, c = math.sin(math.radians(60)), math.cos(math.radians(60))
        lean = plane((7.0, 0.0, 0.0), (-s, 0.0, -c))
        up = (7.0 - c * 4.0, 2.0, s * 4.0)
        frame = gusset.corner_frame(GROUND, lean, (3, 2, 0), up, TOL)
        self.assert_vector(frame["corner"], (7, 2, 0))
        self.assert_vector(frame["dir_a"], (-1, 0, 0))
        self.assert_vector(frame["dir_b"], (-c, 0, s))
        self.assertAlmostEqual(dot(sub(frame["corner"], lean["point"]), lean["normal"]), 0.0, 9)
        self.assertAlmostEqual(dot(frame["dir_a"], frame["dir_b"]), c, 9)

    def test_an_outside_corner_is_refused(self):
        """Two faces of a block facing away from each other: nothing sits between them."""
        with self.assertRaisesRegex(ValueError, "outside corner"):
            gusset.corner_frame(GROUND, plane((0, 0, 0), (1, 0, 0)), (-3, 2, 0), (0, 2, -4), TOL)

    def test_parallel_planes(self):
        with self.assertRaisesRegex(ValueError, "parallel"):
            gusset.corner_frame(FLOOR, plane((0, 0, 5), (0, 0, -1)), (1, 0, 0), (1, 0, 5), TOL)

    def test_points_that_do_not_share_a_plane_across_the_corner(self):
        with self.assertRaisesRegex(ValueError, "apart along the corner line"):
            gusset.corner_frame(FLOOR, WALL, (3, 2, 0), (0, 2.5, 4), TOL)
        gusset.corner_frame(FLOOR, WALL, (3, 2, 0), (0, 2 + TOL, 4), TOL)

    def test_a_point_on_the_corner_gives_no_direction(self):
        with self.assertRaisesRegex(ValueError, "'point_a' is at the corner"):
            gusset.corner_frame(FLOOR, WALL, (0, 2, 0), (0, 2, 4), TOL)

    def test_two_tubes_meeting(self):
        frame = gusset.corner_frame(cylinder((-9, 1, 2), (1, 0, 0)), cylinder((4, 1, -9), (0, 0, 1)),
                                    (9, 1.5, 2), (4, 1.5, 7), TOL)
        self.assert_vector(frame["corner"], (4, 1, 2))
        self.assert_vector(frame["dir_a"], (1, 0, 0))
        self.assert_vector(frame["dir_b"], (0, 0, 1))
        self.assert_vector(frame["normal"], (0, -1, 0))

    def test_a_tube_axis_pointing_away_flips_to_its_point(self):
        frame = gusset.corner_frame(cylinder((9, 1, 2), (1, 0, 0)), cylinder((4, 1, 9), (0, 0, 1)),
                                    (0, 1.5, 2), (4, 1.5, 0), TOL)
        self.assert_vector(frame["dir_a"], (-1, 0, 0))
        self.assert_vector(frame["dir_b"], (0, 0, -1))

    def test_tubes_at_an_angle(self):
        s = math.sqrt(0.5)
        frame = gusset.corner_frame(RAIL, cylinder((3, 3, 0), (s, s, 0)),
                                    (5, 0, 0.5), (4, 4, 0.5), TOL)
        self.assert_vector(frame["corner"], (0, 0, 0))
        self.assert_vector(frame["dir_b"], (s, s, 0))

    def test_skew_tubes_are_refused_past_the_tolerance(self):
        a, b = RAIL, cylinder((4, 0.1, 0), (0, 0, 1))
        with self.assertRaisesRegex(ValueError, r"miss each other by 0\.1"):
            gusset.corner_frame(a, b, (9, 0, 0), (4, 0.1, 5), TOL)
        frame = gusset.corner_frame(a, b, (9, 0, 0), (4, 0.1, 5), 0.2)
        self.assert_vector(frame["corner"], (4, 0.05, 0))

    def test_parallel_tubes(self):
        with self.assertRaisesRegex(ValueError, "parallel"):
            gusset.corner_frame(RAIL, cylinder((0, 5, 0), (1, 0, 0)), (1, 0, 0), (1, 5, 0), TOL)

    def test_a_tube_standing_on_a_plate(self):
        """The axis is the plate's normal, so only the plate's point says which way the gusset faces."""
        frame = gusset.corner_frame(GROUND, POST, (2, 8, 0), (2.5, 3, 4), TOL)
        self.assert_vector(frame["corner"], (2, 3, 0))
        self.assert_vector(frame["dir_a"], (0, 1, 0))
        self.assert_vector(frame["dir_b"], (0, 0, 1))
        self.assert_vector(frame["normal"], (1, 0, 0))

    def test_the_same_with_the_faces_named_the_other_way(self):
        frame = gusset.corner_frame(POST, GROUND, (2.5, 3, 4), (2, 8, 0), TOL)
        self.assert_vector(frame["dir_a"], (0, 0, 1))
        self.assert_vector(frame["dir_b"], (0, 1, 0))

    def test_a_tube_leaning_on_a_plate_meets_it_where_the_axis_does(self):
        s = math.sqrt(0.5)
        frame = gusset.corner_frame(plane((0, 0, 1), (0, 0, 1)), cylinder((0, 0, 0), (s, 0, s)), (8, 0, 1),
                                    (4, 0, 4.5), TOL)
        self.assert_vector(frame["corner"], (1, 0, 1))
        self.assert_vector(frame["dir_a"], (1, 0, 0))
        self.assert_vector(frame["dir_b"], (s, 0, s))

    def test_a_tube_behind_the_plate_is_refused(self):
        with self.assertRaisesRegex(ValueError, "outside corner"):
            gusset.corner_frame(GROUND, POST, (2, 8, 0), (2.5, 3, -4), TOL)

    def test_an_axis_along_the_plate_never_meets_it(self):
        with self.assertRaisesRegex(ValueError, "never meets"):
            gusset.corner_frame(GROUND, cylinder((0, 0, 1), (1, 0, 0)), (2, 8, 0), (4, 0, 1.5), TOL)

    def test_a_plate_point_right_under_the_tube_gives_no_direction(self):
        with self.assertRaisesRegex(ValueError, "plane's point is at the corner"):
            gusset.corner_frame(GROUND, POST, (2, 3, 0), (2.5, 3, 4), TOL)


class PlateOutlineTests(unittest.TestCase):
    def test_runs_back_along_the_bisector_and_out_to_each_tip(self):
        frame = {"corner": (0, 2, 0), "dir_a": (1, 0, 0), "dir_b": (0, 0, 1), "normal": (0, -1, 0)}
        start, tip_a, tip_b = gusset.plate_outline(frame, 3.0, 4.0, 0.2)
        s = 0.2 * math.sqrt(0.5)
        for a, b in zip(start, (-s, 2, -s)):
            self.assertAlmostEqual(a, b, 9)
        self.assertEqual(tip_a, (3.0, 2.0, 0.0))
        self.assertEqual(tip_b, (0.0, 2.0, 4.0))

    def test_stays_on_the_gusset_plane(self):
        s = math.sqrt(0.5)
        frame = {"corner": (1, 1, 1), "dir_a": (s, s, 0), "dir_b": (0, 0, 1), "normal": (s, -s, 0)}
        for point in gusset.plate_outline(frame, 2.0, 2.0, 0.3):
            self.assertAlmostEqual(dot(sub(point, frame["corner"]), frame["normal"]), 0.0, 9)


GUSSET = {"body_a": TUBE_A, "body_b": TUBE_B, "point_a": [3, 2, 0], "point_b": [0, 2, 4], "leg_a": 2.0, "leg_b": 3.0,
          "thickness": 0.125, "overlap": 0.1}
GUSSET_REF = {"occurrence_path": "root", "body_name": "Gusset"}


def planed(status="built", **extra):
    return ("construction_plane", {"status": status, "ok": status == "built", "feature": {"name": "Gusset plane"},
                                   "plane": {"token": "plane", "name": "Gusset plane", "normal": [0, -1, 0]},
                                   "anchors": [{"sketch": "Gusset plane_anchors"}], "reused": False, "warnings": [],
                                   **extra})


def coped(*rows, status="built"):
    rows = rows or ({"ref": GUSSET_REF, "volume": 0.3, "lumps": 1, "faces": 5},)
    return ("combine_bodies", {"status": status, "ok": status == "built", "feature": {"name": "Cope"},
                               "bodies": {"touched": [], "created": []}, "result": {"bodies": list(rows)},
                               "stale_refs": [], "warnings": []})


def gusset_script(cope=None):
    return [found(face_row((5, 5, 0), (0, 0, 1), "a")), found(face_row((0, 5, 5), (1, 0, 0), "b")), planed(),
            sketched("outline"), extruded(name="Plate", created=[{"ref": GUSSET_REF, "volume": 0.5}]),
            cope or coped(), checked()]


class GussetTests(MacroCase):
    def test_every_step_in_order(self):
        result, tools = self.run_macro(gusset.gusset, *gusset_script(), **GUSSET)
        self.assertEqual(tools.order, ["find_entities", "find_entities", "construction_plane", "sketch_profile",
                                       "extrude_profile", "combine_bodies", "check_solids"])
        self.assertEqual(result["status"], "built")
        self.assertEqual(result["body"], GUSSET_REF)
        self.assertEqual(result["stray_bodies"], [])
        self.assertEqual(result["gusset"]["corner"], [0.0, 2.0, 0.0])
        self.assertEqual(result["gusset"]["tip_a"], [2.0, 2.0, 0.0])
        self.assertEqual(result["gusset"]["tip_b"], [0.0, 2.0, 3.0])

    def test_the_plane_outline_and_plate(self):
        _, tools = self.run_macro(gusset.gusset, *gusset_script(), **GUSSET)
        plane_call = tools.named("construction_plane")[0]
        self.assertEqual(plane_call["mode"], "point_normal")
        self.assertEqual(plane_call["name"], "Gusset plane")
        self.assertIs(plane_call["unique_name"], True)
        self.assertEqual(plane_call["point"], [0.0, 2.0, 0.0])
        self.assertEqual(plane_call["normal"], [0.0, -1.0, 0.0])
        outline = tools.named("sketch_profile")[0]
        self.assertEqual((outline["plane_entity_token"], outline["space"]), ("plane", "world"))
        self.assertEqual(outline["points"][1:], [[2.0, 2.0, 0.0], [0.0, 2.0, 3.0]])
        plate = tools.named("extrude_profile")[0]
        self.assertEqual(plate["extent"], {"type": "symmetric", "distance": 0.125, "full_length": True})
        self.assertEqual(plate["body_name"], "Gusset")
        self.assertNotIn("direction", plate)

    def test_the_cope_keeps_both_tools_and_the_check_looks_at_both(self):
        _, tools = self.run_macro(gusset.gusset, *gusset_script(), **GUSSET)
        cope = tools.named("combine_bodies")[0]
        self.assertEqual((cope["target"], cope["tools"]), (GUSSET_REF, [TUBE_A, TUBE_B]))
        self.assertEqual((cope["operation"], cope["keep_tools"]), ("cut", True))
        check = tools.named("check_solids")[0]
        self.assertIn("interference", check["checks"])
        self.assertEqual(check["interference_pairs"], [[GUSSET_REF, TUBE_A], [GUSSET_REF, TUBE_B]])

    def test_one_body_twice_is_one_tool(self):
        _, tools = self.run_macro(gusset.gusset, *gusset_script(), **{**GUSSET, "body_b": TUBE_A})
        self.assertEqual(tools.named("combine_bodies")[0]["tools"], [TUBE_A])

    def test_two_tubes_end_to_end(self):
        script = gusset_script()
        script[0] = found(tube_row((-9, 2, 0), (1, 0, 0), "a"))
        script[1] = found(tube_row((0, 2, -9), (0, 0, 1), "b"))
        arguments = {**GUSSET, "point_a": [3, 2.5, 0], "point_b": [0, 2.5, 4]}
        result, tools = self.run_macro(gusset.gusset, *script, **arguments)
        self.assertEqual(result["gusset"]["corner"], [0.0, 2.0, 0.0])
        self.assertEqual(tools.named("find_entities")[0]["surface_types"], ["plane", "cylinder"])

    def test_stray_pieces_stay_listed_and_make_it_suspect(self):
        stray = {"occurrence_path": "root", "body_name": "Gusset 2"}
        cope = coped({"ref": stray, "volume": 0.01, "lumps": 1}, {"ref": GUSSET_REF, "volume": 0.3, "lumps": 1})
        result, tools = self.run_macro(gusset.gusset, *gusset_script(cope), **GUSSET)
        self.assertEqual(result["body"], GUSSET_REF, "the largest piece, wherever it is in the list")
        self.assertEqual(result["stray_bodies"], [stray])
        self.assertEqual(result["status"], "suspect")
        self.assertIn("stray piece", result["warnings"][0])
        self.assertEqual(tools.named("check_solids")[0]["bodies"], [GUSSET_REF])

    def test_a_fillet_goes_on_the_largest_piece_by_the_callers_points(self):
        script = gusset_script()
        script.insert(6, treated())
        points = [[1.0, 2.0, 1.0]]
        _, tools = self.run_macro(gusset.gusset, *script, **GUSSET, fillet_radius=0.25, fillet_points=points)
        call = tools.named("edge_treatment")[0]
        self.assertEqual(call["kind"], "fillet")
        self.assertEqual(call["sets"], [{"points": points, "body": GUSSET_REF, "size": 0.25}])

    def test_bad_geometry_stops_before_anything_is_built(self):
        script = [found(face_row((5, 5, 0), (0, 0, 1), "a")), found(face_row((5, 5, 3), (0, 0, -1), "b"))]
        result, tools = self.run_macro(gusset.gusset, *script, **GUSSET)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(tools.named("construction_plane"), [])
        self.assertEqual(result["stopped_at"]["step"], "corner")
        self.assertIn("parallel", result["refusals"][0])

    def test_a_refused_plane_keeps_its_anchors_and_says_so(self):
        script = gusset_script()[:2] + [refused("construction_plane", anchors=[{"sketch": "x_anchors"}])]
        result, _ = self.run_macro(gusset.gusset, *script, **GUSSET)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["built"]["anchors"], [{"sketch": "x_anchors"}])

    def test_a_refused_cope_keeps_the_uncoped_plate(self):
        script = gusset_script()[:5] + [refused("combine_bodies", "tool overlaps nothing")]
        result, _ = self.run_macro(gusset.gusset, *script, **GUSSET)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["built"]["bodies"], [{"ref": GUSSET_REF, "volume": 0.5}])
        self.assertEqual(result["not_run"], ["check"])
        self.assertIn("1 planes", result["kept"])

    def test_a_suspect_cope_stops(self):
        script = gusset_script()[:5] + [coped(status="suspect")]
        result, tools = self.run_macro(gusset.gusset, *script, **GUSSET)
        self.assertEqual(tools.named("check_solids"), [])
        self.assertEqual(result["status"], "partial")

    def test_refusals(self):
        cases = [("share an occurrence", {"body_b": SUB_BODY}),
                 ("'overlap' is required", {"overlap": None}),
                 ("above zero", {"leg_a": 0}),
                 ("go together", {"fillet_radius": 0.25}),
                 ("go together", {"fillet_points": [[0, 0, 0]]}),
                 ("non-empty", {"fillet_radius": 0.25, "fillet_points": []}),
                 ("'fillet_points'", {"fillet_radius": 0.25, "fillet_points": [[0, 0]]}),
                 ("'body_name'", {"body_name": " "})]
        for text, change in cases:
            with self.subTest(change=change):
                self.refuses(gusset.gusset, text, **{**GUSSET, **change})


# -- drilled_hole ---------------------------------------------------------------


HOLE = {"body": BODY, "point": [1.0, 1.0, 1.0], "diameter": 0.5}
HOLE_FACE = face_row([0, 0, 1], [0, 0, 1], "top")
THREAD = {"family": "ANSI Unified Screw Threads", "designation": "1/4-20 UNC", "modeled": False}
BORE_ROW = tube_row([1, 1, 0], [0, 0, 1], "bore")


def catalog():
    return ("apply_thread", {"status": "done", "ok": True, "classes": [{"class": "2B", "minor": 0.2}]})


def threaded(status="built", **extra):
    return ("apply_thread", {"status": status, "ok": status == "built", "feature": {"name": "Thread"},
                             "bodies": {"touched": [], "created": []}, "thread": {"designation": "1/4-20 UNC"},
                             "resized": {"from": 0.2, "to": 0.2015}, "volume_delta": -0.001, "warnings": [], **extra})


def hole_script():
    return [found(HOLE_FACE), sketched("hole"), extruded(name="Hole", end=())]


class CountersinkMathTests(unittest.TestCase):
    def test_82_degrees(self):
        on_face, down_bore = drilled_hole.countersink_distances(0.25, 0.5, 82.0)
        self.assertAlmostEqual(on_face, 0.125, 12)
        self.assertAlmostEqual(down_bore, 0.125 / math.tan(math.radians(41.0)), 12)
        self.assertAlmostEqual(down_bore, 0.143796, 5)

    def test_90_degrees_is_equal(self):
        on_face, down_bore = drilled_hole.countersink_distances(1.0, 1.4, 90.0)
        self.assertAlmostEqual(on_face, 0.2, 12)
        self.assertAlmostEqual(down_bore, 0.2, 12)

    def test_the_cone_is_the_angle_asked_for(self):
        """Live ET-3: 0.1 on the face, 0.2 down the bore read a half angle of 26.565."""
        on_face, down_bore = drilled_hole.countersink_distances(1.0, 1.2, 2.0 * 26.565051)
        self.assertAlmostEqual(on_face, 0.1, 12)
        self.assertAlmostEqual(down_bore, 0.2, 6)


class DrilledHoleTests(MacroCase):
    def test_a_bare_through_hole(self):
        result, tools = self.run_macro(drilled_hole.drilled_hole, *hole_script(), **HOLE)
        self.assertEqual(tools.order, ["find_entities", "sketch_profile", "extrude_profile"])
        self.assertEqual(result["status"], "built")
        cut = tools.named("extrude_profile")[0]
        self.assertEqual((cut["operation"], cut["extent"]), ("cut", {"type": "through_all"}))
        self.assertEqual(cut["direction"], [0.0, 0.0, -1.0])
        self.assertEqual(cut["containing_point"], [1.0, 1.0, 1.0])
        self.assertEqual(tools.named("sketch_profile")[0]["plane_entity_token"], "top")

    def test_a_blind_hole(self):
        _, tools = self.run_macro(drilled_hole.drilled_hole, *hole_script(), **HOLE, depth=0.4)
        self.assertEqual(tools.named("extrude_profile")[0]["extent"], {"type": "distance", "distance": 0.4})

    def test_the_normal_narrows_the_face(self):
        _, tools = self.run_macro(drilled_hole.drilled_hole, *hole_script(), **HOLE, normal=[0, 0, 2])
        self.assertEqual(tools.named("find_entities")[0]["normal"], {"direction": [0.0, 0.0, 1.0]})

    def test_a_plain_chamfer_is_one_size(self):
        script = hole_script() + [found(edge_row()), treated()]
        result, tools = self.run_macro(drilled_hole.drilled_hole, *script, **HOLE, chamfer=0.03)
        self.assertEqual(tools.named("edge_treatment")[0]["sets"], [{"edges": [edge_row()["ref"]], "size": 0.03}])
        self.assertEqual(result["hole"]["rim_on_face"], 0.03)

    def test_a_countersink_is_found_again_after_the_cut_and_chamfered_at_two_distances(self):
        new_top = face_row([0, 0, 1], [0, 0, 1], "new-top")
        script = hole_script() + [found(edge_row()), found(new_top), treated(measured=[{"verdict": "ok"}])]
        result, tools = self.run_macro(drilled_hole.drilled_hole, *script, **HOLE, countersink_diameter=1.0,
                                       countersink_angle=82.0)
        self.assertEqual(result["status"], "built")
        self.assertEqual(tools.order, ["find_entities", "sketch_profile", "extrude_profile", "find_entities",
                                       "find_entities", "edge_treatment"])
        chosen = tools.named("edge_treatment")[0]["sets"][0]
        self.assertEqual(chosen["size_on_face"], new_top["ref"], "the face found after the cut, not before")
        self.assertAlmostEqual(chosen["size"], 0.25, 12)
        self.assertAlmostEqual(chosen["size_two"], 0.25 / math.tan(math.radians(41.0)), 12)
        at_rim = tools.named("find_entities")[2]
        self.assertEqual(at_rim["near"]["point"], [0.5, 0.0, 1.0], "at the rim: a coplanar face elsewhere cannot match")

    def test_a_chamfer_nothing_could_measure_is_not_read_as_passed(self):
        script = hole_script() + [found(edge_row()), found(HOLE_FACE), treated(measured=[{"verdict": "not_measured"}])]
        result, _ = self.run_macro(drilled_hole.drilled_hole, *script, **HOLE, countersink_diameter=1.0,
                                   countersink_angle=90.0)
        self.assertEqual(result["status"], "suspect")
        self.assertIn("nothing could measure", result["warnings"][0])

    def test_thread_comes_last_on_the_bore_found_after_the_chamfer(self):
        script = [catalog()] + hole_script() + [found(edge_row()), treated(), found(BORE_ROW), threaded()]
        result, tools = self.run_macro(drilled_hole.drilled_hole, *script, **{**HOLE, "diameter": 0.2}, chamfer=0.02,
                                       thread=THREAD)
        self.assertEqual(tools.order, ["apply_thread", "find_entities", "sketch_profile", "extrude_profile",
                                       "find_entities", "edge_treatment", "find_entities", "apply_thread"])
        self.assertEqual(result["status"], "built")
        lookup, applied = tools.named("apply_thread")
        self.assertEqual(lookup["action"], "catalog")
        self.assertEqual((applied["action"], applied["internal"], applied["modeled"]), ("apply", True, False))
        self.assertEqual(applied["face"], BORE_ROW["ref"])
        self.assertEqual(applied["designation"], "1/4-20 UNC")
        self.assertEqual(result["hole"]["resized"], {"from": 0.2, "to": 0.2015})

    def test_the_bore_search_reaches_the_rim_as_it_now_stands(self):
        script = [catalog()] + hole_script() + [found(edge_row()), treated(), found(BORE_ROW), threaded()]
        _, tools = self.run_macro(drilled_hole.drilled_hole, *script, **{**HOLE, "diameter": 0.6}, chamfer=0.4,
                                  thread=THREAD, tolerance=0.01)
        bore = tools.named("find_entities")[2]
        self.assertAlmostEqual(bore["near"]["within"], 0.51, 12, "hypot(0.3, 0.4) plus the tolerance")
        self.assertEqual(bore["diameter"], {"min": 0.59, "max": 0.61})

    def test_a_bad_designation_stops_before_any_cut(self):
        result, tools = self.run_macro(drilled_hole.drilled_hole, refused("apply_thread", "unknown designation"),
                                       **HOLE, thread=THREAD)
        self.assertEqual(tools.order, ["apply_thread"])
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["not_run"], ["face", "hole sketch", "hole", "bore", "thread"])

    def test_a_refused_thread_keeps_the_hole(self):
        script = [catalog()] + hole_script() + [found(BORE_ROW), refused("apply_thread", "face is 5% off the minor")]
        result, _ = self.run_macro(drilled_hole.drilled_hole, *script, **HOLE, thread=THREAD)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([row["name"] for row in result["built"]["features"]], ["Hole"])
        self.assertIn("5% off", result["stopped_at"]["why"])
        self.assertIsNone(result["hole"]["resized"])

    def test_a_suspect_thread_at_the_end_is_suspect(self):
        script = [catalog()] + hole_script() + [found(BORE_ROW), threaded("suspect", warnings=["modeled, no change"])]
        result, _ = self.run_macro(drilled_hole.drilled_hole, *script, **HOLE, thread=THREAD)
        self.assertEqual(result["status"], "suspect")
        self.assertIs(result["ok"], False)
        self.assertIn("thread: modeled, no change", result["warnings"])

    def test_a_lost_rim_stops_before_the_chamfer(self):
        script = hole_script() + [found()]
        result, tools = self.run_macro(drilled_hole.drilled_hole, *script, **HOLE, chamfer=0.03)
        self.assertEqual(tools.named("edge_treatment"), [])
        self.assertEqual(result["status"], "partial")

    def test_refusals(self):
        cases = [("one of 'chamfer'", {"chamfer": 0.1, "countersink_diameter": 1.0, "countersink_angle": 82}),
                 ("go together", {"countersink_diameter": 1.0}),
                 ("go together", {"countersink_angle": 82}),
                 ("above 'diameter'", {"countersink_diameter": 0.5, "countersink_angle": 82}),
                 ("below 180", {"countersink_diameter": 1.0, "countersink_angle": 180}),
                 ("past 'depth'", {"depth": 0.1, "chamfer": 0.1}),
                 ("past 'depth'", {"depth": 0.2, "countersink_diameter": 1.0, "countersink_angle": 82}),
                 ("'thread' must be", {"thread": "1/4-20"}),
                 ("'thread.family'", {"thread": {"designation": "x", "modeled": False}}),
                 ("'thread.designation'", {"thread": {"family": "x", "modeled": False}}),
                 ("'thread.modeled'", {"thread": {"family": "x", "designation": "y"}}),
                 ("'thread' does not take", {"thread": {**THREAD, "face": {}}}),
                 ("'thread' does not take", {"thread": {**THREAD, "internal": False}})]
        for text, change in cases:
            with self.subTest(change=change):
                self.refuses(drilled_hole.drilled_hole, text, **{**HOLE, **change})


# -- The namespace --------------------------------------------------------------


class NamespaceTests(unittest.TestCase):
    def test_scripts_get_macros_beside_primitives(self):
        with mock.patch.object(python_exec, "get_app", mock.Mock()):
            scope = python_exec._build_runtime_globals()
        self.assertIsInstance(scope["macros"], macros.Macros)
        self.assertIsInstance(scope["macros"]._tools, primitives.Primitives)

    def test_one_method_per_macro_and_no_tools_added(self):
        names = {name for name in vars(macros.Macros) if not name.startswith("_")}
        self.assertEqual(names, {"bored_boss", "flanged_plate", "gusset", "drilled_hole"})

    def test_macros_never_raise_on_bad_arguments(self):
        tools = FakeTools()
        namespace = macros.Macros(tools)
        for name in ("bored_boss", "flanged_plate", "gusset", "drilled_hole"):
            for arguments in ({}, {"tools": 1}, {"units": []}, {"body": 3}, {"point": "x"}):
                with self.subTest(name=name, arguments=arguments):
                    self.assertEqual(getattr(namespace, name)(**arguments)["status"], "refused")
        self.assertEqual(tools.calls, [])

    def test_a_macro_runs_through_the_real_namespace_and_handlers(self):
        """The fakes stop at the tool boundary; this one crosses it, keyword to handler to payload."""
        refusal = tool_result({"error": "refused", "problems": ["no"]}, True)
        handlers = {"find_entities": mock.Mock(return_value=ok_result(found(BOSS_FACE)[1])),
                    "sketch_profile": mock.Mock(return_value=refusal)}
        with mock.patch.dict(primitives.HANDLERS, handlers):
            result = macros.Macros().bored_boss(**BOSS)
        self.assertEqual(result["status"], "refused")
        self.assertEqual([row["name"] for row in result["steps"]], ["find_entities", "sketch_profile"])
        self.assertEqual(handlers["find_entities"].call_args.args[0]["units"], "in")
        self.assertEqual(result["refusals"], ["no"])
