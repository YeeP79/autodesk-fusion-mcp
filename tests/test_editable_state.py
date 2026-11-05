"""set_editable_state: planning, settling and the batch, run against fakes."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401

from fusion_bridge import editable_state as es
from fusion_bridge import tool_surface


class FakeClock:
    """Stands in for the ``time`` module; sleeping just moves the clock."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeFile:
    def __init__(self, name, read_only=True, version=3, latest=None, ext="f3d"):
        self.id = f"urn:{name}"
        self.name = name
        self.isReadOnly = read_only
        self.versionNumber = version
        self.latestVersionNumber = latest or version
        self.fileExtension = ext


class FakeDoc:
    def __init__(self, fusion, data_file, name=None, modified=False, visible=True, links=(), held=False):
        self.fusion = fusion
        self.dataFile = data_file
        self.name = name or (data_file.name if data_file else "Untitled")
        self.isModified = modified
        self.isVisible = visible
        self.documentReferences = [type("Ref", (), {"dataFile": f})() for f in links]
        self.held = held
        self.closed_with = []

    @property
    def isActive(self):
        return self.fusion.activeDocument is self

    def activate(self):
        self.fusion.calls.append(("activate", self.name))
        if self not in self.fusion.docs or self.fusion.refuse_activate:
            return False
        self.fusion.activeDocument = self
        return True

    def close(self, save):
        self.fusion.calls.append(("close", self.name, save))
        self.closed_with.append(save)
        if self.held:
            return True
        self.fusion.docs.remove(self)
        if self.fusion.activeDocument is self:
            self.fusion.activeDocument = self.fusion.docs[-1] if self.fusion.docs else None
        return True


class FakeDocuments:
    def __init__(self, fusion):
        self.fusion = fusion

    @property
    def count(self):
        return len(self.fusion.docs)

    def item(self, i):
        return self.fusion.docs[i]

    def open(self, data_file, visible):
        fusion = self.fusion
        fusion.calls.append(("open", data_file.name))
        fusion.clock.now += fusion.open_seconds
        doc = FakeDoc(fusion, data_file, **fusion.open_kwargs.get(data_file.name, {}))
        fusion.docs.append(doc)
        fusion.activeDocument = doc
        return doc


class FakeCommand:
    def __init__(self, fusion, command_id):
        self.fusion = fusion
        self.id = command_id

    def execute(self):
        self.fusion.calls.append(("execute", self.id, self.fusion.activeDocument.name))
        self.fusion.queued.append([self.fusion.command_delay, self.id])


class FakeFusion:
    """An ``app`` whose commands, like Fusion's, only run during doEvents."""

    def __init__(self, clock, files=(), editable=None, max_editable=None):
        self.clock = clock
        self.files = {f.id: f for f in files}
        self.docs = []
        self.activeDocument = None
        self.activeProduct = object()
        self.calls = []
        self.queued = []
        self.command_delay = 1
        self.open_seconds = 0.0
        self.open_kwargs = {}
        self.refuse_activate = False
        self.pump_seconds = 0.0
        self.documents = FakeDocuments(self)
        self.editable = editable
        self.max_editable = max_editable
        self.projects = []

        fusion = self

        class Data:
            @property
            def dataProjects(self):
                return fusion.projects

            @property
            def personalUseLimits(self):
                if fusion.max_editable is None:
                    return None
                return type("Limits", (), {"editableFileCount": fusion.editable,
                                           "maxEditableFileCount": fusion.max_editable})()

            def findFileById(self, file_id):
                return fusion.files.get(file_id)

        class Definitions:
            def itemById(self, command_id):
                return None if command_id in fusion.missing_commands else FakeCommand(fusion, command_id)

        self.data = Data()
        self.missing_commands = set()
        self.userInterface = type("UI", (), {"activeCommand": "SelectCommand",
                                             "commandDefinitions": Definitions()})()

    def add_doc(self, data_file, active=False, **kwargs):
        doc = FakeDoc(self, data_file, **kwargs)
        self.docs.append(doc)
        if active:
            self.activeDocument = doc
        return doc

    def do_events(self):
        self.clock.now += self.pump_seconds
        for item in list(self.queued):
            item[0] -= 1
            if item[0] > 0:
                continue
            self.queued.remove(item)
            read_only = item[1] == es.COMMAND_IDS["read_only"]
            data_file = self.activeDocument.dataFile
            if data_file.isReadOnly != read_only and self.editable is not None:
                self.editable += 1 if not read_only else -1
            data_file.isReadOnly = read_only


class FusionCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(es, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(es, "log", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fusion(self, *files, **kwargs):
        fusion = FakeFusion(self.clock, files, **kwargs)
        self.home = FakeFile("Home", read_only=False)
        fusion.files[self.home.id] = self.home
        fusion.add_doc(self.home, active=True)
        for patcher in (mock.patch.object(es, "get_app", lambda: fusion),
                        mock.patch.object(es, "adsk", type("A", (), {"doEvents": fusion.do_events}))):
            patcher.start()
            self.addCleanup(patcher.stop)
        return fusion

    def run_tool(self, *files, state="read_only", dry_run=False, **extra):
        specs = [f.id if isinstance(f, FakeFile) else f for f in files]
        result = es.set_editable_state({"files": specs, "state": state, "dry_run": dry_run, **extra})
        text = result["content"][0]["text"]
        return json.loads(text) if text.startswith("{") else text, result["isError"]


class ValidationTests(unittest.TestCase):
    def check(self, arguments, fragment):
        result = es.set_editable_state(arguments)
        self.assertTrue(result["isError"])
        self.assertIn(fragment, result["content"][0]["text"])

    def test_rejects_bad_arguments_without_touching_fusion(self):
        good = {"files": ["urn:a"], "state": "read_only"}
        with mock.patch.object(es, "get_app", side_effect=AssertionError("touched Fusion")):
            self.check({**good, "state": "locked"}, "'state'")
            self.check({"files": ["urn:a"]}, "'state'")
            self.check({**good, "dry_run": "no"}, "'dry_run'")
            self.check({**good, "dry_run": 0}, "'dry_run'")
            self.check({**good, "project": "  "}, "'project'")
            self.check({**good, "extra": 1}, "unknown argument")
            self.check({**good, "files": []}, "'files'")
            self.check({**good, "files": "urn:a"}, "'files'")
            self.check({**good, "files": [f"urn:{i}" for i in range(11)]}, "'files'")
            self.check({**good, "files": ["urn:a", 3]}, "'files'")
            self.check({**good, "files": ["urn:a", " "]}, "'files'")
            self.check({**good, "files": ["urn:a", "urn:a"]}, "twice")
            self.check({**good, "files": ["Folder/File"]}, "'project' is required")

    def test_defaults_to_dry_run_and_accepts_ten_files(self):
        files = [f"urn:{i}" for i in range(10)]
        self.assertEqual(es.validate_arguments({"files": files, "state": "editable"}),
                         (files, None, "editable", True))

    def test_schema(self):
        tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "set_editable_state")
        schema = tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["files", "state"])
        self.assertEqual(sorted(schema["properties"]["state"]["enum"]), sorted(es.COMMAND_IDS))

    def test_command_ids(self):
        self.assertEqual(es.COMMAND_IDS, {"read_only": "ActiveDocumentDeactivateCommand",
                                          "editable": "ActiveDocumentActivateCommand"})

    def test_budget_stays_inside_the_main_thread_limit(self):
        tail = es.CLOSE_SETTLE_TIMEOUT_S + es.CLOSE_VERIFY_TIMEOUT_S + es.RESTORE_TIMEOUT_S
        self.assertLess(es.TOTAL_BUDGET_S + tail, 120)
        self.assertLess(es.COMMAND_RESERVE_S, es.PER_FILE_TIMEOUT_S)


def info(file_id="urn:a", **kwargs):
    return {"name": "A", "is_active": False, "is_visible": True, "is_modified": False,
            "file_id": file_id, "version": 3, **kwargs}


def row(file_id="urn:a", before="read_only", **kwargs):
    return {"id": file_id, "before": before, **kwargs}


