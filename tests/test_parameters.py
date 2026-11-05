"""parameters tool: expression parsing, dependency graph, list/detail/set with fakes."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import parameter_graph as graph
from fusion_bridge import parameters, tool_surface


class ExpressionIdentifierTests(unittest.TestCase):
    def ids(self, expression):
        return graph.expression_identifiers(expression)

    def test_plain_reference_and_arithmetic(self):
        self.assertEqual(self.ids("width / 2 + wall*3"), ["width", "wall"])

    def test_units_after_numbers_are_skipped(self):
        for expression in ("1.75 in", "2in", "45 deg", "3.2mm + od", "10 cm*od"):
            with self.subTest(expression=expression):
                self.assertEqual([i for i in self.ids(expression) if i != "od"], [])
        self.assertEqual(self.ids("3.2mm + od"), ["od"])

    def test_unit_after_parenthesis_is_skipped(self):
        self.assertEqual(self.ids("(a + b) mm"), ["a", "b"])

    def test_unit_words_alone_are_not_references(self):
        self.assertEqual(self.ids("in + deg"), [])

    def test_functions_are_skipped(self):
        self.assertEqual(self.ids("sqrt(a^2 + b^2)"), ["a", "b"])
        self.assertEqual(self.ids("max (a; b) + sin(angle)"), ["a", "b", "angle"])
        self.assertEqual(self.ids("if (len < 20 mm; 'Short'; 'Long')"), ["len"])

    def test_numbers_including_scientific(self):
        self.assertEqual(self.ids("1.5e3 + .5 + 2E-2 * k"), ["k"])

    def test_constants(self):
        self.assertEqual(self.ids("PI * d"), ["d"])

    def test_text_literals_are_skipped(self):
        self.assertEqual(self.ids("'width ' + label"), ["label"])

    def test_feet_mark_is_not_a_string(self):
        self.assertEqual(self.ids("5' + a + 3'"), ["a"])

    def test_names_with_digits_underscores_and_unicode(self):
        self.assertEqual(self.ids("tube_OD2 + _x + länge"), ["tube_OD2", "_x", "länge"])

    def test_duplicates_dropped_order_kept(self):
        self.assertEqual(self.ids("b + a + b"), ["b", "a"])

    def test_empty_and_non_string(self):
        self.assertEqual(self.ids(""), [])
        self.assertEqual(self.ids(None), [])
        self.assertEqual(self.ids(5), [])

    def test_parameter_references_filters_known_and_self(self):
        refs = graph.parameter_references("a + b + typo + self", {"a", "b", "self"}, self_name="self")
        self.assertEqual(refs, ["a", "b"])


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.expressions = {
            "od": "1.75 in",
            "wall": "0.12 in",
            "id": "od - 2 * wall",
            "d1": "od",
            "d2": "id / 2",
            "d3": "d2 + 1 mm",
            "lonely": "5 mm",
        }
        self.dependents = graph.build_dependents(self.expressions)

    def test_build_dependents(self):
        self.assertEqual(self.dependents["od"], ["id", "d1"])
        self.assertEqual(self.dependents["wall"], ["id"])
        self.assertEqual(self.dependents["d2"], ["d3"])
        self.assertEqual(self.dependents["lonely"], [])

    def test_transitive_walk(self):
        nodes, truncated = graph.transitive_dependents("od", self.dependents.get)
        self.assertFalse(truncated)
        self.assertEqual([(n["name"], n["depth"], n["via"]) for n in nodes],
                         [("id", 1, "od"), ("d1", 1, "od"), ("d2", 2, "id"), ("d3", 3, "d2")])

    def test_depth_cap_reports_truncation(self):
        nodes, truncated = graph.transitive_dependents("od", self.dependents.get, max_depth=2)
        self.assertTrue(truncated)
        self.assertEqual([n["name"] for n in nodes], ["id", "d1", "d2"])

    def test_exact_depth_is_not_truncated(self):
        _, truncated = graph.transitive_dependents("od", self.dependents.get, max_depth=3)
        self.assertFalse(truncated)

    def test_node_cap(self):
        nodes, truncated = graph.transitive_dependents("od", self.dependents.get, max_nodes=2)
        self.assertTrue(truncated)
        self.assertEqual(len(nodes), 2)

    def test_cycle_safe(self):
        cyclic = {"a": ["b"], "b": ["c"], "c": ["a"]}
        nodes, truncated = graph.transitive_dependents("a", cyclic.get)
        self.assertEqual([n["name"] for n in nodes], ["b", "c"])
        self.assertFalse(truncated)

    def test_would_create_cycle(self):
        self.assertEqual(graph.would_create_cycle("od", ["d3", "wall"], self.dependents.get), ["d3"])
        self.assertEqual(graph.would_create_cycle("od", ["od"], self.dependents.get), ["od"])
        self.assertEqual(graph.would_create_cycle("od", ["wall"], self.dependents.get), [])

    def test_normalize_expression(self):
        self.assertEqual(graph.normalize_expression(" 2 in "), graph.normalize_expression("2in"))
        self.assertEqual(graph.normalize_expression(None), "")


class ValidationTests(unittest.TestCase):
    def bad(self, arguments, needle):
        with self.assertRaises(ValueError) as ctx:
            parameters.validate_arguments(arguments)
        self.assertIn(needle, str(ctx.exception))

    def test_defaults(self):
        args = parameters.validate_arguments({})
        self.assertEqual(args["action"], "list")
        self.assertFalse(args["include_model"])
        self.assertEqual(args["limit"], parameters.DEFAULT_LIMIT)
        self.assertTrue(parameters.validate_arguments(
            {"action": "set", "updates": [{"name": "a", "expression": "1"}]})["dry_run"])

    def test_rejections(self):
        self.bad({"action": "delete"}, "action")
        self.bad({"limit": 0}, "limit")
        self.bad({"limit": True}, "limit")
        self.bad({"depth": 21}, "depth")
        self.bad({"include_model": "yes"}, "include_model")
        self.bad({"name_contains": ""}, "name_contains")
        self.bad({"updates": []}, "Unexpected")
        self.bad({"action": "set", "include_model": True, "updates": [{"name": "a", "expression": "1"}]},
                 "Unexpected")
        self.bad({"action": "set"}, "updates")
        self.bad({"action": "set", "updates": [{"name": "a"}]}, "updates[0]")
        self.bad({"action": "set", "updates": [{"name": "a", "expression": " "}]}, "expression")
        self.bad({"action": "set", "updates": [{"name": "a", "expression": "1"}] * 2}, "more than once")
        self.bad({"action": "set", "updates": [{"name": f"p{i}", "expression": "1"} for i in range(51)]},
                 "at most")
        self.bad({"action": "set", "dry_run": "no", "updates": [{"name": "a", "expression": "1"}]}, "dry_run")

    def test_handler_rejects_without_touching_fusion(self):
        with patch.object(parameters, "get_app", side_effect=AssertionError("touched Fusion")):
            result = parameters.parameters({"action": "nope"})
        self.assertTrue(result["isError"])

    def test_schema(self):
        tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "parameters")
        schema = tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required", schema)
        self.assertEqual(schema["properties"]["action"]["enum"], ["list", "set"])
        self.assertEqual(set(schema["properties"]["updates"]["items"]["required"]), {"name", "expression"})
        # Every schema property is accepted by at least one action.
        self.assertEqual(set(schema["properties"]), parameters._LIST_ARGS | parameters._SET_ARGS)


# -- Fakes ---------------------------------------------------------------------


class FakeParam:
    def __init__(self, name, expression, value=1.0, unit="cm", comment="", favorite=False,
                 kind="user", created_by=None, role=None, reject=None, rollback=False):
        self.name = name
        self._expression = expression
        self.value = value
        self.unit = unit
        self.comment = comment
        self.isFavorite = favorite
        self.valueType = 0
        self.objectType = f"adsk::fusion::{'UserParameter' if kind == 'user' else 'ModelParameter'}"
        self.createdBy = created_by
        self.role = role
        self.component = SimpleNamespace(name="Root") if kind == "model" else None
        self.dependentParameters = []
        self.dependencyParameters = []
        self._reject = reject
        self._rollback = rollback

    @property
    def expression(self):
        return self._expression

    @expression.setter
    def expression(self, value):
        if self._reject:
            raise RuntimeError(self._reject)
        if not self._rollback:
            self._expression = value


class FakeUnits:
    internalUnits = "internalUnits"
    defaultLengthUnits = "in"

    def formatValue(self, value, unit):
        return f"{value / 2.54:.3f} in" if unit == "in" else f"{value:.3f} {unit}"

    def isValidExpression(self, expression, unit):
        return "??" not in expression


def make_design(link_api=True):
    """od -> id -> d2 (Extrude1); od -> d1 (Sketch1 dimension); wall -> id."""
    sketch = SimpleNamespace(name="Sketch1")
    dim = SimpleNamespace(objectType="adsk::fusion::SketchLinearDimension", name=None, parentSketch=sketch)
    extrude = SimpleNamespace(objectType="adsk::fusion::ExtrudeFeature", name="Extrude1")
    od = FakeParam("od", "1.75 in", value=4.445, unit="in", comment="Tube OD", favorite=True)
    wall = FakeParam("wall", "0.12 in", value=0.3048, unit="in", comment="wall thickness")
    idp = FakeParam("id", "od - 2 * wall", unit="in")
    d1 = FakeParam("d1", "od", unit="in", kind="model", created_by=dim, role="Linear Dimension-2")
    d2 = FakeParam("d2", "id / 2", unit="in", kind="model", created_by=extrude, role="AlongDistance")
    user = [od, wall, idp]
    everything = user + [d1, d2]
    if link_api:
        # Transitive, as in Fusion: od lists d2, which references id, not od.
        od.dependentParameters = [idp, d1, d2]
        wall.dependentParameters = [idp, d2]
        idp.dependentParameters = [d2]
        idp.dependencyParameters = [od, wall]
    else:
        for p in everything:
            del p.dependentParameters
            del p.dependencyParameters
    timeline = [SimpleNamespace(name="Sketch1", healthState=0), SimpleNamespace(name="Extrude1", healthState=2)]
    design = SimpleNamespace(userParameters=user, allParameters=everything, unitsManager=FakeUnits(),
                             timeline=timeline)
    return design, {p.name: p for p in everything}


class FakeFusion:
    """Patches get_app and the adsk.fusion members the handler uses."""

    def __init__(self, design):
        self.design = design

    def __enter__(self):
        import adsk.fusion
        app = SimpleNamespace(activeProduct=self.design, activeDocument=SimpleNamespace(name="Throwaway"))
        self._patches = [
            patch.object(parameters, "get_app", return_value=app),
            patch.object(adsk.fusion, "Design", SimpleNamespace(cast=lambda p: p), create=True),
            patch.object(adsk.fusion, "FeatureHealthStates", SimpleNamespace(
                HealthyFeatureHealthState=0, WarningFeatureHealthState=1, ErrorFeatureHealthState=2),
                create=True),
            patch.object(parameters, "log"),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


def call(design, arguments):
    with FakeFusion(design):
        result = parameters.parameters(arguments)
    return result["isError"], json.loads(result["content"][0]["text"]) if not result["content"][0][
        "text"].startswith("Error") else result["content"][0]["text"]


class OwnerTests(unittest.TestCase):
    def test_sketch_dimension_owner_is_sketch(self):
        dim = SimpleNamespace(objectType="adsk::fusion::SketchAngularDimension", name=None,
                              parentSketch=SimpleNamespace(name="Profile"))
        owner = parameters.describe_owner(dim)
        self.assertEqual(owner, {"type": "SketchAngularDimension", "name": None, "sketch": "Profile"})
        self.assertEqual(parameters.owner_label(owner), "Profile")

    def test_feature_owner_and_none(self):
        owner = parameters.describe_owner(SimpleNamespace(objectType="adsk::fusion::SweepFeature", name="Sweep3"))
        self.assertEqual(parameters.owner_label(owner), "Sweep3")
        self.assertIsNone(parameters.describe_owner(None))
        self.assertIsNone(parameters.owner_label(None))

    def test_owner_attribute_errors_are_tolerated(self):
        class Broken:
            objectType = "adsk::fusion::ConstructionPlane"

            @property
            def name(self):
                raise RuntimeError("dead object")
        self.assertEqual(parameters.owner_label(parameters.describe_owner(Broken())), "ConstructionPlane")


class FormatValueTests(unittest.TestCase):
    def test_internal_units_are_converted(self):
        self.assertEqual(parameters.format_value(FakeUnits(), FakeParam("od", "", value=4.445, unit="in")),
                         "1.750 in")

    def test_unitless(self):
        self.assertEqual(parameters.format_value(FakeUnits(), FakeParam("n", "3", value=3.0, unit="")), "3")

    def test_text_parameter(self):
        p = FakeParam("label", "'abc'", unit="")
        p.valueType = 1
        p.textValue = "abc"
        self.assertEqual(parameters.format_value(FakeUnits(), p), "abc")

    def test_falls_back_to_convert(self):
        units = SimpleNamespace(internalUnits="internalUnits", formatValue=lambda v, u: "",
                                convert=lambda v, a, b: v * 10)
        self.assertEqual(parameters.format_value(units, FakeParam("x", "", value=1.5, unit="mm")), "15 mm")


class ListTests(unittest.TestCase):
    def test_user_rows_with_dependents_and_drives(self):
        is_error, payload = call(make_design()[0], {})
        self.assertFalse(is_error)
        self.assertEqual(payload["document"], "Throwaway")
        self.assertEqual(payload["dependency_source"], "api")
        rows = {r["name"]: r for r in payload["user_parameters"]["rows"]}
        self.assertEqual(set(rows), {"od", "wall", "id"})
        self.assertNotIn("model_parameters", payload)
        od = rows["od"]
        self.assertEqual(od["value"], "1.750 in")
        self.assertEqual(od["comment"], "Tube OD")
        self.assertTrue(od["favorite"])
        self.assertEqual(od["dependents"]["items"], ["id", "d1 (Sketch1)"])
        self.assertEqual(od["all_dependents"]["items"], ["id", "d1 (Sketch1)", "d2 (Extrude1)"])
        self.assertEqual(od["drives"]["items"], ["Sketch1", "Extrude1"])
        self.assertEqual(rows["wall"]["drives"]["items"], ["Extrude1"])
        self.assertEqual(payload["dependency_check"]["mismatch_count"], 0)

    def test_parsed_fallback_matches_api(self):
        _, api = call(make_design(link_api=True)[0], {})
        _, parsed = call(make_design(link_api=False)[0], {})
        self.assertEqual(parsed["dependency_source"], "parsed")
        def strip(payload):
            return [(r["name"], r["dependents"], r["all_dependents"]["total"], r["drives"])
                    for r in payload["user_parameters"]["rows"]]
        self.assertEqual(strip(api), strip(parsed))

    def test_mismatch_is_reported(self):
        design, params = make_design()
        params["od"].dependentParameters = [params["id"], params["d2"]]  # API misses d1
        _, payload = call(design, {})
        self.assertEqual(payload["dependency_check"]["mismatches"],
                         [{"name": "od", "api_only": [], "parsed_only": ["d1"]}])

    def test_api_extra_is_a_mismatch(self):
        design, params = make_design()
        params["wall"].dependentParameters = [params["id"], params["d2"], params["d1"]]
        _, payload = call(design, {})
        self.assertEqual(payload["dependency_check"]["mismatches"],
                         [{"name": "wall", "api_only": ["d1"], "parsed_only": []}])

    def test_live_transitive_api_is_healthy(self):
        # Issue 18: od -> idia -> d7. The API lists d7 under od; it is not a direct dependent.
        od = FakeParam("od", "2 in", unit="in")
        idia = FakeParam("idia", "od - 0.25 in", unit="in")
        d7 = FakeParam("d7", "idia / 2", unit="in", kind="model", role="AlongDistance",
                       created_by=SimpleNamespace(objectType="adsk::fusion::ExtrudeFeature", name="Bore"))
        od.dependentParameters = [idia, d7]
        idia.dependentParameters = [d7]
        design = SimpleNamespace(userParameters=[od, idia], allParameters=[od, idia, d7], unitsManager=FakeUnits(),
                                 timeline=[])
        _, payload = call(design, {})
        rows = {r["name"]: r for r in payload["user_parameters"]["rows"]}
        self.assertEqual(rows["od"]["dependents"]["items"], ["idia"])
        self.assertEqual(rows["od"]["all_dependents"]["items"], ["idia", "d7 (Bore)"])
        self.assertEqual(payload["dependency_check"], {"compared": 2, "mismatches": [], "mismatch_count": 0})
        _, payload = call(design, {"name": "od"})
        detail = payload["parameter"]
        self.assertEqual(detail["direct_dependents"], ["idia"])
        self.assertEqual(detail["all_dependents"], ["idia", "d7 (Bore)"])
        self.assertEqual([(n["name"], n["depth"], n["via"]) for n in detail["dependents_tree"]["nodes"]],
                         [("idia", 1, "od"), ("d7", 2, "idia")])
        self.assertNotIn("dependency_check_mismatch", detail)

    def test_parameter_named_like_a_unit_is_a_dependency(self):
        h = FakeParam("h", "2 in", unit="in")
        double = FakeParam("double", "h * 2", unit="in")
        h.dependentParameters = [double]
        design = SimpleNamespace(userParameters=[h, double], allParameters=[h, double], unitsManager=FakeUnits(),
                                 timeline=[])
        _, payload = call(design, {})
        self.assertEqual(payload["user_parameters"]["rows"][0]["dependents"]["items"], ["double"])
        self.assertEqual(payload["dependency_check"]["mismatch_count"], 0)

    def test_truncated_closure_is_not_compared(self):
        design, _ = make_design()
        with patch.object(parameters, "MAX_CLOSURE_NODES", 1):
            _, payload = call(design, {})
        self.assertEqual(payload["dependency_check"]["mismatch_count"], 0)

    def test_include_model_and_filters(self):
        _, payload = call(make_design()[0], {"include_model": True, "name_contains": "D"})
        self.assertEqual([r["name"] for r in payload["user_parameters"]["rows"]], ["od", "id"])
        model = payload["model_parameters"]
        self.assertEqual([r["name"] for r in model["rows"]], ["d1", "d2"])
        self.assertEqual(model["rows"][1]["owner"], {"type": "ExtrudeFeature", "name": "Extrude1"})
        self.assertEqual(model["rows"][1]["role"], "AlongDistance")
        self.assertEqual(model["rows"][1]["component"], "Root")
        _, payload = call(make_design()[0], {"comment_contains": "WALL"})
        self.assertEqual([r["name"] for r in payload["user_parameters"]["rows"]], ["wall"])

    def test_limit_truncates(self):
        _, payload = call(make_design()[0], {"limit": 2})
        section = payload["user_parameters"]
        self.assertEqual((section["total"], section["matched"], section["returned"], section["truncated"]),
                         (3, 3, 2, True))

    def test_detail_mode(self):
        _, payload = call(make_design()[0], {"name": "od"})
        detail = payload["parameter"]
        self.assertEqual([(n["name"], n["depth"], n["via"], n["owner"]) for n in detail["dependents_tree"]["nodes"]],
                         [("id", 1, "od", None), ("d1", 1, "od", "Sketch1"), ("d2", 2, "id", "Extrude1")])
        self.assertEqual(detail["drives"], ["Sketch1", "Extrude1"])
        _, payload = call(make_design()[0], {"name": "id", "depth": 1})
        self.assertEqual(payload["parameter"]["dependencies"], ["od", "wall"])
        self.assertEqual(payload["parameter"]["dependencies_source"], "api")
        self.assertFalse(payload["parameter"]["dependents_tree"]["truncated"])

    def test_detail_unknown_name(self):
        is_error, text = call(make_design()[0], {"name": "nope"})
        self.assertTrue(is_error)
        self.assertIn("no parameter named", text)

    def test_not_a_design(self):
        with FakeFusion(None):
            result = parameters.parameters({})
        self.assertTrue(result["isError"])


class SetTests(unittest.TestCase):
    def test_refuses_model_and_unknown_without_changing_anything(self):
        design, params = make_design()
        is_error, text = call(design, {"action": "set", "dry_run": False, "updates": [
            {"name": "od", "expression": "2 in"}, {"name": "d2", "expression": "1 in"},
            {"name": "ghost", "expression": "1"}]})
        self.assertTrue(is_error)
        self.assertIn("'d2': model parameter (owned by Extrude1)", text)
        self.assertIn("'ghost': no such parameter", text)
        self.assertEqual(params["od"].expression, "1.75 in")

    def test_dry_run_is_default_and_changes_nothing(self):
        design, params = make_design()
        is_error, payload = call(design, {"action": "set", "updates": [
            {"name": "od", "expression": "2 in"}, {"name": "wall", "expression": "id / 10 + tpyo"}]})
        self.assertFalse(is_error)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(params["od"].expression, "1.75 in")
        od, wall = payload["updates"]
        self.assertEqual((od["old_expression"], od["new_expression"]), ("1.75 in", "2 in"))
        self.assertEqual(od["dependents_affected"]["items"], ["id", "d1 (Sketch1)", "d2 (Extrude1)"])
        self.assertEqual(od["drives"], ["Sketch1", "Extrude1"])
        self.assertNotIn("dependency_check_mismatch", od)
        self.assertTrue(od["expression_valid"])
        self.assertEqual(wall["would_create_cycle"], ["id"])
        self.assertEqual(wall["unknown_identifiers"], ["tpyo"])
        self.assertEqual(wall["references"], ["id"])

    def test_dry_run_flags_unchanged(self):
        _, payload = call(make_design()[0], {"action": "set", "updates": [{"name": "od", "expression": "1.75in"}]})
        self.assertTrue(payload["updates"][0]["unchanged"])

    def test_apply_in_order_with_health(self):
        design, params = make_design()
        is_error, payload = call(design, {"action": "set", "dry_run": False, "updates": [
            {"name": "wall", "expression": "0.065 in"}, {"name": "od", "expression": "1.5 in"}]})
        self.assertFalse(is_error)
        self.assertEqual(payload["applied"], ["wall", "od"])
        self.assertEqual(params["od"].expression, "1.5 in")
        self.assertEqual(payload["timeline_health"], {"healthy": 1, "warning": 0, "error": 1})
        self.assertEqual(payload["timeline_errors"]["items"], ["Extrude1"])
        self.assertIn("compute", payload["note"])

    def test_failure_stops_and_reports_what_applied(self):
        design, params = make_design()
        params["wall"]._reject = "Invalid expression"
        is_error, payload = call(design, {"action": "set", "dry_run": False, "updates": [
            {"name": "od", "expression": "1.5 in"}, {"name": "wall", "expression": "bad"},
            {"name": "id", "expression": "od"}]})
        self.assertTrue(is_error)
        self.assertEqual(payload["applied"], ["od"])
        self.assertEqual([r["status"] for r in payload["results"]], ["applied", "failed", "not_attempted"])
        self.assertEqual(payload["results"][1]["error"], "Invalid expression")
        self.assertEqual(params["id"].expression, "od - 2 * wall")

    def test_silent_rollback_counts_as_failure(self):
        design, params = make_design()
        params["od"]._rollback = True
        is_error, payload = call(design, {"action": "set", "dry_run": False,
                                          "updates": [{"name": "od", "expression": "1.5 in"}]})
        self.assertTrue(is_error)
        self.assertEqual(payload["results"][0]["status"], "failed")
        self.assertIn("old expression", payload["results"][0]["error"])

    def test_normalized_readback_counts_as_applied(self):
        design, params = make_design()

        class Normalizing(FakeParam):
            @FakeParam.expression.setter
            def expression(self, value):
                self._expression = value.replace("in", " in").replace("  ", " ")
        norm = Normalizing("od", "1.75 in", value=4.445, unit="in")
        design.userParameters[0] = norm
        design.allParameters[0] = norm
        is_error, payload = call(design, {"action": "set", "dry_run": False,
                                          "updates": [{"name": "od", "expression": "1.5in"}]})
        self.assertFalse(is_error)
        self.assertEqual(payload["results"][0]["expression_after"], "1.5 in")


if __name__ == "__main__":
    unittest.main()
