"""document_versions / restore_version: schema, validation, pure decision
logic, and the restore sequence against fake Fusion objects."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import document_versions as dv
from fusion_bridge import tool_surface


def _text(result):
    return result["content"][0]["text"]


def _schema(name):
    return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == name)["inputSchema"]


# -- Fakes --------------------------------------------------------------------


class FakeUser:
    def __init__(self, display_name):
        self.displayName = display_name
        self.userName = display_name.lower().replace(" ", ".")


class FakeMilestone:
    def __init__(self, name):
        self.name = name


class FakeCloud:
    """Server-side version list for one lineage, shared by every DataFile view."""

    def __init__(self, count, lineage="urn:adsk.wipprod:dm.lineage:ABC", read_only=False):
        self.lineage = lineage
        self.read_only = read_only
        self.promote_ok = True
        self.promoted = []
        self.versions = [FakeDataFile(self, n) for n in range(1, count + 1)]

    @property
    def latest(self):
        return self.versions[-1]

    def promote(self, data_file):
        self.promoted.append(data_file.versionNumber)
        if not self.promote_ok:
            return False
        self.versions.append(FakeDataFile(self, len(self.versions) + 1, description=f"Promoted from v{data_file.versionNumber}"))
        return True


class FakeDataFile:
    def __init__(self, cloud, number, description="", milestone=None):
        self._cloud = cloud
        self.versionNumber = number
        self.dateCreated = 1_700_000_000 + number * 3600
        self.description = description or f"save {number}"
        self.isMilestone = milestone is not None
        self.milestone = FakeMilestone(milestone) if milestone else None
        self.createdBy = FakeUser("Ryan Hartman")
        self.lastUpdatedBy = None
        self.id = cloud.lineage
        self.versionId = f"urn:adsk.wipprod:fs.file:vf.ABC?version={number}"

    @property
    def isReadOnly(self):
        return self._cloud.read_only

    @property
    def latestVersionNumber(self):
        return self._cloud.latest.versionNumber

    @property
    def latestVersion(self):
        return self._cloud.latest

    @property
    def versions(self):
        # "the other versions": deliberately excludes self to exercise the merge.
        return [v for v in self._cloud.versions if v is not self]

    def promote(self):
        return self._cloud.promote(self)


class FakeDocument:
    def __init__(self, app, name, data_file=None, modified=False, visible=True, links=()):
        self._app = app
        self.name = name
        self.dataFile = data_file
        self.isSaved = data_file is not None
        self.isModified = modified
        self.isVisible = visible
        self.links = list(links)
        self.closed_with = None
        self.close_ok = True

    @property
    def isActive(self):
        return self._app.activeDocument is self

    def close(self, save_changes):
        self.closed_with = save_changes
        if not self.close_ok:
            return False
        for doc in [self] + self.links:
            if doc in self._app.documents:
                self._app.documents.remove(doc)
        if self._app.activeDocument is self:
            self._app.activeDocument = self._app.documents[0] if self._app.documents else None
        return True

    def activate(self):
        self._app.activeDocument = self
        return True


class FakeDocuments(list):
    def __init__(self, app):
        super().__init__()
        self._app = app
        self.opened = []
        self.open_ok = True

    def open(self, data_file, visible=True):
        self.opened.append(data_file)
        if not self.open_ok:
            return None
        doc = FakeDocument(self._app, "Bracket", data_file)
        self.append(doc)
        return doc


class FakeData:
    def __init__(self, clouds):
        self._clouds = clouds
        self.lookups = []

    def findFileById(self, file_id):
        self.lookups.append(file_id)
        if ":fs.file:" in file_id:  # seen live: the version-file form is refused
            raise RuntimeError("1 : Invalid Version URI")
        cloud = self._clouds.get(file_id)
        return cloud.latest if cloud else None


class FakeApp:
    def __init__(self, cloud=None, modified=False):
        self.documents = FakeDocuments(self)
        self.data = FakeData({cloud.lineage: cloud} if cloud else {})
        self.activeDocument = None
        if cloud is not None:
            doc = FakeDocument(self, "Bracket", cloud.latest, modified=modified)
            self.documents.append(doc)
            self.activeDocument = doc


# -- Schema -------------------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def test_document_versions_schema(self):
        schema = _schema("document_versions")
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required", schema)
        self.assertEqual(schema["properties"]["limit"]["type"], "integer")

    def test_restore_version_schema(self):
        schema = _schema("restore_version")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"version_number", "confirm"})
        self.assertEqual(schema["properties"]["discard_unsaved"]["type"], "boolean")


# -- Pure logic ---------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    def test_list_default_limit(self):
        self.assertEqual(dv.validate_list_args({}), dv.DEFAULT_LIMIT)

    def test_list_rejects_bad_limits(self):
        for bad in (0, -1, 1.5, "5", True, None, dv.MAX_LIMIT + 1):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    dv.validate_list_args({"limit": bad})

    def test_list_rejects_unknown_and_non_dict(self):
        with self.assertRaises(ValueError):
            dv.validate_list_args({"lim": 3})
        with self.assertRaises(ValueError):
            dv.validate_list_args([])

    def test_restore_requires_version_number(self):
        with self.assertRaisesRegex(ValueError, "version_number"):
            dv.validate_restore_args({"confirm": True})

    def test_restore_rejects_bad_values(self):
        cases = [
            {"version_number": 0, "confirm": True},
            {"version_number": True, "confirm": True},
            {"version_number": "3", "confirm": True},
            {"version_number": 2.0, "confirm": True},
            {"version_number": 2, "confirm": "yes"},
            {"version_number": 2, "confirm": True, "discard_unsaved": 1},
            {"version_number": 2, "confirm": True, "force": True},
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    dv.validate_restore_args(args)

    def test_restore_defaults(self):
        self.assertEqual(dv.validate_restore_args({"version_number": 3}), (3, False, False))


class HelperTests(unittest.TestCase):
    def test_iso_date(self):
        self.assertEqual(dv.iso_utc(0), None)
        self.assertEqual(dv.iso_utc(None), None)
        self.assertEqual(dv.iso_utc(True), None)
        self.assertEqual(dv.iso_utc(1_700_000_000), "2023-11-14T22:13:20Z")
        self.assertIsNone(dv.iso_utc(1e20))

    def test_closed_documents_is_multiset_difference(self):
        self.assertEqual(dv.closed_documents(["A", "B", "B", "C"], ["B", "C"]), ["A", "B"])
        self.assertEqual(dv.closed_documents([], ["X"]), [])


class FormatTests(unittest.TestCase):
    def _records(self, n):
        return [{"version_number": i, "date_created": f"d{i}", "description": f"s{i}" if i % 2 else "",
                 "milestone": "Release" if i == 2 else None, "created_by": "Ryan"} for i in range(1, n + 1)]

    def test_newest_first_with_marks(self):
        payload = dv.format_version_list("Bracket", self._records(4), 4, 4, 20)
        self.assertEqual([v["version_number"] for v in payload["versions"]], [4, 3, 2, 1])
        self.assertTrue(payload["versions"][0]["is_current"])
        self.assertTrue(payload["versions"][0]["is_latest"])
        self.assertFalse(any(v["is_current"] for v in payload["versions"][1:]))
        self.assertEqual(payload["versions"][2]["milestone"], "Release")
        self.assertNotIn("description", payload["versions"][0])  # empty description omitted
        self.assertNotIn("truncated", payload)
        self.assertNotIn("warnings", payload)

    def test_limit_and_truncation(self):
        payload = dv.format_version_list("Bracket", self._records(5), 5, 5, 2)
        self.assertEqual([v["version_number"] for v in payload["versions"]], [5, 4])
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total_versions"], 5)

    def test_duplicates_and_bad_numbers_dropped(self):
        records = self._records(2) + [{"version_number": 2}, {"version_number": None}, {"version_number": "3"}]
        payload = dv.format_version_list("Bracket", records, 2, 2, 20)
        self.assertEqual([v["version_number"] for v in payload["versions"]], [2, 1])

    def test_warns_when_older_version_loaded(self):
        payload = dv.format_version_list("Bracket", self._records(3), 2, 3, 20)
        self.assertTrue(payload["versions"][1]["is_current"])
        self.assertIn("not the latest", payload["warnings"][0])


class PlanRestoreTests(unittest.TestCase):
    def _state(self, **overrides):
        state = {"document_name": "Bracket", "version_numbers": [1, 2, 3, 4], "loaded_version": 4,
                 "latest_version": 4, "is_modified": False, "is_read_only": False, "other_modified": []}
        state.update(overrides)
        return state

    def test_unknown_version(self):
        refusal, plan = dv.plan_restore(self._state(), 9, True, False)
        self.assertIn("does not exist", refusal)
        self.assertIn("4, 3, 2, 1", refusal)
        self.assertIsNone(plan)

    def test_latest_version_is_noop(self):
        refusal, _ = dv.plan_restore(self._state(), 4, True, False)
        self.assertIn("already the latest", refusal)

    def test_read_only_refused(self):
        refusal, _ = dv.plan_restore(self._state(is_read_only=True), 2, True, True)
        self.assertIn("read-only", refusal)

    def test_unsaved_changes_refused(self):
        refusal, plan = dv.plan_restore(self._state(is_modified=True), 2, True, False)
        self.assertIn("unsaved changes", refusal)
        self.assertIn("discard_unsaved", refusal)
        self.assertIsNotNone(plan)

    def test_other_modified_documents_refused(self):
        refusal, _ = dv.plan_restore(self._state(other_modified=["Frame"]), 2, True, False)
        self.assertIn("Frame", refusal)

    def test_unsaved_checked_before_confirm(self):
        refusal, _ = dv.plan_restore(self._state(is_modified=True), 2, False, False)
        self.assertIn("unsaved changes", refusal)

    def test_confirm_false_describes_plan(self):
        refusal, plan = dv.plan_restore(self._state(), 2, False, False)
        self.assertIn("'confirm' must be true", refusal)
        self.assertIn("Promote version 2", refusal)
        self.assertIn("v5", refusal)
        self.assertEqual(plan["expected_new_version"], 5)

    def test_discard_unsaved_allows_and_warns(self):
        refusal, plan = dv.plan_restore(self._state(is_modified=True), 2, True, True)
        self.assertIsNone(refusal)
        self.assertIn("DISCARDING", plan["steps"][1])

    def test_ok(self):
        refusal, plan = dv.plan_restore(self._state(), 1, True, False)
        self.assertIsNone(refusal)
        self.assertEqual(plan["restore_version"], 1)


# -- Handlers against fakes -----------------------------------------------------


class DocumentVersionsHandlerTests(unittest.TestCase):
    def _run(self, app, args=None):
        with mock.patch.object(dv, "get_app", return_value=app):
            return dv.document_versions(args or {})

    def test_lists_all_versions_including_self(self):
        cloud = FakeCloud(3)
        cloud.versions[1].isMilestone = True
        cloud.versions[1].milestone = FakeMilestone("Fit check")
        result = self._run(FakeApp(cloud))
        self.assertFalse(result["isError"])
        payload = json.loads(_text(result))
        self.assertEqual([v["version_number"] for v in payload["versions"]], [3, 2, 1])
        self.assertTrue(payload["versions"][0]["is_current"])
        self.assertEqual(payload["versions"][1]["milestone"], "Fit check")
        self.assertEqual(payload["versions"][0]["created_by"], "Ryan Hartman")
        self.assertTrue(payload["versions"][0]["date_created"].endswith("Z"))

    def test_bad_limit_does_not_touch_fusion(self):
        with mock.patch.object(dv, "get_app", side_effect=AssertionError("touched Fusion")):
            result = dv.document_versions({"limit": 0})
        self.assertTrue(result["isError"])
        self.assertIn("limit", _text(result))

    def test_no_active_document(self):
        result = self._run(FakeApp())
        self.assertTrue(result["isError"])
        self.assertIn("No active document", _text(result))

    def test_unsaved_document(self):
        app = FakeApp()
        app.activeDocument = FakeDocument(app, "Untitled")
        result = self._run(app)
        self.assertTrue(result["isError"])
        self.assertIn("never been saved", _text(result))


class RestoreHandlerTests(unittest.TestCase):
    def _run(self, app, args):
        with mock.patch.object(dv, "get_app", return_value=app):
            return dv.restore_version(args)

    def test_bad_args_do_not_touch_fusion(self):
        with mock.patch.object(dv, "get_app", side_effect=AssertionError("touched Fusion")):
            result = dv.restore_version({"version_number": -1, "confirm": True})
        self.assertTrue(result["isError"])

    def test_confirm_false_changes_nothing(self):
        cloud = FakeCloud(4)
        app = FakeApp(cloud)
        result = self._run(app, {"version_number": 2, "confirm": False})
        self.assertTrue(result["isError"])
        self.assertIn("confirm", _text(result))
        self.assertEqual(cloud.promoted, [])
        self.assertIsNone(app.activeDocument.closed_with)

    def test_unsaved_refused_without_promote(self):
        cloud = FakeCloud(4)
        app = FakeApp(cloud, modified=True)
        result = self._run(app, {"version_number": 2, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn("unsaved changes", _text(result))
        self.assertEqual(cloud.promoted, [])

    def test_restore_promotes_closes_and_reopens(self):
        cloud = FakeCloud(4)
        app = FakeApp(cloud)
        original = app.activeDocument
        linked = FakeDocument(app, "Bolt")
        other = FakeDocument(app, "Unrelated")
        original.links.append(linked)
        app.documents.extend([linked, other])

        result = self._run(app, {"version_number": 2, "confirm": True})

        self.assertFalse(result["isError"], _text(result))
        payload = json.loads(_text(result))
        self.assertEqual(cloud.promoted, [2])
        self.assertIs(original.closed_with, False)
        self.assertEqual(app.data.lookups, [cloud.lineage])
        self.assertIsNot(app.activeDocument, original)
        self.assertIs(app.activeDocument.dataFile, cloud.latest)
        self.assertEqual(payload["new_version"], 5)
        self.assertEqual(payload["previous_latest_version"], 4)
        self.assertEqual(payload["restored_from_version"], 2)
        self.assertTrue(payload["is_active"])
        self.assertEqual(payload["also_closed"], ["Bolt"])
        self.assertNotIn("warning", payload)

    def test_discard_unsaved_proceeds(self):
        cloud = FakeCloud(3)
        app = FakeApp(cloud, modified=True)
        result = self._run(app, {"version_number": 1, "confirm": True, "discard_unsaved": True})
        self.assertFalse(result["isError"], _text(result))
        self.assertEqual(json.loads(_text(result))["new_version"], 4)

    def test_promote_failure_reports_nothing_changed(self):
        cloud = FakeCloud(3)
        cloud.promote_ok = False
        app = FakeApp(cloud)
        result = self._run(app, {"version_number": 1, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn("Nothing was changed", _text(result))
        self.assertIsNone(app.activeDocument.closed_with)

    def test_close_failure_reports_promoted(self):
        cloud = FakeCloud(3)
        app = FakeApp(cloud)
        app.activeDocument.close_ok = False
        result = self._run(app, {"version_number": 1, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn("was promoted", _text(result))
        self.assertEqual(app.documents.opened, [])

    def test_reopen_failure_reports_promoted(self):
        cloud = FakeCloud(3)
        app = FakeApp(cloud)
        app.documents.open_ok = False
        result = self._run(app, {"version_number": 1, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn("Data Panel", _text(result))

    def test_exception_after_promote_says_so(self):
        cloud = FakeCloud(3)
        app = FakeApp(cloud)
        app.data.findFileById = mock.Mock(side_effect=RuntimeError("network"))
        result = self._run(app, {"version_number": 1, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn("WAS promoted", _text(result))

    def test_read_only_refused(self):
        cloud = FakeCloud(3, read_only=True)
        result = self._run(FakeApp(cloud), {"version_number": 1, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn("read-only", _text(result))
        self.assertEqual(cloud.promoted, [])


if __name__ == "__main__":
    unittest.main()