class PlanTests(unittest.TestCase):
    def test_state_name(self):
        self.assertEqual(es.state_name(True), "read_only")
        self.assertEqual(es.state_name(False), "editable")
        self.assertIsNone(es.state_name(None))
        self.assertIsNone(es.state_name("true"))

    def test_already_in_target_state(self):
        self.assertEqual(es.plan_file(row(before="editable"), [], "editable")["action"], "already")

    def test_unknown_state_is_skipped(self):
        plan = es.plan_file(row(before=None), [], "editable")
        self.assertEqual((plan["action"], plan["reason"]), ("skip", "state_unknown"))

    def test_not_open_means_open(self):
        plan = es.plan_file(row(), [info("urn:other")], "editable")
        self.assertEqual((plan["action"], plan["open_action"]), ("change", "open"))

    def test_active_instance_needs_no_switch(self):
        self.assertEqual(es.plan_file(row(), [info(is_active=True)], "editable")["open_action"], "none")

    def test_inactive_instance_is_activated(self):
        plan = es.plan_file(row(), [info("urn:other"), info()], "editable")
        self.assertEqual((plan["open_action"], plan["index"]), ("activate", 1))

    def test_unsaved_changes_in_any_instance_refuse(self):
        infos = [info(is_active=True), info(name="A (1)", is_modified=True)]
        plan = es.plan_file(row(), infos, "editable")
        self.assertEqual((plan["action"], plan["reason"]), ("skip", "unsaved_changes"))
        self.assertEqual(plan["documents"], ["A (1)"])

    def test_unreadable_modified_flag_counts_as_modified(self):
        self.assertEqual(es.plan_file(row(), [info(is_modified=None)], "editable")["action"], "skip")

    def test_pick_prefers_active_then_visible_then_newest(self):
        self.assertEqual(es.pick_instance([info(version=9), info(version=1, is_active=True)], "urn:a"), 1)
        self.assertEqual(es.pick_instance([info(version=9, is_visible=False), info(version=1)], "urn:a"), 1)
        self.assertEqual(es.pick_instance([info(version=1), info(version=9)], "urn:a"), 1)
        self.assertIsNone(es.pick_instance([info()], "urn:b"))
        self.assertIsNone(es.pick_instance([info(None)], None))

    def test_warnings(self):
        plan = es.plan_file(row(latest_version=12), [info(version=11), info(version=10)], "editable")
        text = " ".join(plan["warnings"])
        self.assertIn("2 instances", text)
        self.assertIn("v11 loaded", text)
        self.assertEqual(es.plan_file(row(), [info()], "editable")["warnings"], [])
        hidden = es.plan_file(row(), [info(is_visible=False)], "editable")
        self.assertIn("hidden", hidden["warnings"][0])

    def test_free_slots(self):
        self.assertEqual(es.free_slots({"count": 8, "max": 10}), 2)
        self.assertEqual(es.free_slots({"count": 12, "max": 10}), 0)
        self.assertIsNone(es.free_slots(None))
        self.assertIsNone(es.free_slots({"count": None, "max": None}))

    def test_batch_refuses_beyond_the_free_slots(self):
        rows = [row("urn:a"), row("urn:b", before="editable"), row("urn:c"), row("urn:d")]
        plans = es.plan_batch(rows, [], "editable", {"count": 8, "max": 10})
        self.assertEqual([p["action"] for p in plans], ["change", "already", "change", "skip"])
        self.assertEqual(plans[3]["reason"], "no_editable_slots")

    def test_batch_with_no_slots_refuses_every_change(self):
        plans = es.plan_batch([row("urn:a"), row("urn:b")], [], "editable", {"count": 10, "max": 10})
        self.assertEqual([p.get("reason") for p in plans], ["no_editable_slots"] * 2)

    def test_limit_ignored_for_read_only_and_paid_licences(self):
        rows = [row(before="editable")]
        self.assertEqual(es.plan_batch(rows, [], "read_only", {"count": 10, "max": 10})[0]["action"], "change")
        self.assertEqual(es.plan_batch([row()], [], "editable", None)[0]["action"], "change")

    def test_close_block(self):
        self.assertIsNone(es.close_block(False, set(), set()))
        self.assertIsNone(es.close_block(False, {"urn:x"}, {"urn:y"}))
        self.assertIsNone(es.close_block(False, None, set()))
        self.assertEqual(es.close_block(True, set(), set()), "unsaved_changes")
        self.assertEqual(es.close_block(None, set(), set()), "unsaved_changes")
        self.assertEqual(es.close_block(False, {"urn:x"}, {"urn:x"}), "links_edited_document")
        self.assertEqual(es.close_block(False, None, {"urn:y"}), "links_edited_document")

    def test_lost_documents(self):
        before = [info("urn:a", name="A v3"), info("urn:a", name="A (1)"), info(None, name="Untitled")]
        self.assertEqual(es.lost_documents(before, before), [])
        self.assertEqual(es.lost_documents(before, [info("urn:a", name="A v4")]), ["A v3", "Untitled"])
        self.assertEqual(es.lost_documents(before[:1], before + [info("urn:b")]), [])

    def test_timeout_message_mentions_dialogs_only_for_editable(self):
        self.assertIn("fusion_dialogs", es.timeout_message("editable", 25))
        self.assertNotIn("fusion_dialogs", es.timeout_message("read_only", 25))

    def test_match_named(self):
        objects = [FakeFile("Arm"), FakeFile("arm plate"), FakeFile("Dup"), FakeFile("Dup", ext="f2d")]
        self.assertEqual(es.match_named(objects, "Arm", "file").name, "Arm")
        self.assertEqual(es.match_named(objects, "ARM", "file").name, "Arm")
        self.assertEqual(es.match_named(objects, "Dup.f2d", "file").fileExtension, "f2d")
        with self.assertRaisesRegex(LookupError, "2 files named"):
            es.match_named(objects, "Dup", "file")
        with self.assertRaisesRegex(LookupError, "No file named"):
            es.match_named(objects, "Nope", "file")


