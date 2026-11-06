"""document_status / activate_document and the read-only guard, against fakes."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import document_guard, documents, python_exec, tool_surface


class Collection:
    def __init__(self, items):
        self._items = list(items)
        self.count = len(self._items)

    def item(self, index):
        return self._items[index]


def folder(name, parent=None, root=False):
    return SimpleNamespace(name=name, parentFolder=parent, isRoot=root)


def data_file(file_id, name, parent_folder, read_only=True, project="Garage"):
    return SimpleNamespace(id=file_id, name=name, versionNumber=3, isReadOnly=read_only,
                           parentFolder=parent_folder, parentProject=SimpleNamespace(name=project))


class FakeDoc:
    def __init__(self, name, df=None, active=False, modified=False, refs=(), app=None):
        self.name = name
        self.dataFile = df
        self.isActive = active
        self.isModified = modified
        self.isSaved = df is not None
        self.objectType = "adsk::fusion::FusionDocument"
        self.products = Collection([SimpleNamespace(productType="DesignProductType")])
        self.documentReferences = Collection([SimpleNamespace(dataFile=r) for r in refs])
        self.app = app
        self.activated = 0

    def activate(self):
        self.activated += 1
        if self.app is not None:
            for doc in self.app.docs:
                doc.isActive = doc is self
            self.app.activeDocument = self
        return True


class FakeApp:
    def __init__(self, docs):
        self.docs = docs
        for doc in docs:
            doc.app = self
        self.documents = Collection(docs)
        self.activeDocument = next((d for d in docs if d.isActive), None)


def body(result):
    return json.loads(result["content"][0]["text"])


ROOT = folder("Garage", root=True)
CHASSIS = folder("Chassis", ROOT)
FRONT = folder("Front", CHASSIS)


class SchemaTests(unittest.TestCase):
    def tool(self, name):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == name)

    def test_document_status_takes_no_arguments(self):
        schema = self.tool("document_status")["inputSchema"]
        self.assertEqual(schema["properties"], {})
        self.assertFalse(schema["additionalProperties"])

    def test_activate_document_schema(self):
        schema = self.tool("activate_document")["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"name", "data_file_id"})
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required", schema)


class FolderPathTests(unittest.TestCase):
    def test_nested_path_excludes_root_name(self):
        self.assertEqual(documents.folder_path(data_file("a", "A", FRONT)), "/Chassis/Front")

    def test_file_in_root(self):
        self.assertEqual(documents.folder_path(data_file("a", "A", ROOT)), "/")

    def test_missing_parent(self):
        self.assertIsNone(documents.folder_path(SimpleNamespace(parentFolder=None)))

    def test_raising_parent_folder(self):
        class Broken:
            @property
            def parentFolder(self):
                raise RuntimeError("offline")
        self.assertIsNone(documents.folder_path(Broken()))

    def test_parent_chain_without_root_flag_stops_at_none(self):
        top = SimpleNamespace(name="Top", parentFolder=None)  # no isRoot attribute
        self.assertEqual(documents.folder_path(data_file("a", "A", folder("Sub", top))), "/Top/Sub")

    def test_cycle_is_bounded(self):
        loop = SimpleNamespace(name="L", isRoot=False)
        loop.parentFolder = loop
        path = documents.folder_path(data_file("a", "A", loop))
        self.assertEqual(path.count("L"), documents._MAX_FOLDER_DEPTH)


class GuardTests(unittest.TestCase):
    def test_decision_table(self):
        cases = {
            (True, True): document_guard.UNSAVED_READ_ONLY_WARNING,
            (True, False): None,
            (False, True): None,
            (None, True): None,
            (None, False): None,
        }
        for (read_only, modified), expected in cases.items():
            with self.subTest(read_only=read_only, modified=modified):
                self.assertEqual(document_guard.unsaved_read_only_warning(read_only, modified), expected)

    def test_read_only_state_unknown_cases(self):
        self.assertIsNone(document_guard.read_only_state(FakeDoc("Unsaved")))
        self.assertIsNone(document_guard.read_only_state(FakeDoc("X", SimpleNamespace(id="1"))))

        class Raising:
            @property
            def isReadOnly(self):
                raise RuntimeError("no")
        self.assertIsNone(document_guard.read_only_state(FakeDoc("X", Raising())))

    def test_read_only_state_reads_flag(self):
        self.assertTrue(document_guard.read_only_state(FakeDoc("X", data_file("1", "X", ROOT, True))))
        self.assertFalse(document_guard.read_only_state(FakeDoc("X", data_file("1", "X", ROOT, False))))

    def test_active_document_warning(self):
        doc = FakeDoc("X", data_file("1", "X", ROOT, True), active=True, modified=True)
        with patch.object(document_guard, "get_app", return_value=FakeApp([doc])):
            self.assertEqual(document_guard.active_document_warning(), document_guard.UNSAVED_READ_ONLY_WARNING)
        with patch.object(document_guard, "get_app", return_value=FakeApp([])):
            self.assertIsNone(document_guard.active_document_warning())
        with patch.object(document_guard, "get_app", side_effect=RuntimeError("boom")):
            self.assertIsNone(document_guard.active_document_warning())

    def test_execute_python_appends_warning(self):
        with patch.object(python_exec, "_build_runtime_globals", return_value={}), \
             patch.object(document_guard, "active_document_warning", return_value="careful"):
            result = python_exec.run_python({"code": "x = 1", "persistent": False})
        self.assertFalse(result["isError"])
        self.assertEqual(body(result)["warnings"], ["careful"])

    def test_execute_python_no_warning_key_when_clean(self):
        with patch.object(python_exec, "_build_runtime_globals", return_value={}), \
             patch.object(document_guard, "active_document_warning", return_value=None):
            result = python_exec.run_python({"code": "x = 1", "persistent": False})
        self.assertNotIn("warnings", body(result))


class DocumentStatusTests(unittest.TestCase):
    def setUp(self):
        self.part = FakeDoc("Tube v3", data_file("f-tube", "Tube", FRONT, read_only=True))
        self.assembly = FakeDoc("Frame v7", data_file("f-frame", "Frame", CHASSIS, read_only=True),
                                active=True, modified=True, refs=[self.part.dataFile])
        self.scratch = FakeDoc("Untitled")
        self.app = FakeApp([self.part, self.assembly, self.scratch])

    def status(self):
        with patch.object(documents, "get_app", return_value=self.app):
            return documents.document_status({})

    def test_reports_every_document(self):
        result = body(self.status())
        self.assertEqual(result["active_document"], "Frame v7")
        self.assertEqual(result["count"], 3)
        tube, frame, scratch = result["documents"]
        self.assertEqual(tube["data_file"]["folder_path"], "/Chassis/Front")
        self.assertEqual(tube["data_file"]["project"], "Garage")
        self.assertEqual(tube["data_file"]["version_number"], 3)
        self.assertEqual(tube["referenced_by"], ["Frame v7"])
        self.assertEqual(frame["referenced_by"], [])
        self.assertTrue(frame["is_active"])
        self.assertEqual(frame["product_types"], ["DesignProductType"])
        self.assertIsNone(scratch["data_file"])
        self.assertFalse(scratch["is_saved"])

    def test_warnings_for_active(self):
        self.assertEqual(body(self.status())["warnings"],
                         [documents.READ_ONLY_WARNING, documents.UNSAVED_WARNING])

    def test_the_active_document_held_by_a_parent_warns(self):
        info = {"name": "Stock", "is_active": True, "data_file": {"is_read_only": False}, "is_modified": False,
                "referenced_by": ["Assembly", "Other"]}
        warning = documents.status_warnings([info])[0]
        self.assertIn("Assembly, Other", warning)
        self.assertIn("on its own", warning)

    def test_an_active_document_nothing_links_does_not_warn(self):
        info = {"name": "Stock", "is_active": True, "data_file": {"is_read_only": False}, "is_modified": False,
                "referenced_by": []}
        self.assertEqual(documents.status_warnings([info]), [])

    def test_unknown_read_only(self):
        self.assembly.dataFile = SimpleNamespace(id="f-frame", name="Frame", parentFolder=ROOT)
        result = body(self.status())
        self.assertEqual(result["documents"][1]["data_file"]["is_read_only"], "unknown")
        self.assertEqual(result["warnings"], [documents.UNSAVED_WARNING])

    def test_no_active_document(self):
        self.assembly.isActive = False
        self.assertEqual(body(self.status())["warnings"], [documents.NO_ACTIVE_WARNING])

    def test_fusion_failure_is_error(self):
        with patch.object(documents, "get_app", side_effect=RuntimeError("down")):
            self.assertTrue(documents.document_status({})["isError"])


class DuplicateInstanceTests(unittest.TestCase):
    def knuckle(self, version, active=False, modified=False):
        df = data_file("f-knuckle", "Knuckle", FRONT, read_only=False)
        df.versionNumber = version
        return FakeDoc("Knuckle v%d" % version, df, active=active, modified=modified)

    def status(self, docs):
        with patch.object(documents, "get_app", return_value=FakeApp(docs)):
            return body(documents.document_status({}))

    def test_single_instances_are_not_flagged(self):
        result = self.status([self.knuckle(12, active=True), FakeDoc("Untitled"), FakeDoc("Untitled")])
        self.assertEqual(result["warnings"], [])
        for info in result["documents"]:
            self.assertNotIn("duplicate_instances", info)

    def test_two_instances_flagged_with_versions(self):
        result = self.status([self.knuckle(12, active=True), self.knuckle(11, modified=True)])
        for info in result["documents"]:
            self.assertEqual(info["duplicate_instances"], 2)
            self.assertEqual(info["loaded_versions"], [12, 11])
        self.assertEqual(len(result["warnings"]), 1)
        warning = result["warnings"][0]
        self.assertIn("2 open instances of 'Knuckle'", warning)
        self.assertIn("f-knuckle", warning)
        self.assertIn("[12, 11]", warning)
        self.assertIn("silently do nothing", warning)
        self.assertIn("without saving", warning)
        self.assertNotIn("link it", warning)

    def test_linked_parent_named(self):
        knuckles = [self.knuckle(12), self.knuckle(11), self.knuckle(12)]
        parent = FakeDoc("Moon Patrol v4", data_file("f-moon", "Moon Patrol", ROOT, read_only=False),
                         active=True, refs=[knuckles[0].dataFile])
        result = self.status(knuckles + [parent])
        self.assertEqual(result["documents"][0]["duplicate_instances"], 3)
        self.assertNotIn("duplicate_instances", result["documents"][3])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("3 open instances", result["warnings"][0])
        self.assertIn("(Moon Patrol v4)", result["warnings"][0])

    def test_duplicate_warning_follows_active_warnings(self):
        result = self.status([self.knuckle(12, active=True, modified=True), self.knuckle(11)])
        self.assertEqual(result["warnings"][0], documents.UNSAVED_WARNING)
        self.assertIn("open instances", result["warnings"][1])

    def test_flag_duplicates_ignores_unsaved_and_missing_ids(self):
        infos = [{"name": "A", "data_file": None}, {"name": "B", "data_file": {"id": None}},
                 {"name": "C", "data_file": {"id": None}}]
        self.assertEqual(documents.flag_duplicates(infos), [])
        self.assertTrue(all("duplicate_instances" not in info for info in infos))


class ActivateDocumentTests(unittest.TestCase):
    def setUp(self):
        self.a = FakeDoc("Frame v7", data_file("f-1", "Frame", CHASSIS), active=True)
        self.b = FakeDoc("Bracket v2", data_file("f-2", "Bracket", FRONT, read_only=False))
        self.c = FakeDoc("Bracket v2", data_file("f-3", "Bracket", CHASSIS))
        self.d = FakeDoc("Tube v1", data_file("f-4", "Tube", ROOT, read_only=True), modified=True)
        self.app = FakeApp([self.a, self.b, self.c, self.d])

    def activate(self, **arguments):
        with patch.object(documents, "get_app", return_value=self.app):
            return documents.activate_document(arguments)

    def test_exactly_one_selector(self):
        for arguments in ({}, {"name": "Frame", "data_file_id": "f-1"}, {"name": None}):
            with self.subTest(arguments=arguments):
                with patch.object(documents, "get_app", side_effect=AssertionError("touched Fusion")):
                    result = documents.activate_document(arguments)
                self.assertTrue(result["isError"])
                self.assertIn("exactly one", result["content"][0]["text"])

    def test_selector_must_be_non_empty_string(self):
        for arguments in ({"name": ""}, {"name": "  "}, {"data_file_id": 7}):
            with self.subTest(arguments=arguments):
                self.assertEqual(documents.validate_selector(arguments)[0], None)

    def test_activate_by_data_file_name(self):
        result = self.activate(name="Tube")
        self.assertFalse(result["isError"])
        payload = body(result)
        self.assertEqual(payload["active_document"]["name"], "Tube v1")
        self.assertFalse(payload["already_active"])
        self.assertEqual(payload["warnings"], [documents.READ_ONLY_WARNING, documents.UNSAVED_WARNING])
        self.assertEqual(self.d.activated, 1)

    def test_activate_by_id_disambiguates(self):
        payload = body(self.activate(data_file_id="f-3"))
        self.assertEqual(payload["active_document"]["data_file"]["id"], "f-3")

    def test_ambiguous_name_lists_candidates(self):
        result = self.activate(name="Bracket v2")
        self.assertTrue(result["isError"])
        candidates = body(result)["candidates"]
        self.assertEqual([c["folder_path"] for c in candidates], ["/Chassis/Front", "/Chassis"])
        self.assertEqual(self.b.activated + self.c.activated, 0)

    def test_duplicate_id_points_at_duplicate_warning(self):
        self.c.dataFile = self.b.dataFile
        result = self.activate(data_file_id="f-2")
        self.assertTrue(result["isError"])
        self.assertIn("duplicate instances", body(result)["error"])

    def test_no_match_lists_open_documents(self):
        result = self.activate(name="Nope")
        self.assertTrue(result["isError"])
        self.assertEqual(len(body(result)["open_documents"]), 4)

    def test_already_active(self):
        payload = body(self.activate(name="Frame v7"))
        self.assertTrue(payload["already_active"])
        self.assertEqual(self.a.activated, 0)

    def test_activation_refused(self):
        self.b.activate = lambda: False
        result = self.activate(data_file_id="f-2")
        self.assertTrue(result["isError"])
        self.assertIn("refused", result["content"][0]["text"])

    def test_activation_without_effect(self):
        self.b.activate = lambda: True  # Fusion said yes, active doc unchanged
        result = self.activate(data_file_id="f-2")
        self.assertTrue(result["isError"])
        self.assertIn("did not take effect", body(result)["error"])


if __name__ == "__main__":
    unittest.main()
