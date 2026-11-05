"""timeline_health tool: schema, validation, report logic, and the rollback
summary prepended to execute_python / call_autodesk_api failures."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import timeline_health as th
from fusion_bridge.common import timeline
from fusion_bridge import tool_surface

# Excerpt of a real dump raised by an API edit on a design with broken features.
DUMP = (
    "3 : SurfaceTrim18 / 1 Reference Failures The model is using cached geometry to solve. "
    "Please reselect reference geometry for failed features in the timeline. "
    "// ENTITY_REFERENCE_LOST_USE_CACHE - Body 1 missing\n"
    "SurfaceExtend4 / 1 Reference Failures The edge reference is lost and this feature is using cached "
    "geometry. Edit this feature and select new edge references. // EDGE_REFERENCE_LOST - Edge 1 missing "
    "/ Compute Failed // NO_EXTEND_EDGE - No Extend Edge Found!\n"
    "Extrude11 / Compute Failed // NO_TARGET_BODY - No target body!\n"
    "Split1 / Compute Failed // ASM_LOP_SHT_EXT_BAD_TRY -   The selected edges could not be extended.\n"
    "    Try changing the edge selection or extend distance.\n"
    "Sketch3 / Project4 // 1 Reference Failures The project source is lost, Cache is used! "
    "/// PROJECT_SOURCE_LOST - Face 1 missing\n"
    "Extrude11 / Compute Failed // NO_TARGET_BODY - No target body!\n"
    "Revolute 3 / 2 Reference Failures The model is using cached geometry to solve. "
    "// ENTITY_REFERENCE_LOST_USE_CACHE - Edge 1 missing"
)


class DetectorTests(unittest.TestCase):
    def test_detects_real_dump(self):
        self.assertTrue(th.is_compute_failure_dump(DUMP))

    def test_single_compute_failed_line(self):
        self.assertTrue(th.is_compute_failure_dump("3 : Extrude11 / Compute Failed // NO_TARGET_BODY - No target body!"))

    def test_ordinary_errors_are_not_dumps(self):
        for text in (
            "",
            None,
            42,
            "'Design' object has no attribute 'isComputeDeferred'",
            "3 : InternalValidationError : geometryOrOriginOne_",
            "division / zero",
            "Compute Failed",  # marker but no feature lines
            "a / b // NOT_A_FAILURE - detail",  # feature-line shape but no reason marker
        ):
            with self.subTest(text=text):
                self.assertFalse(th.is_compute_failure_dump(text))

    def test_one_reference_failure_line_alone_is_not_enough(self):
        self.assertFalse(th.is_compute_failure_dump(
            "Sketch1 / 1 Reference Failures Cache is used // SKETCH_REFPLANE_LOST - Profile 1 missing"))

    def test_parse_names_in_order_deduplicated(self):
        self.assertEqual(
            th.parse_failed_features(DUMP),
            ["SurfaceTrim18", "SurfaceExtend4", "Extrude11", "Split1", "Sketch3", "Revolute 3"],
        )

    def test_parse_ignores_continuation_lines(self):
        self.assertNotIn("Try changing the edge selection or extend distance.", th.parse_failed_features(DUMP))


class SummaryTests(unittest.TestCase):
    def test_format_with_index(self):
        self.assertEqual(
            th.format_rollback_summary(12, "Extrude11", 47),
            "Edit refused: Fusion rolled it back because 12 timeline items are broken "
            "(earliest: Extrude11 at step 47). Fix or suppress them first (timeline_health).",
        )

    def test_format_singular_without_index(self):
        text = th.format_rollback_summary(1, "Split1", None)
        self.assertIn("1 timeline item is broken (earliest: Split1)", text)

    def test_summary_prefers_live_timeline(self):
        text = th.rollback_summary(DUMP, live_reader=lambda: (3, "Sketch2", 4))
        self.assertIn("3 timeline items are broken (earliest: Sketch2 at step 4)", text)

    def test_summary_falls_back_to_dump(self):
        text = th.rollback_summary(DUMP, live_reader=lambda: None)
        self.assertIn("6 timeline items are broken (earliest: SurfaceTrim18)", text)

    def test_no_summary_for_other_errors(self):
        reader = mock.Mock()
        self.assertIsNone(th.rollback_summary("NameError: x", live_reader=reader))
        reader.assert_not_called()

    def test_truncate(self):
        self.assertEqual(th.truncate("abc", 5), "abc")
        cut = th.truncate("x" * 30, 10)
        self.assertTrue(cut.startswith("x" * 10))
        self.assertIn("truncated 20 more characters", cut)


class EditToolHookTests(unittest.TestCase):
    """The two edit tools prepend the summary and truncate the dump."""

    LONG_DUMP = DUMP + "\n" + "\n".join(f"Fillet{i} / Compute Failed // X_Y - z" for i in range(200))

    def test_execute_python(self):
        from fusion_bridge import python_exec
        code = f"raise RuntimeError({self.LONG_DUMP!r})"
        with mock.patch.object(python_exec, "_build_runtime_globals", return_value={"__builtins__": __builtins__}), \
                mock.patch.object(th, "_live_broken_summary", return_value=(7, "Extrude11", 12)):
            result = python_exec.run_python({"code": code, "persistent": False})
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["error"].startswith("Edit refused: Fusion rolled it back because 7 timeline items"))
        self.assertIn("SurfaceTrim18", payload["error"])
        self.assertIn("truncated", payload["error"])
        self.assertLess(len(payload["error"]), len(self.LONG_DUMP))

    def test_execute_python_plain_error_unchanged(self):
        from fusion_bridge import python_exec
        with mock.patch.object(python_exec, "_build_runtime_globals", return_value={"__builtins__": __builtins__}):
            result = python_exec.run_python({"code": "raise ValueError('nope')", "persistent": False})
        self.assertEqual(json.loads(result["content"][0]["text"])["error"], "nope")

    def test_call_autodesk_api(self):
        from fusion_bridge import operations

        def boom(*a, **k):
            raise RuntimeError(self.LONG_DUMP)

        with mock.patch.object(operations, "_execute_api_call", boom), \
                mock.patch.object(th, "_live_broken_summary", return_value=None):
            result = operations.handle_generic_api_call(
                {"params": {"arguments": {"api_path": "rootComponent.timeline.item"}}})
        text = result["content"][0]["text"]
        self.assertTrue(result["isError"])
        self.assertTrue(text.startswith("Edit refused: Fusion rolled it back because 206 timeline items"))
        self.assertIn("Error: RuntimeError: 3 : SurfaceTrim18", text)
        self.assertIn("truncated", text)


class SchemaTests(unittest.TestCase):
    def _schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "timeline_health")["inputSchema"]

    def test_schema_shape(self):
        schema = self._schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"suppress_broken", "confirm"})
        self.assertEqual(schema["properties"]["suppress_broken"]["type"], "boolean")
        self.assertEqual(schema["properties"]["confirm"]["type"], "boolean")
        self.assertNotIn("required", schema)


class ValidationTests(unittest.TestCase):
    def test_suppress_without_confirm_refused_before_fusion(self):
        with mock.patch.object(th, "_active_design") as design:
            for args in ({"suppress_broken": True}, {"suppress_broken": True, "confirm": False}):
                with self.subTest(args=args):
                    result = th.timeline_health(args)
                    self.assertTrue(result["isError"])
                    self.assertIn("confirm: true", result["content"][0]["text"])
            design.assert_not_called()

    def test_non_boolean_arguments_refused(self):
        for args in ({"suppress_broken": "yes"}, {"suppress_broken": 1}, {"confirm": "true"}):
            with self.subTest(args=args):
                result = th.timeline_health(args)
                self.assertTrue(result["isError"])

    def test_no_design(self):
        with mock.patch.object(th, "_active_design", return_value=None):
            result = th.timeline_health({})
        self.assertTrue(result["isError"])
        self.assertIn("not a Fusion design", result["content"][0]["text"])


# -- Fakes for the report and suppression logic ------------------------------

S = timeline.DEFAULT_STATES


class FakeItem:
    def __init__(self, index, name, health, message="", suppressed=False, entity_type="adsk::fusion::ExtrudeFeature",
                 is_group=False, heals_after=None):
        self.index, self.name, self.healthState = index, name, health
        self.errorOrWarningMessage, self.isSuppressed, self.isGroup = message, suppressed, is_group
        self.entity = type("E", (), {"objectType": entity_type})()
        self.heals_after = heals_after


class FakeTimeline:
    def __init__(self, items, marker=None):
        self.items = items
        self.markerPosition = len(items) if marker is None else marker

    @property
    def count(self):
        return len(self.items)

    def item(self, i):
        return self.items[i]


class FakeComponent:
    def __init__(self, name):
        self.name = name


class FakeDesign:
    def __init__(self, items, marker=None, active=None):
        self.timeline = FakeTimeline(items, marker)
        self.rootComponent = FakeComponent("Root")
        self.activeComponent = active or self.rootComponent


class ReportTests(unittest.TestCase):
    def _run(self, design, args=None):
        with mock.patch.object(th, "_active_design", return_value=design), \
                mock.patch.object(th, "health_states", return_value=S):
            result = th.timeline_health(args or {})
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def test_healthy_timeline(self):
        report = self._run(FakeDesign([FakeItem(0, "Sketch1", S["healthy"]), FakeItem(1, "Extrude1", S["healthy"])]))
        self.assertEqual(report["health_counts"], {"healthy": 2, "warning": 0, "error": 0, "unknown": 0})
        self.assertEqual(report["non_healthy"], [])
        self.assertEqual(report["warnings"], [])
        self.assertTrue(report["active_is_root"])
        self.assertFalse(report["rolled_back"])

    def test_empty_timeline(self):
        report = self._run(FakeDesign([]))
        self.assertEqual(report["count"], 0)
        self.assertEqual(report["non_healthy"], [])

    def test_lists_non_healthy_and_skips_groups(self):
        items = [
            FakeItem(0, "Sketch1", S["healthy"]),
            FakeItem(1, "Group1", S["error"], is_group=True),
            FakeItem(2, "Extrude2", S["warning"], "cached geometry"),
            FakeItem(3, "Split1", S["error"], "Compute Failed", suppressed=True),
            FakeItem(4, "Odd", 99),
        ]
        report = self._run(FakeDesign(items))
        self.assertEqual(report["health_counts"], {"healthy": 1, "warning": 1, "error": 1, "unknown": 1})
        self.assertEqual([i["name"] for i in report["non_healthy"]], ["Extrude2", "Split1", "Odd"])
        split = report["non_healthy"][1]
        self.assertEqual(split, {"index": 3, "name": "Split1", "entity_type": "adsk::fusion::ExtrudeFeature",
                                 "health": "error", "message": "Compute Failed", "suppressed": True})

    def test_warns_rolled_back_and_non_root(self):
        design = FakeDesign([FakeItem(0, "A", S["healthy"]), FakeItem(1, "B", S["healthy"])], marker=1,
                            active=FakeComponent("Knuckle:1"))
        report = self._run(design)
        self.assertTrue(report["rolled_back"])
        self.assertFalse(report["active_is_root"])
        self.assertEqual(report["active_component"], "Knuckle:1")
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("rolled back to 1 of 2", report["warnings"][0])

    def test_suppress_broken_earliest_first(self):
        items = [
            FakeItem(0, "Sketch1", S["healthy"]),
            FakeItem(1, "Extrude2", S["error"]),
            FakeItem(2, "Split1", S["error"], suppressed=True),
            FakeItem(3, "Fillet1", S["warning"]),
            FakeItem(4, "Mirror1", S["error"]),
        ]
        report = self._run(FakeDesign(items), {"suppress_broken": True, "confirm": True})
        action = report["suppress_broken"]
        self.assertEqual(action["suppressed"], [{"index": 1, "name": "Extrude2"}, {"index": 4, "name": "Mirror1"}])
        self.assertEqual(action["skipped"], [{"index": 2, "name": "Split1", "reason": "already suppressed"}])
        self.assertEqual(action["failed"], [])
        self.assertTrue(items[1].isSuppressed and items[4].isSuppressed)
        self.assertFalse(items[3].isSuppressed)

    def test_suppress_skips_items_healed_by_earlier_suppression(self):
        rows = [
            ({"index": 0, "name": "A", "health": "error", "suppressed": False}, FakeItem(0, "A", S["error"])),
            ({"index": 1, "name": "B", "health": "error", "suppressed": False}, FakeItem(1, "B", S["healthy"])),
        ]
        action = th._suppress_broken(rows, S)
        self.assertEqual(action["suppressed"], [{"index": 0, "name": "A"}])
        self.assertEqual(action["skipped"], [{"index": 1, "name": "B", "reason": "no longer in error"}])

    def test_suppress_records_failures(self):
        class Stuck(FakeItem):
            def __setattr__(self, key, value):
                if key == "isSuppressed" and value is True:
                    raise RuntimeError("cannot suppress")
                super().__setattr__(key, value)

        rows = [({"index": 0, "name": "A", "health": "error", "suppressed": False}, Stuck(0, "A", S["error"]))]
        action = th._suppress_broken(rows, S)
        self.assertEqual(action["failed"], [{"index": 0, "name": "A", "error": "cannot suppress"}])

    def test_live_broken_summary_ignores_suppressed(self):
        items = [
            FakeItem(0, "S", S["healthy"]),
            FakeItem(1, "Old", S["error"], suppressed=True),
            FakeItem(2, "Ext", S["warning"]),
            FakeItem(3, "Cut", S["error"]),
        ]
        with mock.patch.object(th, "_active_design", return_value=FakeDesign(items)), \
                mock.patch.object(th, "health_states", return_value=S):
            self.assertEqual(th._live_broken_summary(), (2, "Ext", 2))

    def test_live_broken_summary_swallows_errors(self):
        with mock.patch.object(th, "_active_design", side_effect=RuntimeError("no app")):
            self.assertIsNone(th._live_broken_summary())

    def test_state_name(self):
        self.assertEqual(th.state_name(S["error"]), "error")
        self.assertEqual(th.state_name(None), "unknown")


if __name__ == "__main__":
    unittest.main()