class SettleTrackerTests(unittest.TestCase):
    def poll(self, tracker, now, **overrides):
        values = {"target_active": True, "active_command": "SelectCommand", "document_count": 2, "pump_s": 0.0}
        values.update(overrides)
        return tracker.observe(now, values["target_active"], values["active_command"],
                               values["document_count"], values["pump_s"])

    def test_needs_the_whole_quiet_period(self):
        tracker = es.SettleTracker(1.0)
        self.assertFalse(self.poll(tracker, 0.0))  # first poll has no count to compare
        self.assertFalse(self.poll(tracker, 0.1))
        self.assertFalse(self.poll(tracker, 1.0))
        self.assertTrue(self.poll(tracker, 1.1))

    def test_each_busy_signal_restarts_the_period(self):
        for busy in ({"target_active": False}, {"active_command": "Extrude"},
                     {"pump_s": es.MAX_QUIET_PUMP_S + 0.01}, {"document_count": 3}):
            with self.subTest(busy=busy):
                tracker = es.SettleTracker(1.0)
                self.poll(tracker, 0.0)
                self.poll(tracker, 0.1)
                self.assertFalse(self.poll(tracker, 0.9, **busy))
                self.assertFalse(self.poll(tracker, 1.2, document_count=busy.get("document_count", 2)))
                self.assertTrue(self.poll(tracker, 2.3, document_count=busy.get("document_count", 2)))

    def test_empty_command_counts_as_idle(self):
        tracker = es.SettleTracker(0.0)
        self.poll(tracker, 0.0, active_command=None)
        self.assertTrue(self.poll(tracker, 0.1, active_command=""))


