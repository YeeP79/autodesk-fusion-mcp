"""project_tree: argument validation, tree building, path resolution and
duplicate detection over fake Data* objects (no Fusion needed)."""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import data_tree, project_tree, tool_surface


# -- Fakes shaped like DataProject / DataFolder / DataFile -------------------


class FakeCollection:
    def __init__(self, members):
        self._members = list(members)

    def asArray(self):
        return list(self._members)


class FakeProject:
    def __init__(self, name, pid):
        self.name = name
        self.id = pid
        self.rootFolder = FakeFolder(name, f"{pid}-root", project=self, is_root=True)


class FakeFolder:
    def __init__(self, name, fid, parent=None, project=None, is_root=False):
        self.name = name
        self.id = fid
        self.parentFolder = parent
        self.parentProject = project if project is not None else (parent.parentProject if parent else None)
        self.isRoot = is_root
        self._files = []
        self._folders = []

    @property
    def dataFiles(self):
        return FakeCollection(self._files)

    @property
    def dataFolders(self):
        return FakeCollection(self._folders)

    def folder(self, name, fid=None):
        child = FakeFolder(name, fid or f"{self.id}/{name}", parent=self)
        self._folders.append(child)
        return child

    def file(self, name, fid, **kw):
        f = FakeFile(name, fid, self, **kw)
        self._files.append(f)
        return f


class FakeFile:
    def __init__(self, name, fid, folder, version=1, latest=None, ext="f3d", read_only=False, modified=1767225600):
        self.name = name
        self.id = fid
        self.parentFolder = folder
        self.parentProject = folder.parentProject
        self.versionNumber = version
        self.latestVersionNumber = latest if latest is not None else version
        self.fileExtension = ext
        self.isReadOnly = read_only
        self.dateModified = modified
        self.children = []
        self.parents = []
        self.hasOutofDateChildReferences = False

    @property
    def hasChildReferences(self):
        return bool(self.children)

    @property
    def hasParentReferences(self):
        return bool(self.parents)

    @property
    def childReferences(self):
        return FakeCollection(self.children)

    @property
    def parentReferences(self):
        return FakeCollection(self.parents)


def _sample():
    """Project 'Car': root/Chassis/{Front,Rear}, a duplicate 'Tube' and a link out of tree."""
    car = FakeProject("Car", "p1")
    other = FakeProject("Shop", "p2")
    root = car.rootFolder
    chassis = root.folder("Chassis")
    front = chassis.folder("Front")
    rear = chassis.folder("Rear")
    chassis.folder("Empty")
    asm = root.file("Assembly", "f-asm", version=7)
    tube_a = front.file("Tube", "f-tube-a", version=2)
    tube_b = rear.file("Tube", "f-tube-b", version=5, latest=6, read_only=True)
    bracket = other.rootFolder.folder("Parts").file("Bracket", "f-br", version=3)
    asm.children = [tube_a, tube_b, bracket]
    tube_a.parents = [asm]
    tube_b.parents = [asm]
    return car, other, {"asm": asm, "tube_a": tube_a, "tube_b": tube_b}


def _files_by_id(tree, out=None):
    out = {} if out is None else out
    for f in tree.get("files", []):
        out[f["id"]] = f
    for sub in tree.get("folders", []):
        _files_by_id(sub, out)
    return out


# -- Pure helpers ------------------------------------------------------------


