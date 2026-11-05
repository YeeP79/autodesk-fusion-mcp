"""timeline_insert: validation, matching, and the rolled-back window on fakes."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401

from fusion_bridge import timeline_insert as ti
from fusion_bridge import tool_surface

HEALTHY, WARNING, ERROR = 0, 1, 2


class FakeEntity:
    objectType = "adsk::fusion::ExtrudeFeature"

    def __init__(self, participants=None):
        if participants is not None:
            self.participantBodies = participants


class FakeItem:
    isGroup = False

    def __init__(self, name, timeline, health=HEALTHY, participants=None):
        self.name = name
        self.timeline = timeline
        self.healthState = health
        self.errorOrWarningMessage = "broke" if health == ERROR else ""
        self.entity = FakeEntity(participants)
        self.roll_result = True

    @property
    def index(self):
        return self.timeline.items.index(self)

    @property
    def isRolledBack(self):
        return self.index >= self.timeline.markerPosition

    def rollTo(self, roll_before):
        assert roll_before is True
        self.timeline.rolls.append(self.name)
        if self.roll_result:
            self.timeline.markerPosition = self.index
        return self.roll_result


class FakeGroup:
    isGroup = True

    def __init__(self, name, children, collapsed, index):
        self.name = name
        self.children = children
        self.isCollapsed = collapsed
        self.index = index if collapsed else -1

    @property
    def count(self):
        return len(self.children)

    def item(self, i):
        return self.children[i]

    def rollTo(self, roll_before):
        return True


class FakeChild:
    isGroup = False
    healthState = HEALTHY
    errorOrWarningMessage = ""
    entity = None

    def __init__(self, name, index=-1):
        self.name = name
        self.index = index


class FakeTimeline:
    """Marker semantics of adsk.fusion.Timeline: 0 is the start, count is the end."""

    def __init__(self, *names):
        self.items = []
        self.rolls = []
        self.move_error = None
        self.move_moves = True
        self.move_calls = 0
        self.after_restore = None
        for name in names:
            self.items.append(FakeItem(name, self))
        self.markerPosition = len(self.items)

    @property
    def count(self):
        return len(self.items)

    def item(self, i):
        return self.items[i]

    def named(self, name):
        return next(item for item in self.items if item.name == name)

    def add(self, name):
        """A new feature lands at the marker, which moves past it."""
        self.items.insert(self.markerPosition, FakeItem(name, self))
        self.markerPosition += 1

    def moveToEnd(self):
        self.move_calls += 1
        if self.move_moves:
            self.markerPosition = len(self.items)
            if self.after_restore:
                self.after_restore()
        if self.move_error:
            raise RuntimeError(self.move_error)
        return True


class FakeDesign:
    designType = 1

    def __init__(self, timeline):
        self.timeline = timeline


class FakeDocument:
    def __init__(self):
        self.isActive = True
        self.activations = 0

    def activate(self):
        self.activations += 1
        self.isActive = True
        return True


class FakeApp:
    def __init__(self):
        self.activeDocument = FakeDocument()


def python_ok(**extra):
    return {"content": [{"type": "text", "text": json.dumps({"success": True, "stdout": "", **extra})}],
            "isError": False}


def python_failed(message):
    return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": message})}],
            "isError": True}


class Harness(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.timeline = FakeTimeline("Sketch1", "Extrude1", "Mirror1", "Fillet1")
        self.design = FakeDesign(self.timeline)
        for target, value in (("get_app", lambda: self.app), ("_active_design", lambda app: self.design),
                              ("log", lambda *a, **k: None)):
            patcher = mock.patch.object(ti, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, side_effect=None, **arguments):
        arguments.setdefault("before", "Mirror1")
        arguments.setdefault("code", "x = 1")
        arguments.setdefault("dry_run", False)
        with mock.patch.object(ti.python_exec, "run_python", side_effect=side_effect) as self.runner:
            result = ti.timeline_insert(arguments)
        text = result["content"][0]["text"]
        try:
            return json.loads(text), result["isError"]
        except ValueError:
            return text, result["isError"]

    def adds(self, *names):
        def run(_arguments):
            for name in names:
                self.timeline.add(name)
            return python_ok()
        return run


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict(self):
        tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "timeline_insert")
        schema = tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["before", "code"])
        self.assertEqual(set(schema["properties"]), ti._ARGS)


class ValidationTests(unittest.TestCase):
    def test_defaults_to_dry_run(self):
        self.assertEqual(ti.validate_arguments({"before": " Mirror1 ", "code": "x = 1"}),
                         ("Mirror1", "x = 1", None, True))

    def test_rejects_bad_arguments(self):
        for arguments in ({}, {"before": "", "code": "x"}, {"before": 3, "code": "x"}, {"before": "a"},
                          {"before": "a", "code": "  "}, {"before": "a", "code": "x", "dry_run": "no"},
                          {"before": "a", "code": "x", "session_id": ""},
                          {"before": "a", "code": "x", "session_id": 7},
                          {"before": "a", "code": "x", "index": 3}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ti.validate_arguments(arguments)

    def test_syntax_error_is_caught_before_fusion(self):
        with self.assertRaisesRegex(ValueError, "does not compile"):
            ti.validate_arguments({"before": "a", "code": "def ("})

    def test_null_byte_in_code_is_a_validation_error(self):
        with self.assertRaisesRegex(ValueError, "does not compile"):
            ti.validate_arguments({"before": "a", "code": "x = 1\0"})

    def test_bad_arguments_never_reach_fusion(self):
        with mock.patch.object(ti, "get_app", side_effect=AssertionError("touched Fusion")):
            result = ti.timeline_insert({"before": "a", "code": "def ("})
        self.assertTrue(result["isError"])


def row(name, health="healthy", kind="feature", rollable=True, index=0, participants=None, **extra):
    return {"index": index, "name": name, "kind": kind, "health": health, "rollable": rollable,
            "message": "", "participant_bodies": participants, **extra}


def rows(*names):
    return [row(name, index=i) for i, name in enumerate(names)]


class ResolveTests(unittest.TestCase):
    def test_exact_match_only(self):
        self.assertEqual(ti.resolve_target(rows("Sketch1", "Mirror1"), "Mirror1"), (1, None))
        position, problem = ti.resolve_target(rows("Mirror1"), "mirror1")
        self.assertIsNone(position)
        self.assertIn("Mirror1", problem)

    def test_no_match_on_empty_timeline(self):
        position, problem = ti.resolve_target([], "Mirror1")
        self.assertIsNone(position)
        self.assertIn("No timeline item", problem)

    def test_duplicate_name_is_refused_and_listed(self):
        items = [row("Extrude1", index=2, component="A", entity_type="ExtrudeFeature"),
                 row("Extrude1", index=5, component="B", entity_type="ExtrudeFeature")]
        position, problem = ti.resolve_target(items, "Extrude1")
        self.assertIsNone(position)
        self.assertIn("component A", problem)
        self.assertIn("index 5", problem)

    def test_child_of_collapsed_group_is_refused(self):
        items = [row("Inner", rollable=False, index=None, group="G")]
        position, problem = ti.resolve_target(items, "Inner")
        self.assertIsNone(position)
        self.assertIn("collapsed group 'G'", problem)

    def test_expanded_group_is_refused(self):
        _, problem = ti.resolve_target([row("G", kind="group", health="group", rollable=False)], "G")
        self.assertIn("expanded group", problem)

    def test_collapsed_group_can_be_the_target(self):
        self.assertEqual(ti.resolve_target([row("G", kind="group", health="group")], "G"), (0, None))


class PreflightTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(ti.preflight(rows("a", "b"), 2, 2, "b"), (1, []))

    def test_marker_not_at_end(self):
        _, refusals = ti.preflight(rows("a", "b"), 1, 2, "b")
        self.assertIn("marker is at 1 of 2", refusals[0])

    def test_existing_errors(self):
        items = rows("a", "b") + [row("c", health="error")]
        _, refusals = ti.preflight(items, 3, 3, "b")
        self.assertIn("1 item(s) in error ('c')", refusals[0])

    def test_warnings_do_not_refuse(self):
        self.assertEqual(ti.preflight([row("a", health="warning")], 1, 1, "a")[1], [])

    def test_reports_every_problem(self):
        _, refusals = ti.preflight([row("c", health="error")], 0, 1, "nope")
        self.assertEqual(len(refusals), 3)


class RolledOkTests(unittest.TestCase):
    def test_cases(self):
        self.assertTrue(ti.rolled_ok(True, 3, 3, True))
        self.assertTrue(ti.rolled_ok(True, 3, 4, True))
        self.assertFalse(ti.rolled_ok(False, 3, 3, True))
        self.assertFalse(ti.rolled_ok(True, 9, 3, False))
        self.assertFalse(ti.rolled_ok(None, 3, 3, None))


class AlignTests(unittest.TestCase):
    def names(self, items):
        return [item["name"] for item in items]

    def test_insertion_at_marker(self):
        pairs, added, removed = ti.align(rows("a", "m", "z"), rows("a", "n1", "n2", "m", "z"), 1)
        self.assertEqual(self.names(added), ["n1", "n2"])
        self.assertEqual(removed, [])
        self.assertEqual(sorted(pairs), [(0, 0), (1, 3), (2, 4)])

    def test_new_item_named_like_a_later_one(self):
        pairs, added, _ = ti.align(rows("a", "Extrude1"), rows("a", "Extrude1", "Extrude1"), 1)
        self.assertEqual(added[0]["index"], 1)
        self.assertIn((1, 2), pairs)

    def test_insert_before_first_item(self):
        _, added, removed = ti.align(rows("a"), rows("n", "a"), 0)
        self.assertEqual(self.names(added), ["n"])
        self.assertEqual(removed, [])

    def test_nothing_changed(self):
        pairs, added, removed = ti.align(rows("a", "b"), rows("a", "b"), 1)
        self.assertEqual((sorted(pairs), added, removed), ([(0, 0), (1, 1)], [], []))

    def test_deleted_item(self):
        _, added, removed = ti.align(rows("a", "b", "c"), rows("a", "c"), 2)
        self.assertEqual(added, [])
        self.assertEqual(self.names(removed), ["b"])

    def test_rename_reads_as_removed_and_added(self):
        _, added, removed = ti.align(rows("a", "b", "c"), rows("a", "B2", "c"), 1)
        self.assertEqual((self.names(added), self.names(removed)), (["B2"], ["b"]))

    def test_added_at_end_after_moving_marker(self):
        pairs, added, _ = ti.align(rows("a", "m"), rows("a", "m", "n"), 1)
        self.assertEqual(self.names(added), ["n"])
        self.assertIn((1, 1), pairs)

    def test_empty_sides(self):
        self.assertEqual(ti.align([], [], 0), ([], [], []))
        self.assertEqual(self.names(ti.align([], rows("n"), 0)[1]), ["n"])
        self.assertEqual(self.names(ti.align(rows("a"), [], 0)[2]), ["a"])


class CompareRowsTests(unittest.TestCase):
    def test_splits_errors_around_the_target(self):
        before = rows("a", "m", "z")
        after = [row("a", health="error"), row("m", index=1), row("z", health="error", index=2)]
        changes = ti.compare_rows(before, after, 1)
        self.assertEqual([r["name"] for r in changes["later_items_in_error"]], ["z"])
        self.assertEqual([r["name"] for r in changes["earlier_items_in_error"]], ["a"])

    def test_the_target_itself_counts_as_later(self):
        changes = ti.compare_rows(rows("m"), [row("m", health="error")], 0)
        self.assertEqual([r["name"] for r in changes["later_items_in_error"]], ["m"])

    def test_new_warnings(self):
        changes = ti.compare_rows(rows("m"), [row("m", health="warning")], 0)
        self.assertEqual([r["name"] for r in changes["later_items_with_new_warnings"]], ["m"])

    def test_participant_drop(self):
        before = [row("cut", participants=3)]
        for now in (0, None, 1):
            changes = ti.compare_rows(before, [row("cut", participants=now)], 0)
            self.assertEqual(changes["participant_bodies_changed"],
                             [{"name": "cut", "before": 3, "after": now}])

    def test_participants_unreadable_before_or_empty_both_ways(self):
        self.assertEqual(ti.compare_rows([row("c")], [row("c", participants=2)], 0)["participant_bodies_changed"], [])
        self.assertEqual(ti.compare_rows([row("c", participants=0)], [row("c")], 0)["participant_bodies_changed"], [])

    def test_item_landing_after_target(self):
        changes = ti.compare_rows(rows("a", "m"), rows("a", "m", "n"), 1)
        self.assertEqual(changes["landed_after_target"], ["n"])

    def test_target_gone(self):
        self.assertFalse(ti.compare_rows(rows("a", "m"), rows("a"), 1)["target_found_after"])

    def test_listing_is_bounded(self):
        after = rows(*[f"n{i}" for i in range(ti.MAX_LISTED + 5)])
        self.assertEqual(len(ti.compare_rows([], after, 0)["items_added"]), ti.MAX_LISTED)


class ReadRowsTests(unittest.TestCase):
    def timeline(self, *top):
        timeline = mock.Mock(spec=["count", "item"])
        timeline.count = len(top)
        timeline.item = lambda i: top[i]
        return timeline

    def test_collapsed_group_children_are_listed_but_not_rollable(self):
        group = FakeGroup("G", [FakeChild("in1"), FakeChild("in2")], True, 0)
        items, objects = ti.read_rows(self.timeline(group, FakeChild("after", 1)))
        self.assertEqual([r["name"] for r in items], ["G", "in1", "in2", "after"])
        self.assertEqual([r["rollable"] for r in items], [True, False, False, True])
        self.assertEqual(items[1]["group"], "G")
        self.assertIsNone(items[1]["index"])
        self.assertIs(objects[0], group)

    def test_expanded_group_children_are_not_listed_twice(self):
        first, second = FakeChild("in1", 0), FakeChild("in2", 1)
        group = FakeGroup("G", [first, second], False, -1)
        items, _ = ti.read_rows(self.timeline(group, first, second))
        self.assertEqual([r["name"] for r in items], ["G", "in1", "in2"])
        self.assertEqual([r["rollable"] for r in items], [False, True, True])

    def test_group_is_not_counted_as_a_feature(self):
        group = FakeGroup("G", [FakeChild("in1")], True, 0)
        items, _ = ti.read_rows(self.timeline(group))
        self.assertEqual(ti.health_counts(items), {"healthy": 1})

    def test_unknown_health_and_raising_properties(self):
        class Broken:
            isGroup = False
            index = 0
            name = "x"
            healthState = 9

            @property
            def entity(self):
                raise RuntimeError("stale")

        items, _ = ti.read_rows(self.timeline(Broken()))
        self.assertEqual(items[0]["health"], "unknown")
        self.assertIsNone(items[0]["entity_type"])

    def test_every_health_state_has_a_name(self):
        self.assertEqual([ti._health_name(i) for i in range(6)],
                         ["healthy", "warning", "error", "suppressed", "rolled_back", "unknown"])


class DryRunTests(Harness):
    def test_reports_and_touches_nothing(self):
        report, is_error = self.call(dry_run=True)
        self.assertFalse(is_error)
        self.assertEqual(report["item"]["name"], "Mirror1")
        self.assertEqual(report["item"]["index"], 2)
        self.assertEqual(report["marker_position"], 4)
        self.assertEqual([r["name"] for r in report["items_after"]], ["Fillet1"])
        self.assertEqual(report["items_after_count"], 1)
        self.assertEqual(self.timeline.rolls, [])
        self.assertEqual(self.timeline.move_calls, 0)
        self.runner.assert_not_called()

    def test_is_the_default(self):
        arguments = {"before": "Mirror1", "code": "x = 1"}
        with mock.patch.object(ti.python_exec, "run_python") as runner:
            result = ti.timeline_insert(arguments)
        self.assertTrue(json.loads(result["content"][0]["text"])["dry_run"])
        runner.assert_not_called()
        self.assertEqual(self.timeline.rolls, [])

    def test_shows_refusals_too(self):
        self.timeline.markerPosition = 1
        report, is_error = self.call(dry_run=True)
        self.assertTrue(is_error)
        self.assertIn("marker", report["refusals"][0])


class RefusalTests(Harness):
    def assert_refused(self, report, is_error):
        self.assertTrue(is_error)
        self.assertEqual(self.timeline.rolls, [])
        self.assertEqual(self.timeline.move_calls, 0)
        self.runner.assert_not_called()

    def test_existing_errors(self):
        self.timeline.named("Fillet1").healthState = ERROR
        self.assert_refused(*self.call())

    def test_rolled_back_marker(self):
        self.timeline.markerPosition = 3
        self.assert_refused(*self.call())
        self.assertEqual(self.timeline.markerPosition, 3)

    def test_unknown_item(self):
        report, is_error = self.call(before="Mirror2")
        self.assert_refused(report, is_error)
        self.assertIn("Mirror1", report["refusals"][0])

    def test_duplicate_name(self):
        self.timeline.items.append(FakeItem("Mirror1", self.timeline))
        self.timeline.markerPosition = self.timeline.count
        self.assert_refused(*self.call())

    def test_direct_design(self):
        self.design.designType = 0
        text, is_error = self.call()
        self.assertTrue(is_error)
        self.assertIn("direct design", text)

    def test_no_design(self):
        self.design = None
        text, is_error = self.call()
        self.assertTrue(is_error)
        self.assertIn("not a Fusion design", text)


class RunTests(Harness):
    def test_new_items_land_before_the_target(self):
        report, is_error = self.call(side_effect=self.adds("Sketch2", "Extrude2"))
        self.assertFalse(is_error)
        self.assertEqual(report["status"], "ok")
        self.assertEqual([(r["name"], r["index"]) for r in report["items_added"]],
                         [("Sketch2", 2), ("Extrude2", 3)])
        self.assertEqual(report["inserted_at"], 2)
        self.assertEqual(self.timeline.rolls, ["Mirror1"])
        self.assertEqual(self.timeline.markerPosition, self.timeline.count)
        self.assertEqual(report["health_before"], {"healthy": 4})
        self.assertEqual(report["health_after"], {"healthy": 6})
        self.assertEqual(report["later_items_in_error"], [])
        self.assertEqual(report["warnings"], [])
        self.assertTrue(report["marker"]["restored"])

    def test_code_runs_rolled_back(self):
        seen = []

        def run(_arguments):
            seen.append(self.timeline.markerPosition)
            return python_ok()

        self.call(side_effect=run)
        self.assertEqual(seen, [2])

    def test_session_id_goes_to_the_runner(self):
        self.call(side_effect=self.adds("n"), session_id="s1", code="y = 2")
        self.runner.assert_called_once_with({"code": "y = 2", "persistent": True, "session_id": "s1"})

    def test_no_session_id_is_an_independent_run(self):
        self.call(side_effect=self.adds("n"))
        self.runner.assert_called_once_with({"code": "x = 1", "persistent": False})

    def test_python_output_is_reported(self):
        report, _ = self.call(side_effect=lambda _a: python_ok(stdout="hello\n"))
        self.assertEqual(report["python"]["stdout"], "hello\n")

    def test_nothing_added_is_flagged(self):
        report, is_error = self.call(side_effect=lambda _a: python_ok())
        self.assertFalse(is_error)
        self.assertIn("added no timeline items", report["warnings"][0])

    def test_later_item_breaking_is_reported_as_an_error(self):
        def breaks():
            self.timeline.named("Fillet1").healthState = ERROR
            self.timeline.named("Fillet1").errorOrWarningMessage = "no edges"

        self.timeline.after_restore = breaks
        report, is_error = self.call(side_effect=self.adds("Extrude2"))
        self.assertTrue(is_error)
        self.assertEqual(report["status"], "items_in_error")
        self.assertEqual(report["later_items_in_error"][0]["name"], "Fillet1")
        self.assertEqual(report["later_items_in_error"][0]["message"], "no edges")
        self.assertEqual(report["health_after"], {"healthy": 4, "error": 1})

    def test_added_item_in_error(self):
        def run(_arguments):
            self.timeline.add("Bad")
            self.timeline.named("Bad").healthState = ERROR
            return python_ok()

        report, is_error = self.call(side_effect=run)
        self.assertTrue(is_error)
        self.assertEqual(report["status"], "items_in_error")

    def test_new_warning_is_not_a_failure(self):
        self.timeline.after_restore = lambda: setattr(self.timeline.named("Fillet1"), "healthState", WARNING)
        report, is_error = self.call(side_effect=self.adds("n"))
        self.assertFalse(is_error)
        self.assertEqual(report["later_items_with_new_warnings"][0]["name"], "Fillet1")

    def test_code_that_moves_the_marker_itself_is_flagged(self):
        def run(_arguments):
            self.timeline.moveToEnd()
            self.timeline.add("Late")
            return python_ok()

        report, _ = self.call(side_effect=run)
        self.assertEqual([r["name"] for r in report["items_added"]], ["Late"])
        self.assertIn("landed after", report["warnings"][0])

    def test_participant_loss_is_flagged(self):
        item = self.timeline.named("Extrude1")
        item.entity = FakeEntity(["b1", "b2"])

        def run(_arguments):
            item.entity = FakeEntity([])
            return python_ok()

        report, _ = self.call(side_effect=run)
        self.assertEqual(report["participant_bodies_changed"], [{"name": "Extrude1", "before": 2, "after": 0}])
        self.assertTrue(any("Participant bodies" in w for w in report["warnings"]))


class RestoreTests(Harness):
    def assert_at_end(self):
        self.assertEqual(self.timeline.markerPosition, self.timeline.count)

    def test_after_the_code_fails(self):
        def run(_arguments):
            self.timeline.add("Partial")
            return python_failed("boom")

        report, is_error = self.call(side_effect=run)
        self.assertTrue(is_error)
        self.assert_at_end()
        self.assertEqual(report["status"], "code_failed")
        self.assertEqual(report["python"]["error"], "boom")
        self.assertEqual([r["name"] for r in report["items_added"]], ["Partial"])
        self.assertIn("still in the timeline", report["note"])

    def test_after_the_runner_raises(self):
        report, is_error = self.call(side_effect=RuntimeError("runner broke"))
        self.assertTrue(is_error)
        self.assert_at_end()
        self.assertEqual(report["status"], "code_failed")
        self.assertIn("runner broke", report["exception"])

    def test_after_system_exit(self):
        report, is_error = self.call(side_effect=SystemExit(3))
        self.assertTrue(is_error)
        self.assert_at_end()
        self.assertIn("SystemExit", report["exception"])

    def test_after_a_failed_roll(self):
        self.timeline.named("Mirror1").roll_result = False
        report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertEqual(report["status"], "roll_failed")
        self.runner.assert_not_called()
        self.assertEqual(self.timeline.move_calls, 1)

    def test_when_roll_to_raises(self):
        with mock.patch.object(FakeItem, "rollTo", side_effect=RuntimeError("no")):
            report, _ = self.call()
        self.assertEqual(report["status"], "roll_failed")
        self.assertEqual(self.timeline.move_calls, 1)
        self.runner.assert_not_called()

    def test_move_to_end_raising_after_moving_counts_as_restored(self):
        self.timeline.move_error = "Fillet1 / Compute Failed // NO_EDGE - x" * 200
        report, is_error = self.call(side_effect=self.adds("n"))
        self.assertTrue(report["marker"]["restored"])
        self.assertEqual(report["status"], "ok")
        self.assertFalse(is_error)
        self.assertLess(len(report["marker"]["errors"][0]), ti.MAX_ERROR_CHARS + 100)

    def test_stuck_marker_is_loud_and_retried(self):
        self.timeline.move_moves = False
        report, is_error = self.call(side_effect=self.adds("n"))
        self.assertTrue(is_error)
        self.assertEqual(report["status"], "marker_not_restored")
        self.assertFalse(report["marker"]["restored"])
        self.assertIn("NOT at the end", report["marker"]["message"])
        self.assertEqual(self.timeline.move_calls, ti.RESTORE_ATTEMPTS)

    def test_move_to_end_returning_false(self):
        with mock.patch.object(FakeTimeline, "moveToEnd", return_value=False):
            report, _ = self.call(side_effect=self.adds("n"))
        self.assertEqual(report["status"], "marker_not_restored")
        self.assertIn("moveToEnd returned False", report["marker"]["errors"])

    def test_code_that_switched_documents(self):
        def run(_arguments):
            self.app.activeDocument.isActive = False
            return python_ok()

        report, _ = self.call(side_effect=run)
        self.assertEqual(self.app.activeDocument.activations, 1)
        self.assertTrue(report["marker"]["reactivated_document"])

    def test_activate_failure_does_not_stop_the_restore(self):
        def run(_arguments):
            self.app.activeDocument.isActive = False
            self.app.activeDocument.activate = mock.Mock(side_effect=RuntimeError("gone"))
            return python_ok()

        report, _ = self.call(side_effect=run)
        self.assertTrue(report["marker"]["restored"])
        self.assertIn("activate", report["marker"]["errors"][0])

    def test_failed_read_after_restore_is_reported(self):
        real = ti.read_rows
        calls = []

        def read(timeline):
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("document closed")
            return real(timeline)

        with mock.patch.object(ti, "read_rows", read):
            report, is_error = self.call(side_effect=self.adds("n"))
        self.assertTrue(is_error)
        self.assertEqual(report["status"], "unverified")
        self.assertIn("document closed", report["read_after_error"])


class RealRunnerTests(Harness):
    """The real execute_python runner, with only its globals stubbed."""

    def run_code(self, code):
        scope = {"timeline": self.timeline}
        with mock.patch.object(ti.python_exec, "_build_runtime_globals", return_value=scope):
            result = ti.timeline_insert({"before": "Mirror1", "code": code, "dry_run": False})
        return json.loads(result["content"][0]["text"]), result["isError"]

    def test_raise_is_reported_and_the_marker_comes_back(self):
        report, is_error = self.run_code("timeline.add('Half')\nraise ValueError('bad input')")
        self.assertTrue(is_error)
        self.assertEqual(report["status"], "code_failed")
        self.assertIn("bad input", report["python"]["error"])
        self.assertEqual(self.timeline.markerPosition, self.timeline.count)
        self.assertEqual(report["items_added"][0]["name"], "Half")

    def test_success(self):
        report, is_error = self.run_code("timeline.add('New')\nprint('done')")
        self.assertFalse(is_error)
        self.assertEqual(report["python"]["stdout"], "done\n")
        self.assertEqual(report["items_added"][0]["index"], 2)


if __name__ == "__main__":
    unittest.main()