class DryRunTests(FusionCase):
    def test_reports_the_plan_and_touches_nothing(self):
        closed, opened = FakeFile("Closed"), FakeFile("Open")
        done = FakeFile("Done", read_only=False)
        edited = FakeFile("Edited")
        fusion = self.fusion(closed, opened, done, edited, editable=5, max_editable=10)
        fusion.add_doc(opened)
        fusion.add_doc(edited, modified=True)
        result, is_error = self.run_tool(closed, opened, done, edited, state="editable", dry_run=True)
        self.assertFalse(is_error)
        self.assertEqual([f["status"] for f in result["files"]], ["would_change", "would_change", "already", "skipped"])
        self.assertEqual([f.get("open_action") for f in result["files"][:2]], ["open", "activate"])
        self.assertEqual(result["editable_count"], {"before": 5, "max": 10, "free_before": 5})
        self.assertEqual(fusion.calls, [])
        self.assertTrue(closed.isReadOnly)

    def test_is_the_default(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        result = es.set_editable_state({"files": [target.id], "state": "editable"})
        self.assertTrue(json.loads(result["content"][0]["text"])["dry_run"])
        self.assertEqual(fusion.calls, [])

    def test_missing_command_is_reported(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        fusion.missing_commands.add(es.COMMAND_IDS["editable"])
        result, is_error = self.run_tool(target, state="editable", dry_run=True)
        self.assertTrue(is_error)
        self.assertIn("not available", result)


class ResolveTests(FusionCase):
    def tree(self, fusion):
        arm, dup_a, dup_b = FakeFile("Arm"), FakeFile("Dup"), FakeFile("Dup")
        dup_b.id = "urn:Dup2"
        sub = type("Folder", (), {"name": "Sub", "id": "f2", "dataFolders": [], "dataFiles": [arm]})()
        root = type("Folder", (), {"name": "root", "id": "f1", "dataFolders": [sub], "dataFiles": [dup_a, dup_b]})()
        fusion.projects = [type("Project", (), {"name": "Buggy", "id": "p1", "rootFolder": root})()]
        fusion.files[arm.id] = arm
        return arm

    def test_resolves_paths_inside_the_project(self):
        fusion = self.fusion()
        arm = self.tree(fusion)
        result, is_error = self.run_tool("Sub/Arm", project="Buggy", state="editable", dry_run=True)
        self.assertFalse(is_error)
        self.assertEqual(result["files"][0]["id"], arm.id)

    def test_problems_refuse_the_whole_call(self):
        fusion = self.fusion()
        self.tree(fusion)
        result, is_error = self.run_tool("Sub/Arm", "Sub/Nope", "Dup", "urn:missing", project="Buggy")
        self.assertTrue(is_error)
        self.assertEqual(len(result["problems"]), 3)
        self.assertEqual(fusion.calls, [])

    def test_unknown_project(self):
        fusion = self.fusion()
        self.tree(fusion)
        result, is_error = self.run_tool("Sub/Arm", project="Nope")
        self.assertTrue(is_error)
        self.assertIn("No project named", result["problems"][0])

    def test_same_file_by_path_and_id_is_refused(self):
        fusion = self.fusion()
        arm = self.tree(fusion)
        result, is_error = self.run_tool("Sub/Arm", arm.id, project="Buggy")
        self.assertTrue(is_error)
        self.assertIn("same file", result["problems"][0])


class BatchTests(FusionCase):
    def statuses(self, result):
        return [f["status"] for f in result["files"]]

    def test_opens_changes_closes_and_restores(self):
        target = FakeFile("A", read_only=False)
        fusion = self.fusion(target, editable=8, max_editable=10)
        fusion.add_doc(FakeFile("Other"))
        result, is_error = self.run_tool(target)
        self.assertFalse(is_error)
        entry = result["files"][0]
        self.assertEqual((entry["status"], entry["before"], entry["after"]), ("changed", "editable", "read_only"))
        self.assertTrue(entry["opened"] and entry["closed"])
        self.assertFalse(entry["still_open"])
        self.assertGreater(entry["elapsed_seconds"], 0)
        self.assertTrue(target.isReadOnly)
        self.assertEqual(result["editable_count"], {"before": 8, "max": 10, "free_before": 2, "after": 7})
        self.assertTrue(result["complete"])
        self.assertTrue(result["active_document"]["restored"])
        self.assertIs(fusion.activeDocument.dataFile, self.home)
        self.assertEqual([c[0] for c in fusion.calls], ["open", "execute", "close", "activate"])
        self.assertIn(("close", "A", False), fusion.calls)
        self.assertEqual(result["documents_lost"], [])

    def test_command_runs_on_the_target_only_after_it_settled(self):
        target = FakeFile("A", read_only=False)
        fusion = self.fusion(target)
        started = self.clock.now
        original = FakeCommand.execute
        times = []

        def execute(command):
            times.append(self.clock.now - started)
            original(command)

        with mock.patch.object(FakeCommand, "execute", execute):
            self.run_tool(target)
        self.assertGreaterEqual(times[0], es.SETTLE_QUIET_S * 2)
        self.assertIn(("execute", es.COMMAND_IDS["read_only"], "A"), fusion.calls)

    def test_reused_document_stays_open(self):
        target = FakeFile("A")
        fusion = self.fusion(target, editable=1, max_editable=10)
        fusion.add_doc(target)
        result, _ = self.run_tool(target, state="editable")
        entry = result["files"][0]
        self.assertEqual(entry["status"], "changed")
        self.assertEqual(entry["open_action"], "activate")
        self.assertFalse(entry["opened"] or entry["closed"])
        self.assertEqual(len(fusion.docs), 2)
        self.assertFalse(any(c[0] in ("open", "close") for c in fusion.calls))
        self.assertIs(fusion.activeDocument.dataFile, self.home)

    def test_active_document_needs_no_activation(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        fusion.activeDocument = fusion.add_doc(target)
        result, _ = self.run_tool(target, state="editable")
        self.assertEqual(result["files"][0]["open_action"], "none")
        self.assertFalse(any(c[0] == "activate" for c in fusion.calls))

    def test_already_in_state_touches_nothing(self):
        target = FakeFile("A", read_only=True)
        fusion = self.fusion(target)
        result, is_error = self.run_tool(target)
        self.assertFalse(is_error)
        self.assertEqual(self.statuses(result), ["already"])
        self.assertEqual(result["files"][0]["after"], "read_only")
        self.assertEqual(fusion.calls, [])
        self.assertTrue(result["active_document"]["restored"])

    def test_unsaved_changes_refused_and_batch_continues(self):
        edited, clean = FakeFile("Edited", read_only=False), FakeFile("Clean", read_only=False)
        fusion = self.fusion(edited, clean)
        doc = fusion.add_doc(edited, modified=True)
        result, is_error = self.run_tool(edited, clean)
        self.assertTrue(is_error)
        self.assertEqual(self.statuses(result), ["skipped", "changed"])
        self.assertEqual(result["files"][0]["reason"], "unsaved_changes")
        self.assertFalse(edited.isReadOnly)
        self.assertIn(doc, fusion.docs)
        self.assertEqual(doc.closed_with, [])

    def test_document_modified_by_opening_is_left_open(self):
        target = FakeFile("A", read_only=False)
        fusion = self.fusion(target)
        fusion.open_kwargs["A"] = {"modified": True}
        result, is_error = self.run_tool(target)
        entry = result["files"][0]
        self.assertTrue(is_error)
        self.assertEqual((entry["status"], entry["reason"]), ("skipped", "unsaved_changes"))
        self.assertTrue(entry["still_open"])
        self.assertFalse(any(c[0] in ("execute", "close") for c in fusion.calls))

    def test_no_editable_slots_refused_up_front(self):
        target = FakeFile("A")
        fusion = self.fusion(target, editable=10, max_editable=10)
        result, is_error = self.run_tool(target, state="editable")
        self.assertTrue(is_error)
        self.assertEqual(result["files"][0]["reason"], "no_editable_slots")
        self.assertEqual(fusion.calls, [])

    def test_paid_licence_reports_null_counts(self):
        target = FakeFile("A")
        self.fusion(target)
        result, is_error = self.run_tool(target, state="editable")
        self.assertFalse(is_error)
        self.assertEqual(result["editable_count"],
                         {"before": None, "max": None, "free_before": None, "after": None})

    def test_timeout_leaves_target_active_and_stops(self):
        first, second = FakeFile("A"), FakeFile("B")
        fusion = self.fusion(first, second)
        fusion.command_delay = 10 ** 9
        result, is_error = self.run_tool(first, second, state="editable")
        self.assertTrue(is_error)
        self.assertEqual(self.statuses(result), ["failed", "not_attempted"])
        entry = result["files"][0]
        self.assertEqual(entry["reason"], "timeout")
        self.assertIn("fusion_dialogs", entry["message"])
        self.assertLessEqual(entry["elapsed_seconds"], es.PER_FILE_TIMEOUT_S + 2)
        self.assertTrue(entry["command_pending"] and entry["still_open"])
        self.assertTrue(result["command_pending"])
        self.assertEqual(result["files"][1]["reason"], "stopped_after_failure")
        self.assertEqual(result["remaining_files"], [second.id])
        # The late command must not land on the user's document.
        self.assertFalse(result["active_document"]["restored"])
        self.assertIs(fusion.activeDocument.dataFile, first)
        self.assertFalse(any(c[0] == "close" for c in fusion.calls))

    def test_busy_fusion_means_the_command_is_never_run(self):
        first, second = FakeFile("A"), FakeFile("B")
        fusion = self.fusion(first, second)
        fusion.userInterface.activeCommand = "Extrude"
        result, is_error = self.run_tool(first, second, state="editable")
        self.assertTrue(is_error)
        self.assertEqual(self.statuses(result), ["failed", "not_attempted"])
        self.assertEqual(result["files"][0]["reason"], "not_settled")
        self.assertIn("Extrude", result["files"][0]["message"])
        self.assertEqual(fusion.calls[-1:], [])
        self.assertFalse(result["command_pending"])

    def test_slow_pump_counts_as_busy(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        fusion.pump_seconds = es.MAX_QUIET_PUMP_S + 0.1
        result, _ = self.run_tool(target, state="editable")
        self.assertEqual(result["files"][0]["reason"], "not_settled")

    def test_long_open_does_not_starve_the_settle(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        fusion.open_seconds = es.PER_FILE_TIMEOUT_S + 5
        result, _ = self.run_tool(target, state="editable")
        self.assertEqual(self.statuses(result), ["changed"])

    def test_unsettled_open_document_is_not_closed(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        fusion.activeProduct = None
        result, _ = self.run_tool(target, state="editable")
        entry = result["files"][0]
        self.assertEqual(entry["reason"], "not_settled")
        self.assertTrue(entry["opened"] and entry["still_open"])
        self.assertFalse(any(c[0] == "close" for c in fusion.calls))

    def test_time_budget_leaves_files_not_attempted(self):
        files = [FakeFile(f"F{i}") for i in range(10)]
        fusion = self.fusion(*files)
        fusion.open_seconds = 20.0
        result, is_error = self.run_tool(*files, state="editable")
        self.assertFalse(is_error)
        self.assertFalse(result["complete"])
        statuses = self.statuses(result)
        self.assertIn("changed", statuses)
        self.assertEqual(statuses[-1], "not_attempted")
        self.assertEqual(result["files"][-1]["reason"], "time_budget")
        self.assertEqual(len(result["remaining_files"]), statuses.count("not_attempted"))
        self.assertLess(result["elapsed_seconds"], 120)

    def test_slow_open_at_the_end_of_the_budget_is_not_a_failure(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        fusion.open_seconds = 5.0
        real_run = es._change_file

        def late(app, cmd_def, row, state, batch_deadline, entry):
            real_run(app, cmd_def, row, state, self.clock.now + es.COMMAND_RESERVE_S + 3, entry)

        with mock.patch.object(es, "_change_file", late):
            result, is_error = self.run_tool(target, state="editable")
        entry = result["files"][0]
        self.assertFalse(is_error)
        self.assertEqual((entry["status"], entry["reason"]), ("not_attempted", "time_budget"))
        self.assertTrue(entry["closed"])
        self.assertEqual(result["remaining_files"], [target.id])
        self.assertFalse(any(c[0] == "execute" for c in fusion.calls))

    def test_second_call_finishes_the_remainder(self):
        files = [FakeFile(f"F{i}") for i in range(10)]
        fusion = self.fusion(*files)
        fusion.open_seconds = 20.0
        result, _ = self.run_tool(*files, state="editable")
        fusion.open_seconds = 0.0
        second, _ = self.run_tool(*result["remaining_files"], state="editable")
        self.assertTrue(second["complete"])
        self.assertTrue(all(f.isReadOnly is False for f in files))

    def test_one_file_at_a_time(self):
        files = [FakeFile("A"), FakeFile("B")]
        fusion = self.fusion(*files)
        self.run_tool(*files, state="editable")
        steps = [c[0] for c in fusion.calls]
        self.assertEqual(steps, ["open", "execute", "close", "open", "execute", "close"])

    def test_close_held_by_a_parent_reports_still_open(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        fusion.open_kwargs["A"] = {"held": True}
        result, is_error = self.run_tool(target, state="editable")
        entry = result["files"][0]
        self.assertFalse(is_error)
        self.assertEqual(entry["status"], "changed")
        self.assertTrue(entry["still_open"])
        self.assertFalse(entry["closed"])
        self.assertIn("held by", entry["close_note"])

    def test_close_raising_does_not_undo_a_verified_change(self):
        target = FakeFile("A")
        self.fusion(target)
        with mock.patch.object(FakeDoc, "close", side_effect=RuntimeError("nope")):
            result, is_error = self.run_tool(target, state="editable")
        self.assertFalse(is_error)
        self.assertEqual(result["files"][0]["status"], "changed")
        self.assertIn("nope", result["files"][0]["close_note"])
        self.assertTrue(result["files"][0]["still_open"])

    def test_document_linking_an_edited_one_stays_open(self):
        target, edited = FakeFile("A"), FakeFile("Edited")
        fusion = self.fusion(target, edited)
        fusion.add_doc(edited, modified=True)
        fusion.open_kwargs["A"] = {"links": [edited]}
        result, _ = self.run_tool(target, state="editable")
        entry = result["files"][0]
        self.assertEqual(entry["status"], "changed")
        self.assertEqual(entry["close_skipped"], "links_edited_document")
        self.assertTrue(entry["still_open"])
        self.assertFalse(any(c[0] == "close" for c in fusion.calls))

    def test_refused_activation_fails_and_stops(self):
        first, second = FakeFile("A"), FakeFile("B")
        fusion = self.fusion(first, second)
        fusion.add_doc(first, visible=False)
        fusion.refuse_activate = True
        result, is_error = self.run_tool(first, second, state="editable")
        self.assertTrue(is_error)
        self.assertEqual(self.statuses(result), ["failed", "not_attempted"])
        self.assertEqual(result["files"][0]["reason"], "activate_failed")
        self.assertTrue(any("hidden" in w for w in result["files"][0]["warnings"]))

    def test_open_failure_fails_and_stops(self):
        first, second = FakeFile("A"), FakeFile("B")
        fusion = self.fusion(first, second)
        with mock.patch.object(FakeDocuments, "open", return_value=None):
            result, _ = self.run_tool(first, second, state="editable")
        self.assertEqual(self.statuses(result), ["failed", "not_attempted"])
        self.assertEqual(result["files"][0]["reason"], "open_failed")
        self.assertEqual(fusion.queued, [])

    def test_exception_is_a_failure_with_partial_results(self):
        first, second = FakeFile("A"), FakeFile("B")
        self.fusion(first, second)
        with mock.patch.object(FakeDocuments, "open", side_effect=RuntimeError("boom")):
            result, is_error = self.run_tool(first, second, state="editable")
        self.assertTrue(is_error)
        self.assertEqual(result["files"][0]["reason"], "error")
        self.assertIn("boom", result["files"][0]["message"])

    def test_state_already_reached_after_opening(self):
        target = FakeFile("A", read_only=False)
        fusion = self.fusion(target)
        real_open = fusion.documents.open

        def open_and_flip(data_file, visible):
            data_file.isReadOnly = True
            return real_open(data_file, visible)

        with mock.patch.object(fusion.documents, "open", open_and_flip):
            result, _ = self.run_tool(target)
        entry = result["files"][0]
        self.assertEqual((entry["status"], entry["after"]), ("already", "read_only"))
        self.assertTrue(entry["closed"])
        self.assertFalse(any(c[0] == "execute" for c in fusion.calls))

    def test_lost_starting_document_is_reported(self):
        target = FakeFile("A")
        fusion = self.fusion(target)
        start = fusion.activeDocument
        real_execute = FakeCommand.execute

        def execute(command):
            fusion.docs.remove(start)
            real_execute(command)

        with mock.patch.object(FakeCommand, "execute", execute):
            result, _ = self.run_tool(target, state="editable")
        self.assertFalse(result["active_document"]["restored"])
        self.assertIn("note", result["active_document"])
        self.assertEqual(result["documents_lost"], ["Home"])


class ItemsTests(unittest.TestCase):
    def test_vector_without_count_or_item(self):
        self.assertEqual(es._items(iter([1, 2])), [1, 2])

    def test_collection_and_none(self):
        self.assertEqual(es._items(None), [])

        class Collection:
            count = 2

            def item(self, i):
                return i * 10

        self.assertEqual(es._items(Collection()), [0, 10])


if __name__ == "__main__":
    unittest.main()