class PathHelperTests(unittest.TestCase):
    def test_split_path(self):
        self.assertEqual(data_tree.split_path(None), [])
        self.assertEqual(data_tree.split_path(""), [])
        self.assertEqual(data_tree.split_path("/a//b/ "), ["a", "b"])
        self.assertEqual(data_tree.split_path("a\\b"), ["a", "b"])

    def test_split_path_rejects_dots_and_non_strings(self):
        for bad in ("a/../b", "./a", 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                data_tree.split_path(bad)

    def test_iso_utc(self):
        self.assertEqual(data_tree.iso_utc(1767225600), "2026-01-01T00:00:00Z")
        for bad in (0, None, -1, True, "x"):
            self.assertIsNone(data_tree.iso_utc(bad))

    def test_file_type(self):
        self.assertEqual(data_tree.file_type("f3d"), "Fusion design")
        self.assertEqual(data_tree.file_type("PDF"), "PDF file")
        self.assertEqual(data_tree.file_type(""), "unknown")

    def test_items_falls_back_to_count_and_item(self):
        coll = mock.Mock(spec=["count", "item"], count=2)
        coll.item.side_effect = ["a", "b"]
        self.assertEqual(data_tree.items(coll), ["a", "b"])
        self.assertEqual(data_tree.items(None), [])

    def test_case_collisions(self):
        self.assertEqual(data_tree.case_collisions(["Tubes", "tubes", "Other"]), [["Tubes", "tubes"]])
        self.assertEqual(data_tree.case_collisions(["a", "b"]), [])


class LookupTests(unittest.TestCase):
    def test_find_project_by_name_id_and_case(self):
        car, other, _ = _sample()
        self.assertIs(data_tree.find_project([car, other], "Car"), car)
        self.assertIs(data_tree.find_project([car, other], "p2"), other)
        self.assertIs(data_tree.find_project([car, other], "car"), car)

    def test_find_project_missing_lists_names(self):
        car, other, _ = _sample()
        with self.assertRaisesRegex(LookupError, "'Car', 'Shop'"):
            data_tree.find_project([car, other], "Boat")

    def test_find_project_duplicate_names_demand_id(self):
        a, b = FakeProject("Car", "p1"), FakeProject("Car", "p9")
        with self.assertRaisesRegex(LookupError, "ambiguous.*p1.*p9"):
            data_tree.find_project([a, b], "Car")
        self.assertIs(data_tree.find_project([a, b], "p9"), b)

    def test_resolve_folder_case_insensitive_returns_fusion_names(self):
        car, _, _ = _sample()
        folder, actual = data_tree.resolve_folder(car.rootFolder, ["chassis", "FRONT"])
        self.assertEqual(folder.name, "Front")
        self.assertEqual(actual, ["Chassis", "Front"])

    def test_resolve_folder_missing(self):
        car, _, _ = _sample()
        with self.assertRaisesRegex(LookupError, "No folder 'Nope'.*'Chassis'"):
            data_tree.resolve_folder(car.rootFolder, ["Nope"])

    def test_resolve_folder_case_collision_refuses_to_guess(self):
        car = FakeProject("Car", "p1")
        car.rootFolder.folder("Tubes", "t1")
        car.rootFolder.folder("tubes", "t2")
        with self.assertRaisesRegex(LookupError, "case-insensitive.*t1.*t2"):
            data_tree.resolve_folder(car.rootFolder, ["TUBES"])

    def test_resolve_folder_prefers_unique_exact_match(self):
        car = FakeProject("Car", "p1")
        car.rootFolder.folder("Tubes", "t1")
        folder, _ = data_tree.resolve_folder(car.rootFolder, ["Tubes"])
        self.assertEqual(folder.id, "t1")


class PathResolverTests(unittest.TestCase):
    def test_paths_walk_to_project_name_and_cache(self):
        car, _, files = _sample()
        resolver = data_tree.PathResolver()
        self.assertEqual(resolver.file_path(files["tube_b"]), "Car/Chassis/Rear/Tube")
        self.assertEqual(resolver.file_path(files["asm"]), "Car/Assembly")
        # cached by id: renaming the fake does not change an already-resolved path
        files["tube_b"].parentFolder.name = "Renamed"
        self.assertEqual(resolver.folder_path(files["tube_b"].parentFolder), "Car/Chassis/Rear")

    def test_parent_loop_does_not_hang(self):
        a = FakeFolder("A", "a")
        b = FakeFolder("B", "b", parent=a)
        a.parentFolder = b
        self.assertTrue(data_tree.PathResolver().folder_path(b).endswith("A/B"))


# -- Tree building -----------------------------------------------------------


class BuildTreeTests(unittest.TestCase):
    def _build(self, **kw):
        car, other, files = _sample()
        tree, summary = data_tree.build_tree(car.rootFolder, "Car", **kw)
        return tree, summary, files

    def test_nested_shape_and_file_fields(self):
        tree, summary, _ = self._build()
        self.assertEqual(tree["path"], "Car")
        self.assertEqual([f["name"] for f in tree["folders"]], ["Chassis"])
        chassis = tree["folders"][0]
        self.assertEqual([f["name"] for f in chassis["folders"]], ["Empty", "Front", "Rear"])
        rear_tube = _files_by_id(tree)["f-tube-b"]
        self.assertEqual(rear_tube["path"], "Car/Chassis/Rear/Tube")
        self.assertEqual(rear_tube["version"], 5)
        self.assertEqual(rear_tube["latest_version"], 6)
        self.assertTrue(rear_tube["is_read_only"])
        self.assertEqual(rear_tube["extension"], "f3d")
        self.assertEqual(rear_tube["type"], "Fusion design")
        self.assertEqual(rear_tube["date_modified"], "2026-01-01T00:00:00Z")
        self.assertEqual(summary["files_listed"], 3)
        self.assertEqual(summary["folders_visited"], 5)
        self.assertFalse(summary["truncated"])

    def test_empty_folders_flagged(self):
        tree, summary, _ = self._build()
        empty = tree["folders"][0]["folders"][0]
        self.assertTrue(empty["empty"])
        self.assertEqual(summary["empty_folders"], ["Car/Chassis/Empty"])

    def test_duplicate_names_distinguishable(self):
        tree, summary, _ = self._build()
        self.assertEqual(len(summary["duplicate_names"]), 1)
        dup = summary["duplicate_names"][0]
        self.assertEqual(dup["name"], "Tube")
        self.assertEqual({(c["path"], c["version"], c["id"]) for c in dup["copies"]},
                         {("Car/Chassis/Front/Tube", 2, "f-tube-a"), ("Car/Chassis/Rear/Tube", 5, "f-tube-b")})
        by_id = _files_by_id(tree)
        self.assertTrue(by_id["f-tube-a"]["duplicate_name"])
        self.assertNotIn("duplicate_name", by_id["f-asm"])

    def test_duplicate_detection_is_case_insensitive(self):
        nodes = [{"name": "Tube", "path": "P/Tube", "version": 1, "id": "1"},
                 {"name": "tube", "path": "P/x/tube", "version": 1, "id": "2"}]
        self.assertEqual(len(data_tree.duplicate_names(nodes)), 1)

    def test_links_resolved_to_paths_by_id(self):
        tree, _, _ = self._build()
        by_id = _files_by_id(tree)
        uses = by_id["f-asm"]["uses"]
        self.assertEqual([(u["path"], u["id"], u["in_tree"]) for u in uses], [
            ("Car/Chassis/Front/Tube", "f-tube-a", True),
            ("Car/Chassis/Rear/Tube", "f-tube-b", True),
            ("Shop/Parts/Bracket", "f-br", False),
        ])
        self.assertEqual(uses[1]["latest_version"], 6)
        self.assertEqual(by_id["f-tube-a"]["used_by"][0]["path"], "Car/Assembly")
        self.assertNotIn("uses", by_id["f-tube-a"])

    def test_links_skipped_when_disabled(self):
        tree, _, _ = self._build(include_links=False)
        self.assertNotIn("uses", _files_by_id(tree)["f-asm"])

    def test_link_error_is_isolated(self):
        car, _, files = _sample()
        broken = mock.PropertyMock(side_effect=RuntimeError("cloud hiccup"))
        with mock.patch.object(FakeFile, "childReferences", broken):
            tree, summary = data_tree.build_tree(car.rootFolder, "Car")
        self.assertIn("cloud hiccup", _files_by_id(tree)["f-asm"]["links_error"])
        self.assertEqual(summary["files_with_link_errors"], 1)
        self.assertEqual(summary["files_listed"], 3)

    def test_depth_limit_reports_unlisted_subfolders(self):
        tree, summary, _ = self._build(depth=1)
        chassis = tree["folders"][0]
        self.assertNotIn("folders", chassis)
        self.assertEqual(chassis["subfolders_not_listed"], 3)
        self.assertEqual(summary["depth_limited_folders"], ["Car/Chassis"])
        self.assertFalse(summary["truncated"])

    def test_depth_zero_lists_only_start_folder(self):
        tree, summary, _ = self._build(depth=0)
        self.assertEqual([f["name"] for f in tree["files"]], ["Assembly"])
        self.assertEqual(tree["subfolders_not_listed"], 1)

    def test_max_files_truncates(self):
        tree, summary, _ = self._build(max_files=2)
        self.assertEqual(summary["files_listed"], 2)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["truncation"]["reason"], "max_files")

    def test_time_budget_truncates(self):
        ticks = iter([0.0] + [100.0] * 50)
        tree, summary, _ = self._build(budget_s=10, clock=lambda: next(ticks))
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["truncation"]["reason"], "time_budget")
        self.assertTrue(tree["not_listed"])

    def test_name_filter_prunes_folders_without_matches(self):
        tree, summary, _ = self._build(name_contains="tube")
        self.assertNotIn("files", tree)
        chassis = tree["folders"][0]
        self.assertEqual([f["name"] for f in chassis["folders"]], ["Front", "Rear"])
        self.assertEqual(summary["files_listed"], 2)
        self.assertEqual(summary["folders_pruned_by_filter"], 1)
        self.assertEqual(summary["empty_folders"], ["Car/Chassis/Empty"])

    def test_folder_case_collisions_reported(self):
        car = FakeProject("Car", "p1")
        car.rootFolder.folder("Tubes", "t1")
        car.rootFolder.folder("tubes", "t2")
        _, summary = data_tree.build_tree(car.rootFolder, "Car")
        self.assertEqual(summary["folder_case_collisions"], [{"parent": "Car", "names": ["Tubes", "tubes"]}])

    def test_same_name_files_in_one_folder_sorted_stably_by_id(self):
        car = FakeProject("Car", "p1")
        car.rootFolder.file("Part", "z")
        car.rootFolder.file("Part", "a")
        tree, summary = data_tree.build_tree(car.rootFolder, "Car")
        self.assertEqual([f["id"] for f in tree["files"]], ["a", "z"])
        self.assertEqual(len(summary["duplicate_names"][0]["copies"]), 2)


