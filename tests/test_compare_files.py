"""compare_files: validation, the comparison, and the open/read/close run on fakes."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401

from fusion_bridge import compare_files as cf
from fusion_bridge import tool_surface

ID_A = "urn:adsk.wipprod:dm.lineage:a"
ID_B = "urn:adsk.wipprod:dm.lineage:b"


class Vector:
    """Fusion vectors iterate but have no count/item."""

    def __init__(self, items):
        self.items = items

    def __iter__(self):
        return iter(self.items)


class Collection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def count(self):
        return len(self.items)

    def item(self, i):
        return self.items[i]


class FakeBody:
    def __init__(self, volume):
        self.volume = volume


class FakeComponent:
    def __init__(self, name, volumes=(), component_id=None):
        self.name = name
        self.id = component_id or f"id-{name}"
        self.bRepBodies = Collection(FakeBody(v) for v in volumes)
        self.allOccurrences = Collection([])


class FakeOccurrence:
    def __init__(self, component):
        self.component = component


class FakeTimeline:
    def __init__(self, count, marker=None):
        self.count = count
        self.markerPosition = count if marker is None else marker


class FakeDesign:
    def __init__(self, root, others=(), occurrences=(), timeline_count=5, direct=False, marker=None):
        self.rootComponent = root
        self.allComponents = Collection([root, *others])
        root.allOccurrences = Collection(FakeOccurrence(c) for c in occurrences)
        self.designType = 0 if direct else 1
        self.timeline = None if direct else FakeTimeline(timeline_count, marker)


def simple_design(root="Root", volume=10.0, timeline_count=5, parts=("Part",)):
    components = [FakeComponent(name, (volume,)) for name in parts]
    return FakeDesign(FakeComponent(root), components, components, timeline_count)


class FakeDataFile:
    def __init__(self, file_id, name, design, extension="f3d", version=3, latest=3):
        self.id = file_id
        self.name = name
        self.design = design
        self.fileExtension = extension
        self.versionNumber = version
        self.latestVersionNumber = latest


class FakeProducts:
    def __init__(self, design):
        self.design = design

    def itemByProductType(self, product_type):
        assert product_type == "DesignProductType"
        return self.design


class FakeDocument:
    def __init__(self, app, data_file, visible=True):
        self.app = app
        self.dataFile = data_file
        self.name = f"{data_file.name} v{data_file.versionNumber}"
        self.isVisible = visible
        self.isModified = False
        self.products = FakeProducts(data_file.design)
        self.documentReferences = Collection([])
        self.close_works = True
        self.activate_works = True
        self.closed_calls = []

    @property
    def isActive(self):
        return self.app.activeDocument is self

    def activate(self):
        self.app.events.append(("activate", self.dataFile.name))
        if self.activate_works:
            self.app.activeDocument = self
        return self.activate_works

    def close(self, save):
        self.closed_calls.append(save)
        self.app.events.append(("close", self.dataFile.name))
        if self.close_works:
            self.app.open_documents.remove(self)
            if self.app.activeDocument is self:
                self.app.activeDocument = self.app.open_documents[0] if self.app.open_documents else None
        return True


class FakeDocuments:
    def __init__(self, app):
        self.app = app

    @property
    def count(self):
        return len(self.app.open_documents)

    def item(self, i):
        return self.app.open_documents[i]

    def open(self, data_file, visible=True):
        self.app.events.append(("open", data_file.name))
        if self.app.open_returns_none:
            return None
        document = FakeDocument(self.app, data_file)
        self.app.open_documents.append(document)
        self.app.activeDocument = document
        if self.app.on_open:
            self.app.on_open(document)
        return document


class FakeFolder:
    def __init__(self, name="", files=(), folders=()):
        self.name = name
        self.dataFiles = Vector(list(files))
        self.dataFolders = Collection(folders)


class FakeProject:
    def __init__(self, name, root):
        self.id = f"pid-{name}"
        self.name = name
        self.rootFolder = root


class FakeData:
    def __init__(self, files, projects):
        self.files = {f.id: f for f in files}
        self.dataProjects = Collection(projects)

    def findFileById(self, file_id):
        return self.files.get(file_id)


class FakeUI:
    activeCommand = "SelectCommand"


class FakeApp:
    def __init__(self, files, projects=()):
        self.data = FakeData(files, projects)
        self.open_documents = []
        self.activeDocument = None
        self.documents = FakeDocuments(self)
        self.userInterface = FakeUI()
        self.events = []
        self.open_returns_none = False
        self.on_open = None

    def already_open(self, data_file, active=True, visible=True):
        document = FakeDocument(self, data_file, visible)
        self.open_documents.append(document)
        if active:
            self.activeDocument = document
        return document


class Harness(unittest.TestCase):
    def setUp(self):
        self.file_a = FakeDataFile(ID_A, "Winch", simple_design("Winch"))
        self.file_b = FakeDataFile(ID_B, "Winch copy", simple_design("Winch copy"))
        self.app = FakeApp([self.file_a, self.file_b])
        design = type("Design", (), {"cast": staticmethod(lambda product: product)})
        for target, name, value in ((cf, "get_app", lambda: self.app), (cf, "pump", lambda: 0.0),
                                    (cf, "log", lambda *a, **k: None), (cf, "SETTLE_QUIET_S", 0.0),
                                    (cf, "CLOSE_SETTLE_QUIET_S", 0.0), (cf, "_POLL_S", 0.0),
                                    (cf, "CLOSE_VERIFY_TIMEOUT_S", 0.0), (cf, "RESTORE_TIMEOUT_S", 0.0)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cf.adsk.fusion, "Design", design, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, **arguments):
        arguments.setdefault("file_a", ID_A)
        arguments.setdefault("file_b", ID_B)
        result = cf.compare_files(arguments)
        text = result["content"][0]["text"]
        try:
            return json.loads(text), result["isError"]
        except ValueError:
            return text, result["isError"]


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict(self):
        tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "compare_files")
        schema = tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["file_a", "file_b"])
        self.assertEqual(set(schema["properties"]), cf._ARGS)
        self.assertIn("not proof", tool["description"])


class ValidationTests(unittest.TestCase):
    def test_ids_need_no_project(self):
        self.assertEqual(cf.validate_arguments({"file_a": ID_A, "file_b": ID_B}), (ID_A, ID_B, None, None))

    def test_project_b_defaults_to_project(self):
        self.assertEqual(cf.validate_arguments({"file_a": "a", "file_b": "b", "project": "P"})[2:], ("P", "P"))
        self.assertEqual(cf.validate_arguments({"file_a": "a", "file_b": "b", "project": "P", "project_b": "Q"})[2:],
                         ("P", "Q"))

    def test_same_path_in_two_projects_is_allowed(self):
        arguments = {"file_a": "a", "file_b": "a", "project": "P", "project_b": "Q"}
        self.assertEqual(cf.validate_arguments(arguments), ("a", "a", "P", "Q"))

    def test_rejects_bad_arguments(self):
        for arguments in ({}, {"file_a": ID_A}, {"file_a": "", "file_b": ID_B}, {"file_a": 1, "file_b": ID_B},
                          {"file_a": "path", "file_b": ID_B}, {"file_a": ID_A, "file_b": ID_A},
                          {"file_a": ID_A, "file_b": ID_B, "project": ""},
                          {"file_a": ID_A, "file_b": ID_B, "project_b": 3},
                          {"file_a": ID_A, "file_b": ID_B, "extra": 1}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                cf.validate_arguments(arguments)

    def test_bad_arguments_never_reach_fusion(self):
        with mock.patch.object(cf, "get_app", side_effect=AssertionError("touched Fusion")):
            self.assertTrue(cf.compare_files({"file_a": ID_A})["isError"])


class ReadDesignTests(unittest.TestCase):
    def test_counts_every_instance_and_each_component_once(self):
        part = FakeComponent("Part", (2.0, 3.0))
        root = FakeComponent("Root", (1.0,))
        facts = cf.read_design(FakeDesign(root, [part], [part, part, part], timeline_count=32))
        self.assertEqual(facts["occurrence_count"], 3)
        self.assertEqual(facts["component_count"], 2)
        self.assertEqual(facts["body_count"], 7)
        self.assertEqual(facts["unique_body_count"], 3)
        self.assertEqual(facts["total_volume_cm3"], 16.0)
        self.assertEqual(facts["unique_volume_cm3"], 6.0)
        self.assertEqual(facts["timeline_length"], 32)

    def test_root_is_left_out_of_the_names(self):
        facts = cf.read_design(simple_design("Warn M8000", parts=("Drum", "Bracket")))
        self.assertEqual(facts["component_names"], ["Bracket", "Drum"])
        self.assertEqual(facts["root_component"], "Warn M8000")

    def test_direct_design_has_no_timeline(self):
        facts = cf.read_design(FakeDesign(FakeComponent("Root", (1.0,)), direct=True))
        self.assertEqual(facts["design_type"], "direct")
        self.assertIsNone(facts["timeline_length"])
        self.assertEqual(facts["body_count"], 1)

    def test_empty_design(self):
        facts = cf.read_design(FakeDesign(FakeComponent("Root")))
        self.assertEqual((facts["occurrence_count"], facts["body_count"], facts["total_volume_cm3"]), (0, 0, 0.0))

    def test_copied_components_sharing_an_id_stay_separate(self):
        first = FakeComponent("Left", (1.0,), component_id="same")
        second = FakeComponent("Right", (2.0, 2.0), component_id="same")
        facts = cf.read_design(FakeDesign(FakeComponent("Root"), [first, second], [first, second]))
        self.assertEqual((facts["body_count"], facts["total_volume_cm3"]), (3, 5.0))

    def test_unreadable_volume_is_counted(self):
        class Bad:
            @property
            def volume(self):
                raise RuntimeError("no")

        part = FakeComponent("Part", (1.0,))
        part.bRepBodies.items.append(Bad())
        facts = cf.read_design(FakeDesign(FakeComponent("Root"), [part], [part]))
        self.assertEqual((facts["body_count"], facts["total_volume_cm3"], facts["unreadable_volumes"]), (2, 1.0, 1))

    def test_occurrence_without_a_component(self):
        design = FakeDesign(FakeComponent("Root", (1.0,)))
        design.rootComponent.allOccurrences = Collection([FakeOccurrence(None)])
        self.assertEqual(cf.read_design(design)["occurrence_count"], 1)


def facts(**overrides):
    base = {"design_type": "parametric", "occurrence_count": 22, "component_count": 10, "body_count": 23,
            "unique_body_count": 20, "timeline_length": 32, "total_volume_cm3": 4226.2,
            "unique_volume_cm3": 4000.0, "component_names": ["a", "b"], "unreadable_volumes": 0,
            "marker_position": 32, "root_component": "R"}
    base.update(overrides)
    return base


class CompareFactsTests(unittest.TestCase):
    def test_matching(self):
        self.assertEqual(cf.compare_facts(facts(root_component="A"), facts(root_component="B")), [])

    def test_each_field_is_compared(self):
        for field, value in (("design_type", "direct"), ("occurrence_count", 21), ("component_count", 9),
                             ("body_count", 22), ("unique_body_count", 19), ("timeline_length", None)):
            with self.subTest(field=field):
                self.assertEqual(cf.compare_facts(facts(), facts(**{field: value})),
                                 [{"field": field, "a": facts()[field], "b": value}])

    def test_volume_tolerance(self):
        self.assertEqual(cf.compare_facts(facts(), facts(total_volume_cm3=4226.2 * (1 + 1e-9))), [])
        difference = cf.compare_facts(facts(), facts(total_volume_cm3=4226.3))
        self.assertEqual(difference[0]["field"], "total_volume_cm3")

    def test_zero_volumes_match(self):
        self.assertEqual(cf.compare_facts(facts(total_volume_cm3=0.0), facts(total_volume_cm3=0.0)), [])

    def test_names_are_a_multiset(self):
        differences = cf.compare_facts(facts(component_names=["a", "a", "b"]), facts(component_names=["a", "b", "c"]))
        self.assertEqual(differences, [{"field": "component_names", "only_in_a": ["a"], "only_in_b": ["c"]}])

    def test_public_facts_rounds_and_bounds(self):
        names = [f"n{i}" for i in range(cf.MAX_LISTED_NAMES + 3)]
        report = cf.public_facts(facts(total_volume_cm3=4226.15, component_names=names))
        self.assertEqual(report["total_volume_in3"], 257.895)
        self.assertEqual(len(report["component_names"]), cf.MAX_LISTED_NAMES)
        self.assertEqual(report["component_names_not_listed"], 3)


class PureHelperTests(unittest.TestCase):
    def test_budget(self):
        self.assertTrue(cf.fits_budget(40.0, 40.0))
        self.assertTrue(cf.fits_budget(50.0, 50.0))
        self.assertFalse(cf.fits_budget(55.0, 55.0))

    def test_pick_instance_prefers_active_then_visible_then_newest(self):
        infos = [{"file_id": "x", "is_active": False, "is_visible": False, "version": 12},
                 {"file_id": "x", "is_active": False, "is_visible": True, "version": 11},
                 {"file_id": "y", "is_active": True}]
        self.assertEqual(cf.pick_instance(infos, "x"), 1)
        infos[0]["is_active"] = True
        self.assertEqual(cf.pick_instance(infos, "x"), 0)
        self.assertIsNone(cf.pick_instance(infos, "z"))
        self.assertIsNone(cf.pick_instance(infos, None))

    def test_instance_warnings(self):
        row = {"id": "x", "latest_version": 12}
        infos = [{"file_id": "x", "name": "K", "is_modified": True, "version": 11}, {"file_id": "x", "name": "K (1)"}]
        warnings = cf.instance_warnings(row, infos, 0)
        self.assertEqual(len(warnings), 3)
        self.assertEqual(cf.instance_warnings({"id": "x", "latest_version": 3},
                                              [{"file_id": "x", "is_modified": False, "version": 3}], 0), [])

    def test_close_block(self):
        self.assertEqual(cf.close_block(True, set(), set()), "unsaved_changes")
        self.assertEqual(cf.close_block(None, set(), set()), "unsaved_changes")
        self.assertEqual(cf.close_block(False, {"x"}, {"x"}), "links_edited_document")
        self.assertEqual(cf.close_block(False, None, {"x"}), "links_edited_document")
        self.assertIsNone(cf.close_block(False, None, set()))
        self.assertIsNone(cf.close_block(False, {"y"}, {"x"}))

    def test_lost_documents(self):
        before = [{"file_id": "x", "name": "X v1"}, {"file_id": "x", "name": "X v1 (1)"}, {"name": "Unsaved"}]
        self.assertEqual(cf.lost_documents(before, [{"file_id": "x", "name": "X v2"}]), ["X v1", "Unsaved"])

    def test_settle_needs_a_stable_document_count(self):
        tracker = cf.SettleTracker(1.0)
        self.assertFalse(tracker.observe(0.0, True, "SelectCommand", 3, 0.0))
        self.assertFalse(tracker.observe(0.5, True, "SelectCommand", 4, 0.0))
        self.assertFalse(tracker.observe(1.0, True, "SelectCommand", 4, 0.0))
        self.assertTrue(tracker.observe(2.0, True, "SelectCommand", 4, 0.0))

    def test_settle_breaks_on_activity(self):
        tracker = cf.SettleTracker(1.0)
        tracker.observe(0.0, True, None, 1, 0.0)
        tracker.observe(0.1, True, None, 1, 0.0)
        self.assertFalse(tracker.observe(1.5, True, "SomeCommand", 1, 0.0))
        self.assertFalse(tracker.observe(3.0, True, None, 1, 1.0))
        self.assertFalse(tracker.observe(4.0, False, None, 1, 0.0))


class RunTests(Harness):
    def test_matching_files_open_read_close_one_at_a_time(self):
        report, is_error = self.call()
        self.assertFalse(is_error)
        self.assertIs(report["same"], True)
        self.assertEqual(report["differences"], [])
        self.assertIn("not proof", report["note"])
        self.assertEqual(self.app.events, [("open", "Winch"), ("close", "Winch"),
                                           ("open", "Winch copy"), ("close", "Winch copy")])
        self.assertEqual(self.app.open_documents, [])
        self.assertTrue(report["file_a"]["closed"])
        self.assertEqual(report["file_a"]["facts"]["body_count"], 1)

    def test_live_numbers_shape(self):
        self.file_a.design = self.file_b.design = FakeDesign(
            FakeComponent("Root", (257.898 * cf.CM3_PER_IN3,)), timeline_count=32)
        report, _ = self.call()
        self.assertEqual(report["file_a"]["facts"]["total_volume_in3"], 257.898)
        self.assertEqual(report["file_a"]["facts"]["timeline_length"], 32)

    def test_differences_are_listed(self):
        self.file_b.design = simple_design("Winch copy", volume=11.0, timeline_count=6, parts=("Other",))
        report, is_error = self.call()
        self.assertFalse(is_error)
        self.assertIs(report["same"], False)
        fields = [d["field"] for d in report["differences"]]
        self.assertEqual(fields, ["timeline_length", "total_volume_cm3", "unique_volume_cm3", "component_names"])

    def test_direct_against_parametric(self):
        self.file_b.design = FakeDesign(FakeComponent("R", (10.0,)), direct=True)
        report, _ = self.call()
        self.assertIn("design_type", [d["field"] for d in report["differences"]])

    def test_open_instance_is_reused_and_left_open(self):
        document = self.app.already_open(self.file_a)
        report, _ = self.call()
        self.assertTrue(report["file_a"]["reused"])
        self.assertFalse(report["file_a"]["opened"])
        self.assertEqual(self.app.open_documents, [document])
        self.assertNotIn(("open", "Winch"), self.app.events)
        self.assertEqual(document.closed_calls, [])

    def test_background_instance_is_brought_forward_then_active_restored(self):
        active = self.app.already_open(self.file_b)
        self.app.already_open(self.file_a, active=False)
        report, _ = self.call()
        self.assertIn(("activate", "Winch"), self.app.events)
        self.assertIs(self.app.activeDocument, active)
        self.assertTrue(report["active_document"]["restored"])
        self.assertEqual(len(self.app.open_documents), 2)

    def test_active_document_comes_back_after_closes(self):
        other = FakeDataFile("urn:other", "Other", simple_design())
        active = self.app.already_open(other)
        report, _ = self.call()
        self.assertIs(self.app.activeDocument, active)
        self.assertEqual(report["documents_lost"], [])

    def test_no_active_document(self):
        report, is_error = self.call()
        self.assertFalse(is_error)
        self.assertFalse(report["active_document"]["restored"])

    def test_refuses_to_read_an_instance_that_will_not_activate(self):
        self.app.already_open(self.file_b)
        hidden = self.app.already_open(self.file_a, active=False, visible=False)
        hidden.activate_works = False
        report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertIsNone(report["same"])
        self.assertEqual(report["file_a"]["reason"], "activate_failed")
        self.assertEqual(report["file_b"]["status"], "not_attempted")
        self.assertNotIn("facts", report["file_a"])

    def test_reused_old_version_and_unsaved_changes_are_warned(self):
        self.file_a.latestVersionNumber = 5
        self.app.already_open(self.file_a).isModified = True
        report, _ = self.call()
        self.assertEqual(len(report["file_a"]["warnings"]), 2)

    def test_rolled_back_marker_is_warned(self):
        self.file_a.design.timeline.markerPosition = 2
        report, _ = self.call()
        self.assertIn("marker is at 2 of 5", report["file_a"]["warnings"][0])

    def test_document_modified_after_open_is_left_open(self):
        def dirty(document):
            if document.dataFile is self.file_a:
                document.isModified = True

        self.app.on_open = dirty
        report, _ = self.call()
        self.assertEqual(report["file_a"]["close_skipped"], "unsaved_changes")
        self.assertTrue(report["file_a"]["still_open"])
        self.assertEqual(len(self.app.open_documents), 1)

    def test_never_closes_a_document_linking_an_edited_one(self):
        edited = FakeDataFile("urn:edited", "Edited", simple_design())
        self.app.already_open(edited).isModified = True

        def link(document):
            document.documentReferences = Collection([type("Ref", (), {"dataFile": edited})()])

        self.app.on_open = link
        report, _ = self.call()
        self.assertEqual(report["file_a"]["close_skipped"], "links_edited_document")

    def test_close_that_does_nothing_is_reported(self):
        def hold(document):
            document.close_works = False

        self.app.on_open = hold
        report, _ = self.call()
        self.assertTrue(report["file_a"]["still_open"])
        self.assertFalse(report["file_a"]["closed"])
        self.assertIn("held by", report["file_a"]["close_note"])

    def test_close_runs_even_when_the_read_raises(self):
        def broken(document):
            document.products = mock.Mock(itemByProductType=mock.Mock(side_effect=RuntimeError("boom")))

        self.app.on_open = broken
        report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertEqual(report["file_a"]["reason"], "error")
        self.assertIn(("close", "Winch"), self.app.events)
        self.assertEqual(report["file_b"]["reason"], "stopped_after_failure")

    def test_no_design_product(self):
        self.file_a.design = None
        report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertEqual(report["file_a"]["reason"], "no_design")
        self.assertIn(("close", "Winch"), self.app.events)

    def test_failed_open(self):
        self.app.open_returns_none = True
        report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertEqual(report["file_a"]["reason"], "open_failed")

    def test_lost_document_is_reported(self):
        other = FakeDataFile("urn:other", "Other", simple_design())
        victim = self.app.already_open(other)

        def close_victim(document):
            original = document.close

            def close(save):
                original(save)
                if victim in self.app.open_documents:
                    self.app.open_documents.remove(victim)
                    self.app.activeDocument = None
                    victim.activate = mock.Mock(side_effect=RuntimeError("invalid object"))

            document.close = close

        self.app.on_open = close_victim
        report, _ = self.call()
        self.assertEqual(report["documents_lost"], [victim.name])
        self.assertFalse(report["active_document"]["restored"])

    def test_slow_first_file_stops_before_the_second(self):
        with mock.patch.object(cf, "fits_budget", return_value=False):
            report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertIsNone(report["same"])
        self.assertFalse(report["complete"])
        self.assertIn("facts", report["file_a"])
        self.assertEqual(report["file_b"]["reason"], "time_budget")
        self.assertNotIn(("open", "Winch copy"), self.app.events)

    def test_busy_fusion_touches_nothing(self):
        self.app.userInterface.activeCommand = "SomeCommand"
        with mock.patch.object(cf, "SETTLE_TIMEOUT_S", 0.0):
            report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertEqual(report["file_a"]["reason"], "not_settled")
        self.assertEqual(self.app.events, [])

    def test_never_settling_load_reads_nothing_and_leaves_it_open(self):
        def wait(_app, document, _deadline, _quiet_s=None):
            return document is None

        with mock.patch.object(cf, "_wait_settled", wait):
            report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertEqual(report["file_a"]["reason"], "not_settled")
        self.assertNotIn("facts", report["file_a"])


class ResolutionTests(Harness):
    def setUp(self):
        super().setUp()
        folder = FakeFolder("Archive", [self.file_b])
        root = FakeFolder("", [self.file_a], [folder])
        self.app.data.dataProjects = Collection([FakeProject("Buggy", root)])

    def test_paths_in_a_project(self):
        report, _ = self.call(file_a="Winch", file_b="Archive/Winch copy", project="Buggy")
        self.assertIs(report["same"], True)

    def test_id_and_path_mix(self):
        report, _ = self.call(file_a=ID_A, file_b="Archive/Winch copy", project="Buggy")
        self.assertIs(report["same"], True)

    def test_extension_and_case_fallback(self):
        report, _ = self.call(file_a="winch.f3d", file_b="archive/WINCH COPY", project="Buggy")
        self.assertIs(report["same"], True)

    def test_two_files_of_one_name_are_refused(self):
        twin = FakeDataFile("urn:twin", "Winch", simple_design())
        self.app.data.dataProjects.items[0].rootFolder.dataFiles.items.append(twin)
        report, is_error = self.call(file_a="Winch", file_b=ID_B, project="Buggy")
        self.assertTrue(is_error)
        self.assertIn("use the id", report["problems"][0])
        self.assertEqual(self.app.events, [])

    def test_unknown_file_opens_nothing(self):
        report, is_error = self.call(file_b="urn:missing")
        self.assertTrue(is_error)
        self.assertIn("No file with id", report["problems"][0])
        self.assertEqual(self.app.events, [])

    def test_unknown_project(self):
        report, is_error = self.call(file_a="Winch", file_b=ID_B, project="Nope")
        self.assertTrue(is_error)
        self.assertIn("No project", report["problems"][0])

    def test_empty_path(self):
        report, is_error = self.call(file_a="/", file_b=ID_B, project="Buggy")
        self.assertTrue(is_error)
        self.assertIn("does not name a file", report["problems"][0])

    def test_two_paths_to_the_same_file_are_refused(self):
        report, is_error = self.call(file_a="Winch", file_b=ID_A, project="Buggy")
        self.assertTrue(is_error)
        self.assertIn("same file", report["problems"][0])

    def test_drawing_is_refused_before_opening(self):
        self.file_a.fileExtension = "f2d"
        report, is_error = self.call()
        self.assertTrue(is_error)
        self.assertIn(".f2d", report["problems"][0])
        self.assertEqual(self.app.events, [])

    def test_assembly_extension_is_accepted(self):
        self.file_a.fileExtension = "f3z"
        self.assertFalse(self.call()[1])


if __name__ == "__main__":
    unittest.main()
