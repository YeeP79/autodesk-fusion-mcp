"""editable_files: schema, argument validation, walk, summary and hints (fakes, no Fusion)."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import editable_files as ef
from fusion_bridge import tool_surface

DAY = 86400.0
NOW = 1_800_000_000.0


class Coll:
    """Fusion-style collection: count + item(i)."""

    def __init__(self, items):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


class FakeFile:
    def __init__(self, name, file_id=None, ext="f3d", read_only=True, version=1,
                 created=NOW - 30 * DAY, modified=None, parents=(), children=(), project=None):
        self.name = name
        self.id = file_id or f"id:{name}"
        self.fileExtension = ext
        self.isReadOnly = read_only
        self.versionNumber = version
        self.dateCreated = created
        self.dateModified = modified if modified is not None else created
        self.hasParentReferences = bool(parents)
        self.hasChildReferences = bool(children)
        self.parentReferences = Coll(parents)
        self.childReferences = Coll(children)
        self.parentProject = project


class FakeFolder:
    def __init__(self, name, files=(), folders=()):
        self.name = name
        self.dataFiles = Coll(files)
        self.dataFolders = Coll(folders)


class FakeProject:
    def __init__(self, name, root):
        self.name = name
        self.rootFolder = root


class FakeDoc:
    def __init__(self, data_file, modified=False, active=False):
        self.dataFile = data_file
        self.isModified = modified
        self.isActive = active


class FakeLimits:
    def __init__(self, files, max_count=10):
        self.editableFiles = list(files)
        self.editableFileCount = len(files)
        self.maxEditableFileCount = max_count


def _options(**overrides):
    opts, err = ef.validate_arguments(overrides)
    assert err is None, err
    return opts


class SchemaTests(unittest.TestCase):
    def _tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "editable_files")

    def test_strict_schema_no_required(self):
        schema = self._tool()["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required", schema)
        self.assertEqual(set(schema["properties"]),
                         {"project", "editable_limit", "max_files", "max_depth", "stale_days", "include_read_only"})

    def test_description_says_read_only(self):
        self.assertIn("Never changes anything", self._tool()["description"])


class ValidationTests(unittest.TestCase):
    def test_defaults(self):
        opts = _options()
        self.assertEqual(opts["max_files"], 500)
        self.assertEqual(opts["max_depth"], 8)
        self.assertEqual(opts["stale_days"], 14)
        self.assertIsNone(opts["editable_limit"])
        self.assertIsNone(opts["project"])
        self.assertFalse(opts["include_read_only"])

    def test_rejects_bad_values(self):
        cases = {
            "project": ["", "  ", 5],
            "editable_limit": [0, -1, 2.5, True, "10"],
            "max_files": [0, 20001, 1.5, False],
            "max_depth": [-1, 65, "3"],
            "stale_days": [-0.1, "14", True],
            "include_read_only": ["yes", 1],
        }
        for key, values in cases.items():
            for bad in values:
                with self.subTest(key=key, value=bad):
                    result = ef.editable_files({key: bad})
                    self.assertTrue(result["isError"])
                    self.assertIn(key, result["content"][0]["text"])

    def test_accepts_float_stale_days_and_strips_project(self):
        opts = _options(stale_days=0.5, project=" Shop ")
        self.assertEqual(opts["stale_days"], 0.5)
        self.assertEqual(opts["project"], "Shop")


class ProjectSelectionTests(unittest.TestCase):
    def test_exact_match_beats_substring(self):
        projects = [FakeProject("Cart", None), FakeProject("Cart Parts", None)]
        self.assertEqual([p.name for p in ef.select_projects(projects, "cart")], ["Cart"])

    def test_substring_case_insensitive(self):
        projects = [FakeProject("Go Kart", None), FakeProject("Shop", None)]
        self.assertEqual([p.name for p in ef.select_projects(projects, "KART")], ["Go Kart"])

    def test_no_filter_returns_all(self):
        projects = [FakeProject("A", None), FakeProject("B", None)]
        self.assertEqual(len(ef.select_projects(projects, None)), 2)


class WalkTests(unittest.TestCase):
    def _tree(self):
        deep = FakeFolder("L3", files=[FakeFile("deep")])
        l2 = FakeFolder("L2", files=[FakeFile("mid")], folders=[deep])
        l1 = FakeFolder("L1", files=[FakeFile("drawing", ext="f2d"), FakeFile("top")], folders=[l2])
        root = FakeFolder("root", files=[FakeFile("rootfile", read_only=False, version=7)], folders=[l1])
        return FakeProject("Cart", root)

    def test_paths_versions_and_designs_only(self):
        entries, info = ef.walk_projects([self._tree()], max_depth=8, max_files=500)
        self.assertEqual([e["path"] for e in entries],
                         ["Cart/rootfile", "Cart/L1/top", "Cart/L1/L2/mid", "Cart/L1/L2/L3/deep"])
        self.assertEqual(entries[0]["version"], 7)
        self.assertFalse(entries[0]["is_read_only"])
        self.assertEqual(info["non_design_files"], 1)
        self.assertEqual(info["folders_visited"], 4)
        self.assertFalse(info["truncated"])
        self.assertEqual(info["projects_walked"], ["Cart"])

    def test_depth_cap(self):
        entries, info = ef.walk_projects([self._tree()], max_depth=1, max_files=500)
        self.assertEqual([e["path"] for e in entries], ["Cart/rootfile", "Cart/L1/top"])
        self.assertEqual(info["folders_skipped_depth"], 1)

    def test_depth_zero_is_root_only(self):
        entries, info = ef.walk_projects([self._tree()], max_depth=0, max_files=500)
        self.assertEqual([e["name"] for e in entries], ["rootfile"])
        self.assertEqual(info["folders_skipped_depth"], 1)

    def test_file_cap_truncates_and_stops_other_projects(self):
        other = FakeProject("Other", FakeFolder("root", files=[FakeFile("x")]))
        entries, info = ef.walk_projects([self._tree(), other], max_depth=8, max_files=2)
        self.assertEqual(len(entries), 2)
        self.assertTrue(info["truncated"])
        self.assertEqual(info["projects_walked"], ["Cart"])

    def test_exact_cap_not_truncated(self):
        project = FakeProject("P", FakeFolder("root", files=[FakeFile("a"), FakeFile("b")]))
        entries, info = ef.walk_projects([project], max_depth=8, max_files=2)
        self.assertEqual(len(entries), 2)
        self.assertFalse(info["truncated"])

    def test_missing_root_and_unreadable_items(self):
        class Broken:
            count = 2

            def item(self, i):
                if i == 0:
                    raise RuntimeError("cloud")
                return FakeFile("ok")

        folder = FakeFolder("root")
        folder.dataFiles = Broken()
        entries, info = ef.walk_projects([FakeProject("NoRoot", None), FakeProject("P", folder)], 8, 500)
        self.assertEqual([e["name"] for e in entries], ["ok"])
        self.assertIn("NoRoot: no root folder", info["errors"])

    def test_unreadable_read_only_flag_defaults_to_read_only(self):
        class NoFlag(FakeFile):
            @property
            def isReadOnly(self):
                raise RuntimeError("cloud")

            @isReadOnly.setter
            def isReadOnly(self, value):
                pass

        project = FakeProject("P", FakeFolder("root", files=[NoFlag("x")]))
        entries, _ = ef.walk_projects([project], 8, 500)
        self.assertTrue(entries[0]["is_read_only"])


class HintTests(unittest.TestCase):
    def _entry(self, data_file):
        return ef.file_entry(data_file, ["P"])

    def test_stale_and_closed_is_likely_safe(self):
        hints = ef.release_hints(self._entry(FakeFile("a", created=NOW - 30 * DAY)), {}, NOW, 14)
        self.assertTrue(hints["likely_safe"])
        self.assertEqual(hints["days_since_change"], 30.0)
        self.assertEqual(hints["blockers"], [])

    def test_recent_change_blocks(self):
        hints = ef.release_hints(self._entry(FakeFile("a", created=NOW - 2 * DAY)), {}, NOW, 14)
        self.assertFalse(hints["likely_safe"])
        self.assertIn("changed 2.0 days ago", hints["blockers"][0])

    def test_stale_boundary_is_stale(self):
        hints = ef.release_hints(self._entry(FakeFile("a", created=NOW - 14 * DAY)), {}, NOW, 14)
        self.assertTrue(hints["likely_safe"])

    def test_rename_date_counts_as_change(self):
        f = FakeFile("a", created=NOW - 60 * DAY, modified=NOW - DAY)
        self.assertFalse(ef.release_hints(self._entry(f), {}, NOW, 14)["likely_safe"])

    def test_unknown_dates(self):
        f = FakeFile("a")
        f.dateCreated = None
        f.dateModified = None
        hints = ef.release_hints(self._entry(f), {}, NOW, 14)
        self.assertIsNone(hints["days_since_change"])
        self.assertIn("last change date unknown", hints["reasons"])

    def test_open_and_unsaved_block(self):
        f = FakeFile("a")
        open_hints = ef.release_hints(self._entry(f), {f.id: {"modified": False, "active": True}}, NOW, 14)
        self.assertFalse(open_hints["likely_safe"])
        self.assertIn("active document", open_hints["blockers"][0])
        dirty = ef.release_hints(self._entry(f), {f.id: {"modified": True, "active": False}}, NOW, 14)
        self.assertIn("unsaved changes", dirty["blockers"][0])

    def test_parent_and_child_references(self):
        parents = [FakeFile(f"asm{i}") for i in range(12)]
        f = FakeFile("bracket", parents=parents, children=[FakeFile("bolt")])
        hints = ef.release_hints(self._entry(f), {}, NOW, 14)
        self.assertEqual(len(hints["parent_references"]), ef.MAX_REFERENCE_NAMES)
        self.assertEqual(hints["parent_references_truncated"], 12)
        self.assertEqual(hints["child_reference_count"], 1)
        self.assertTrue(any("linked into 12" in r for r in hints["reasons"]))
        self.assertTrue(hints["likely_safe"])  # links are informational, not blockers

    def test_open_file_states(self):
        f = FakeFile("a")
        unsaved = FakeDoc(None)
        states = ef.open_file_states(Coll([FakeDoc(f, modified=True, active=True), unsaved]))
        self.assertEqual(states, {f.id: {"modified": True, "active": True}})


class SummaryTests(unittest.TestCase):
    def _walk(self, files):
        project = FakeProject("P", FakeFolder("root", files=files))
        return ef.walk_projects([project], 8, 500)

    def test_personal_use_counts_and_limit(self):
        a = FakeFile("a", read_only=False)
        b = FakeFile("b")
        entries, info = self._walk([a, b])
        pu = ef.personal_use_summary(FakeLimits([a], max_count=10))
        report = ef.summarize(entries, info, pu, {}, _options(), NOW)
        self.assertEqual(report["license"], "personal_use")
        self.assertEqual(report["editable_count"], 1)
        self.assertEqual(report["editable_limit"], 10)
        self.assertEqual(report["limit_source"], "personalUseLimits.maxEditableFileCount")
        self.assertEqual(report["slots_free"], 9)
        self.assertEqual([r["path"] for r in report["editable_files"]], ["P/a"])
        self.assertTrue(report["editable_files"][0]["in_personal_use_list"])
        self.assertEqual(report["read_only_files_scanned"], 1)
        self.assertNotIn("read_only_files", report)
        json.dumps(report)  # no DataFile objects leak into the output

    def test_api_file_outside_walk_is_reported(self):
        outside = FakeFile("elsewhere", project=FakeProject("Other", None))
        entries, info = self._walk([FakeFile("b")])
        report = ef.summarize(entries, info, ef.personal_use_summary(FakeLimits([outside])), {}, _options(), NOW)
        row = report["editable_files"][0]
        self.assertEqual(row["path"], "Other/.../elsewhere")
        self.assertTrue(row["outside_walk"])
        self.assertFalse(row["is_read_only"])

    def test_walk_editable_not_in_api_list_is_flagged(self):
        a = FakeFile("a", read_only=False)
        entries, info = self._walk([a])
        report = ef.summarize(entries, info, ef.personal_use_summary(FakeLimits([])), {}, _options(), NOW)
        self.assertEqual(report["editable_count"], 0)  # licence count is authoritative
        self.assertFalse(report["editable_files"][0]["in_personal_use_list"])

    def test_over_limit_negative_slots_and_argument_override(self):
        files = [FakeFile(f"f{i}", read_only=False) for i in range(4)]
        entries, info = self._walk(files)
        report = ef.summarize(entries, info, None, {}, _options(editable_limit=3), NOW)
        self.assertEqual(report["limit_source"], "argument")
        self.assertEqual(report["slots_free"], -1)
        self.assertIn("not a Personal Use", report["note"])
        self.assertIsNone(report["editable_files"][0]["in_personal_use_list"])

    def test_default_limit_without_licence_info(self):
        entries, info = self._walk([])
        report = ef.summarize(entries, info, None, {}, _options(), NOW)
        self.assertEqual((report["editable_limit"], report["limit_source"]), (10, "default"))
        self.assertEqual(report["editable_files"], [])

    def test_include_read_only_and_truncation_reason(self):
        entries, info = self._walk([FakeFile("ro", version=3)])
        info["truncated"] = True
        report = ef.summarize(entries, info, None, {}, _options(include_read_only=True, max_files=1), NOW)
        self.assertEqual(report["read_only_files"], [{"path": "P/ro", "version": 3}])
        self.assertIn("max_files=1", report["walk"]["truncated_reason"])

    def test_errors_omitted_when_empty(self):
        entries, info = self._walk([])
        self.assertNotIn("errors", ef.summarize(entries, info, None, {}, _options(), NOW)["walk"])


class HandlerTests(unittest.TestCase):
    def _app(self, projects, limits=None, docs=()):
        data = mock.Mock()
        data.dataProjects = Coll(projects)
        data.personalUseLimits = limits
        data.activeHub.name = "Ryan's hub"
        app = mock.Mock()
        app.data = data
        app.documents = Coll(docs)
        return app

    def test_end_to_end_with_fakes(self):
        a = FakeFile("a", read_only=False)
        app = self._app([FakeProject("Cart", FakeFolder("root", files=[a]))], FakeLimits([a]), [FakeDoc(a)])
        with mock.patch.object(ef, "get_app", return_value=app):
            result = ef.editable_files({})
        self.assertFalse(result["isError"])
        report = json.loads(result["content"][0]["text"])
        self.assertEqual(report["hub"], "Ryan's hub")
        self.assertIn("elapsed_seconds", report)
        self.assertFalse(report["editable_files"][0]["safe_to_release_hints"]["likely_safe"])

    def test_unknown_project_lists_available(self):
        app = self._app([FakeProject("Cart", FakeFolder("root"))])
        with mock.patch.object(ef, "get_app", return_value=app):
            result = ef.editable_files({"project": "Boat"})
        self.assertTrue(result["isError"])
        self.assertIn("Cart", result["content"][0]["text"])

    def test_exception_becomes_error(self):
        with mock.patch.object(ef, "get_app", side_effect=RuntimeError("boom")):
            result = ef.editable_files({})
        self.assertTrue(result["isError"])
        self.assertIn("boom", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