# -- Handler -----------------------------------------------------------------


class ProjectTreeHandlerTests(unittest.TestCase):
    def _app(self, projects):
        hub = mock.Mock(id="hub1")
        hub.name = "My Hub"
        data = mock.Mock(activeHub=hub, dataProjects=FakeCollection(projects))
        return mock.Mock(data=data)

    def _call(self, arguments, projects=None):
        if projects is None:
            car, other, _ = _sample()
            projects = [car, other]
        with mock.patch.object(project_tree, "get_app", return_value=self._app(projects)), \
                mock.patch.object(project_tree, "log"):
            return project_tree.project_tree(arguments)

    def test_validation_errors(self):
        cases = [
            ({}, "'project' is required"),
            ({"project": ""}, "project"),
            ({"project": "Car", "depth": -1}, "depth"),
            ({"project": "Car", "depth": True}, "depth"),
            ({"project": "Car", "max_files": 0}, "max_files"),
            ({"project": "Car", "include_links": "yes"}, "include_links"),
            ({"project": "Car", "name_contains": ""}, "name_contains"),
            ({"project": "Car", "folder": "a/../b"}, "folder"),
            ({"list_projects": "true"}, "list_projects"),
            ({"project": "Car", "bogus": 1}, "bogus"),
        ]
        for arguments, needle in cases:
            with self.subTest(arguments=arguments):
                with mock.patch.object(project_tree, "get_app") as get_app:
                    result = project_tree.project_tree(arguments)
                    get_app.assert_not_called()
                self.assertTrue(result["isError"])
                self.assertIn(needle, result["content"][0]["text"])

    def test_list_projects(self):
        result = self._call({"list_projects": True}, [FakeProject("b", "2"), FakeProject("A", "1"), FakeProject("a", "3")])
        payload = json.loads(result["content"][0]["text"])
        self.assertFalse(result["isError"])
        self.assertEqual([p["id"] for p in payload["projects"]], ["1", "3", "2"])
        self.assertEqual(payload["duplicate_project_names"], [["A", "a"]])
        self.assertEqual(payload["hub"], {"name": "My Hub", "id": "hub1"})

    def test_tree_from_subfolder(self):
        result = self._call({"project": "car", "folder": "chassis/rear"})
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["root"], "Car/Chassis/Rear")
        self.assertEqual(payload["project"], {"name": "Car", "id": "p1"})
        self.assertEqual(payload["tree"]["files"][0]["used_by"][0]["path"], "Car/Assembly")
        self.assertIn("elapsed_seconds", payload["summary"])
        self.assertTrue(payload["notes"])

    def test_unknown_project_and_folder(self):
        self.assertIn("No project named", self._call({"project": "Boat"})["content"][0]["text"])
        self.assertIn("No folder", self._call({"project": "Car", "folder": "Nope"})["content"][0]["text"])

    def test_time_budget_below_dispatch_timeout(self):
        from fusion_bridge import dispatch
        self.assertLess(project_tree.time_budget(), dispatch.settings.MCP_MAIN_THREAD_TIMEOUT)


class ProjectTreeSchemaTests(unittest.TestCase):
    def _schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "project_tree")["inputSchema"]

    def test_schema_properties_match_handler(self):
        schema = self._schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required", schema)
        self.assertEqual(set(schema["properties"]), project_tree._KNOWN_ARGS)

    def test_schema_limits_match_handler(self):
        props = self._schema()["properties"]
        self.assertEqual(props["depth"]["maximum"], data_tree.MAX_DEPTH)
        self.assertEqual(props["max_files"]["maximum"], project_tree.MAX_FILES_LIMIT)


if __name__ == "__main__":
    unittest.main()
