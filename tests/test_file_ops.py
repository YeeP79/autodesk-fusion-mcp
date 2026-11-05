"""move_files / rename_folder / rename_file / remove_empty_folders: planning,
refusals, execution and the link check over fake Data* objects (no Fusion)."""

import json
import os
import tempfile
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

import adsk

from fusion_bridge import file_ops, tool_surface
from fusion_bridge.common import cloud as data_tree
from fusion_bridge.common import app_log

OPAQUE = "2 : InternalValidationError : res"


def log_entry(status, url, code, message, body="[Empty body]", method="POST"):
    """A failed request as Fusion writes it to the app log."""
    response = json.dumps({"errorCode": code, "errorClass": "ApiError", "message": message})
    return (f"20261105T113355 E .24563490 {method} {status} {url} - workflow:WIPRequest2 attempt:0 "
            "requestId:f360_WIPRequest2__1-abc dt=0.34\n"
            "Request Content-Type: application/json\n"
            f"Request Body: {body}\n"
            "Response Content-Type: application/json\n"
            f"Response Body: {response}\n"
            "20261105T113355 I 31442672 PushNotification broadcasting 0811\n")


def lock_entry(token):
    return log_entry(423, f"https://api/storage/v3/entities/urn%3Aadsk.wipprod%3Afs.folder%3A{token}/rename?newName=Sub",
                     "LOCK_RESOURCE_IS_LOCKED", "Resource is locked by other operation.")


class TempLog:
    """A real file for ServerLog, so marks and offsets are exercised."""

    def __enter__(self):
        handle, self.path = tempfile.mkstemp(suffix=".log")
        os.close(handle)
        self.log = app_log.ServerLog(self.path)
        return self

    def __exit__(self, *exc):
        os.remove(self.path)

    def write(self, text):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(text)


class RefusingFolder:
    """Stands in for a FakeFolder whose name setter raises the opaque error a set number of times."""

    def __init__(self, folder, refusals, on_refuse=lambda: None):
        self.folder = folder
        self.refusals = refusals
        self.on_refuse = on_refuse
        self.attempts = []

    def __enter__(self):
        real = FakeFolder.name
        outer = self

        def setter(folder, value):
            if folder is outer.folder:
                outer.attempts.append(value)
                if outer.refusals(value, len(outer.attempts)):
                    outer.on_refuse()
                    raise RuntimeError(OPAQUE)
            real.fset(folder, value)

        self._patch = mock.patch.object(FakeFolder, "name", property(real.fget, setter))
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def fail_with(log, entry):
    log.write(entry)
    raise RuntimeError(OPAQUE)


def attempts_seen(n):
    """True for the first try at the restore: attempt numbers run step 1, 5 tries of step 2, restore."""
    return n == 2 + len(file_ops._LOCK_RETRY_DELAYS_S) + 1


# -- Fakes shaped like DataProject / DataFolder / DataFile, with mutations ----


class FakeCollection:
    def __init__(self, owner, members):
        self._owner = owner
        self._members = members

    def asArray(self):
        return list(self._members)

    def add(self, name):  # DataFolders.add
        if any(f.name.casefold() == name.casefold() for f in self._members):
            raise RuntimeError("CB_NAE: name already exists")
        return self._owner.folder(name)


class FakeProject:
    def __init__(self, name, pid):
        self.name = name
        self.id = pid
        self.rootFolder = FakeFolder(name, f"{pid}-root", project=self, is_root=True)


class FakeFolder:
    def __init__(self, name, fid, parent=None, project=None, is_root=False):
        self._name = name
        self.id = fid
        self.parentFolder = parent
        self.parentProject = project if project is not None else parent.parentProject
        self.isRoot = is_root
        self._files = []
        self._folders = []
        self.deleted = False
        self.name_log = []

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        # Fusion folder names are case-insensitive among siblings.
        if self.parentFolder is not None and any(
                f is not self and f.name.casefold() == value.casefold() for f in self.parentFolder._folders):
            raise RuntimeError("CB_NAE: name already exists")
        self.name_log.append(value)
        self._name = value

    @property
    def dataFiles(self):
        return FakeCollection(self, self._files)

    @property
    def dataFolders(self):
        return FakeCollection(self, self._folders)

    def folder(self, name, fid=None):
        child = FakeFolder(name, fid or f"{self.id}/{name}", parent=self)
        self._folders.append(child)
        return child

    def file(self, name, fid, **kw):
        f = FakeFile(name, fid, self, **kw)
        self._files.append(f)
        return f

    def deleteMe(self):
        self.parentFolder._folders.remove(self)
        self.deleted = True
        return True


class FakeFile:
    def __init__(self, name, fid, folder, version=1, ext="f3d"):
        self.name = name
        self.id = fid
        self.parentFolder = folder
        self.versionNumber = version
        self.fileExtension = ext
        self.children = []
        self.parents = []

    @property
    def parentProject(self):
        return self.parentFolder.parentProject

    hasChildReferences = property(lambda self: bool(self.children))
    hasParentReferences = property(lambda self: bool(self.parents))
    childReferences = property(lambda self: self.children)
    parentReferences = property(lambda self: self.parents)

    def move(self, target):
        self.parentFolder._files.remove(self)
        target._files.append(self)
        self.parentFolder = target
        return True


class FakeDoc:
    def __init__(self, name, data_file, visible=True):
        self.name = name
        self.dataFile = data_file
        self.isVisible = visible


class FakeData:
    def __init__(self, projects):
        self.dataProjects = projects

    def all_files(self):
        out = []
        for p in self.dataProjects:
            out.extend(file_ops.collect_files(p.rootFolder, 10_000)[0])
        return out

    def findFileById(self, fid):
        return next((f for f in self.all_files() if f.id == fid), None)


def _sample():
    """Car: root/{Assembly}, Chassis/{Front/Tube, Rear/Tube, Empty/Deeper}, drivetrain/{Shaft}; Shop: Parts/Bracket."""
    car, shop = FakeProject("Car", "p1"), FakeProject("Shop", "p2")
    root = car.rootFolder
    chassis = root.folder("Chassis")
    front, rear = chassis.folder("Front"), chassis.folder("Rear")
    chassis.folder("Empty").folder("Deeper")
    drive = root.folder("drivetrain")
    asm = root.file("Assembly", "urn:asm", version=7)
    tube_a = front.file("Tube", "urn:tube-a", version=2)
    tube_b = rear.file("Tube", "urn:tube-b", version=5)
    shaft = drive.file("Shaft", "urn:shaft")
    bracket = shop.rootFolder.folder("Parts").file("Bracket", "urn:br")
    asm.children = [tube_a, tube_b, bracket]
    tube_a.parents = tube_b.parents = [asm]
    bracket.parents = [asm]
    files = {"asm": asm, "tube_a": tube_a, "tube_b": tube_b, "shaft": shaft, "bracket": bracket}
    return car, shop, files


def _find(data):
    return data.findFileById


# -- Resolution --------------------------------------------------------------


class ResolveFileTests(unittest.TestCase):
    def setUp(self):
        self.car, self.shop, self.files = _sample()
        self.data = FakeData([self.car, self.shop])
        self.resolver = data_tree.PathResolver()

    def _resolve(self, spec, project=None):
        return file_ops.resolve_file(project or self.car, spec, _find(self.data), self.resolver)

    def test_by_path_case_insensitive_and_with_extension(self):
        self.assertIs(self._resolve("chassis/FRONT/tube"), self.files["tube_a"])
        self.assertIs(self._resolve("Chassis/Front/Tube.f3d"), self.files["tube_a"])
        self.assertIs(self._resolve("/Assembly"), self.files["asm"])

    def test_by_id(self):
        self.assertIs(self._resolve("urn:tube-b"), self.files["tube_b"])

    def test_id_in_other_project_refused(self):
        with self.assertRaises(file_ops.Refusal) as ctx:
            self._resolve("urn:br")
        self.assertEqual(ctx.exception.problem["problem"], "wrong_project")

    def test_unknown_id_and_path(self):
        for spec, needle in (("urn:nope", "No file with id"), ("Chassis/Front/Nut", "Files: 'Tube'"),
                             ("Nope/Tube", "No folder"), ("a/../b", "may not contain")):
            with self.subTest(spec=spec), self.assertRaises(file_ops.Refusal) as ctx:
                self._resolve(spec)
            self.assertIn(needle, ctx.exception.problem["message"])

    def test_ambiguous_path_lists_path_version_id(self):
        self.car.rootFolder.file("assembly", "urn:asm-2", version=3)
        with self.assertRaises(file_ops.Refusal) as ctx:
            self._resolve("Assembly")
        problem = ctx.exception.problem
        self.assertEqual(problem["problem"], "ambiguous")
        self.assertEqual(problem["candidates"], [
            {"path": "Car/Assembly", "version": 7, "id": "urn:asm"},
            {"path": "Car/assembly", "version": 3, "id": "urn:asm-2"},
        ])

    def test_resolve_folder_prefix(self):
        root = self.car.rootFolder
        folder, actual, missing = file_ops.resolve_folder_prefix(root, ["chassis", "New", "Sub"])
        self.assertEqual((folder.name, actual, missing), ("Chassis", ["Chassis"], ["New", "Sub"]))
        root.folder("CHASSIS", "dup")
        with self.assertRaises(LookupError):
            file_ops.resolve_folder_prefix(root, ["Chassis"])

    def test_open_file_ids_skips_unsaved_and_errors(self):
        broken = mock.Mock()
        type(broken).dataFile = mock.PropertyMock(side_effect=RuntimeError("no"))
        docs = [FakeDoc("Assembly v7", self.files["asm"]), FakeDoc("Untitled", None),
                FakeDoc("Tube", self.files["tube_a"], visible=False), broken]
        self.assertEqual(file_ops.open_file_ids(docs), {
            "urn:asm": {"document": "Assembly v7", "visible": True},
            "urn:tube-a": {"document": "Tube", "visible": False},
        })

    def test_validate_new_name(self):
        self.assertEqual(file_ops.validate_new_name("  Drive  ", "new_name"), "Drive")
        for bad in ("", "  ", "a/b", "a\\b", "..", 5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                file_ops.validate_new_name(bad, "new_name")


# -- move_files planning -----------------------------------------------------


class PlanMoveTests(unittest.TestCase):
    def setUp(self):
        self.car, self.shop, self.files = _sample()
        self.data = FakeData([self.car, self.shop])

    def _plan(self, specs, to, *, target=None, create_missing=False, open_ids=None):
        return file_ops.plan_move(
            self.car, specs, target or self.car, data_tree.split_path(to), create_missing=create_missing,
            find_by_id=_find(self.data), open_ids=open_ids or {}, resolver=data_tree.PathResolver())

    def _kinds(self, problems):
        return [p["problem"] for p in problems]

    def test_clean_plan(self):
        plan, problems, (files, target, missing) = self._plan(["Chassis/Front/Tube", "urn:shaft"], "chassis/empty")
        self.assertEqual(problems, [])
        self.assertEqual(plan["target_folder"], "Car/Chassis/Empty")
        self.assertEqual([(m["from"], m["to"]) for m in plan["moves"]], [
            ("Car/Chassis/Front/Tube", "Car/Chassis/Empty/Tube"), ("Car/drivetrain/Shaft", "Car/Chassis/Empty/Shaft")])
        self.assertEqual(target.name, "Empty")
        self.assertEqual(missing, [])

    def test_target_name_collision_is_case_insensitive(self):
        self.files["tube_b"].name = "TUBE"
        _, problems, _ = self._plan(["Chassis/Front/Tube"], "Chassis/Rear")
        self.assertEqual(self._kinds(problems), ["name_collision"])
        self.assertEqual(problems[0]["existing"][0]["id"], "urn:tube-b")

    def test_same_name_different_type_is_not_a_collision(self):
        self.car.rootFolder._folders[0]._folders[1].file("Tube", "urn:tube-drw", ext="f2d")
        self.files["tube_b"].parentFolder._files.remove(self.files["tube_b"])
        _, problems, _ = self._plan(["Chassis/Front/Tube"], "Chassis/Rear")
        self.assertEqual(problems, [])

    def test_two_moving_files_with_same_name_collide(self):
        _, problems, _ = self._plan(["urn:tube-a", "urn:tube-b"], "drivetrain")
        self.assertEqual(self._kinds(problems), ["name_collision"])
        self.assertEqual({f["id"] for f in problems[0]["files"]}, {"urn:tube-a", "urn:tube-b"})

    def test_open_file_refused_and_listed(self):
        open_ids = {"urn:tube-a": {"document": "Tube v2", "visible": False}}
        _, problems, _ = self._plan(["Chassis/Front/Tube"], "drivetrain", open_ids=open_ids)
        self.assertEqual(self._kinds(problems), ["open_in_fusion"])
        self.assertEqual(problems[0]["document"], "Tube v2")
        self.assertFalse(problems[0]["visible"])

    def test_ambiguous_listed_twice_already_there(self):
        _, problems, _ = self._plan(["urn:shaft", "drivetrain/shaft", "Assembly"], "")
        self.assertEqual(self._kinds(problems), ["listed_twice", "already_there"])

    def test_missing_target_needs_create_missing(self):
        _, problems, _ = self._plan(["urn:shaft"], "Drivetrain/Axles/Rear")
        self.assertEqual(self._kinds(problems), ["target_missing"])
        plan, problems, (_, target, missing) = self._plan(["urn:shaft"], "Drivetrain/Axles/Rear", create_missing=True)
        self.assertEqual(problems, [])
        self.assertEqual(target.name, "drivetrain")
        self.assertEqual(missing, ["Axles", "Rear"])
        self.assertEqual(plan["folders_to_create"], ["Car/drivetrain/Axles", "Car/drivetrain/Axles/Rear"])
        self.assertEqual(plan["moves"][0]["to"], "Car/drivetrain/Axles/Rear/Shaft")

    def test_ambiguous_target_folder(self):
        self.car.rootFolder.folder("DRIVETRAIN", "dup")
        _, problems, _ = self._plan(["Assembly"], "drivetrain")
        self.assertEqual(self._kinds(problems), ["target_ambiguous"])

    def test_cross_project_target(self):
        plan, problems, (_, target, _) = self._plan(["urn:shaft"], "Parts", target=self.shop)
        self.assertEqual(problems, [])
        self.assertEqual(plan["moves"][0]["to"], "Shop/Parts/Shaft")
        self.assertEqual(target.parentProject.id, "p2")


class ExecuteMoveTests(unittest.TestCase):
    def test_creates_folders_and_moves(self):
        car, _, files = _sample()
        out = file_ops.execute_move([files["shaft"]], car.rootFolder, ["New", "Sub"])
        self.assertEqual(out, {"folders_created": ["New", "Sub"], "moved": ["urn:shaft"]})
        self.assertEqual(data_tree.PathResolver().file_path(files["shaft"]), "Car/New/Sub/Shaft")

    def test_stops_at_first_failure(self):
        car, _, files = _sample()
        with mock.patch.object(FakeFile, "move", side_effect=[True, False]):
            out = file_ops.execute_move([files["shaft"], files["tube_a"], files["asm"]], car.rootFolder, [])
        self.assertEqual(out["moved"], ["urn:shaft"])
        self.assertEqual(out["failed"]["id"], "urn:tube-a")
        self.assertEqual(out["not_attempted"], ["urn:asm"])

    def test_folder_creation_failure(self):
        car, _, files = _sample()
        out = file_ops.execute_move([files["shaft"]], car.rootFolder, ["CHASSIS"])
        self.assertIn("CB_NAE", out["failed"]["error"])
        self.assertEqual(out["not_attempted"], ["urn:shaft"])


class ServerReasonTests(unittest.TestCase):
    """Every cloud write reports the server's reason for the opaque error."""

    def test_failed_move(self):
        car, _, files = _sample()
        entry = log_entry(403, "https://api/storage/v3/move", "FORBIDDEN", "Not allowed",
                          body=json.dumps({"urn": files["shaft"].id}))
        with TempLog() as log, mock.patch.object(FakeFile, "move", side_effect=lambda t: fail_with(log, entry)):
            out = file_ops.execute_move([files["shaft"]], car.rootFolder, [], log.log)
        self.assertEqual(out["failed"]["reason"], "Not allowed")
        self.assertEqual(out["failed"]["id"], "urn:shaft")

    def test_failed_folder_create(self):
        car, _, files = _sample()
        entry = log_entry(409, "https://api/storage/v3/folders", "CONTAINER_CHILD_NAME_EXISTS", "Name exists",
                          body=json.dumps([{"parentFolderUrn": car.rootFolder.id}]))
        collection = mock.Mock()
        with TempLog() as log:
            collection.add.side_effect = lambda seg: fail_with(log, entry)
            with mock.patch.object(FakeFolder, "dataFolders", collection):
                out = file_ops.execute_move([files["shaft"]], car.rootFolder, ["New"], log.log)
        self.assertEqual(out["failed"]["server"]["error_code"], "CONTAINER_CHILD_NAME_EXISTS")
        self.assertEqual(out["failed"]["step"], "create folder 'New'")

    def test_failed_folder_delete(self):
        car, _, _ = _sample()
        folder = car.rootFolder.folder("Empty", "urn:folder:co.empty")
        entry = log_entry(400, "https://api/storage/v3/folders/delete", "GEN_RELATED_OBJECTS", "Has relationships",
                          body=json.dumps({"urn": folder.id}))
        with TempLog() as log, mock.patch.object(FakeFolder, "deleteMe", side_effect=lambda: fail_with(log, entry)):
            out = file_ops.execute_delete_folders([(folder, "Car/Empty")], log.log)
        self.assertEqual(out["failed"]["reason"], "Has relationships")
        self.assertEqual(out["failed"]["path"], "Car/Empty")

    def test_plain_errors_skip_the_log(self):
        car, _, files = _sample()
        log = mock.Mock()
        with mock.patch.object(FakeFile, "move", side_effect=RuntimeError("offline")):
            out = file_ops.execute_move([files["shaft"]], car.rootFolder, [], log)
        self.assertEqual(out["failed"]["error"], "RuntimeError: offline")
        log.reason.assert_not_called()

    def test_older_failure_is_not_borrowed(self):
        with TempLog() as log:
            log.write(lock_entry("co.a"))
            offset = log.log.mark()
            self.assertIsNone(log.log.reason(offset, "urn:x:co.a", polls=1))
            log.write(lock_entry("co.a"))
            self.assertEqual(log.log.reason(offset, "urn:x:co.a", polls=1)["status"], 423)

    def test_other_resources_and_reads_are_ignored(self):
        with TempLog() as log:
            offset = log.log.mark()
            log.write(lock_entry("co.other"))
            log.write(log_entry(404, "https://api/storage/v3/folders/urn:x:co.a/parents", "NOT_FOUND", "gone",
                                method="GET"))
            self.assertIsNone(log.log.reason(offset, "urn:x:co.a", polls=1))

    def test_file_ids_match_their_version_urls(self):
        with TempLog() as log:
            offset = log.log.mark()
            log.write(log_entry(423, "https://api/entities/urn%3Aadsk.wipprod%3Afs.file%3Avf.Abc%3Fversion%3D7/rename",
                                "LOCK_RESOURCE_IS_LOCKED", "locked"))
            self.assertEqual(log.log.reason(offset, "urn:adsk.wipprod:dm.lineage:Abc", polls=1)["message"], "locked")
            self.assertEqual(log.log.reason(offset, "urn:adsk.wipprod:fs.file:vf.Abc?version=7", polls=1)["status"], 423)

    def test_unparseable_body_and_missing_log(self):
        with TempLog() as log:
            offset = log.log.mark()
            log.write("20261105T113355 E 1 POST 500 https://api/co.a - requestId:r1\nResponse Body: <html>oops\n")
            reason = log.log.reason(offset, "urn:x:co.a", polls=1)
        self.assertEqual((reason["status"], reason["message"], reason["error_code"]), (500, "<html>oops", None))
        self.assertIsNone(app_log.ServerLog().reason(app_log.ServerLog().mark(), "urn:x:co.a"))
        missing = app_log.ServerLog("/nonexistent/AppLogFile.log")
        self.assertIsNone(missing.mark())
        self.assertEqual(missing.since(0), "")

    def test_wait_pumps_events(self):
        with mock.patch.object(adsk, "doEvents", create=True) as pump:
            file_ops._wait(0)
        pump.assert_called_once_with()


# -- Link check --------------------------------------------------------------


class LinkCheckTests(unittest.TestCase):
    def setUp(self):
        self.car, self.shop, self.files = _sample()
        self.data = FakeData([self.car, self.shop])

    def test_snapshot_includes_linked_files(self):
        snap, capped = file_ops.link_snapshot([self.files["tube_a"]], data_tree.PathResolver())
        self.assertFalse(capped)
        self.assertEqual(set(snap), {"urn:tube-a", "urn:asm"})
        self.assertEqual(snap["urn:asm"]["uses"], ["urn:br", "urn:tube-a", "urn:tube-b"])
        self.assertEqual(snap["urn:tube-a"]["used_by"], ["urn:asm"])

    def test_snapshot_cap(self):
        snap, capped = file_ops.link_snapshot([self.files["asm"]], data_tree.PathResolver(), cap=2)
        self.assertTrue(capped)
        self.assertEqual(len(snap), 2)

    def test_move_keeps_links_and_reports_expected_path_change(self):
        primary = [self.files["tube_a"]]
        before, _ = file_ops.link_snapshot(primary, data_tree.PathResolver())
        self.files["tube_a"].move(self.car.rootFolder._folders[1])
        after = file_ops.link_snapshot_after(list(before), _find(self.data), data_tree.PathResolver())
        report = file_ops.diff_links(before, after, {"urn:tube-a"})
        self.assertTrue(report["ok"])
        self.assertEqual(report["path_changes"], [
            {"id": "urn:tube-a", "before": "Car/Chassis/Front/Tube", "after": "Car/drivetrain/Tube", "expected": True}])
        self.assertEqual(report["links_lost"], [])

    def test_lost_link_and_unresolvable(self):
        before, _ = file_ops.link_snapshot([self.files["tube_a"], self.files["asm"]], data_tree.PathResolver())
        self.files["asm"].children.remove(self.files["tube_a"])
        lookup = lambda fid: None if fid == "urn:br" else self.data.findFileById(fid)  # noqa: E731
        after = file_ops.link_snapshot_after(list(before), lookup, data_tree.PathResolver())
        report = file_ops.diff_links(before, after, set())
        self.assertFalse(report["ok"])
        self.assertEqual([u["id"] for u in report["unresolvable"]], ["urn:br"])
        self.assertEqual(report["links_lost"], [{"id": "urn:asm", "path": "Car/Assembly", "link": "uses",
                                                 "ref_id": "urn:tube-a", "ref_path": "Car/Chassis/Front/Tube"}])

    def test_unexpected_path_change_fails_check(self):
        before, _ = file_ops.link_snapshot([self.files["tube_a"]], data_tree.PathResolver())
        self.files["asm"].move(self.car.rootFolder._folders[1])
        after = file_ops.link_snapshot_after(list(before), _find(self.data), data_tree.PathResolver())
        report = file_ops.diff_links(before, after, {"urn:tube-a"})
        self.assertFalse(report["ok"])
        self.assertFalse(report["path_changes"][0]["expected"])

    def test_lookup_exception_is_unresolvable(self):
        after = file_ops.link_snapshot_after(["urn:x"], mock.Mock(side_effect=RuntimeError("gone")),
                                             data_tree.PathResolver())
        self.assertTrue(after["urn:x"]["unresolvable"])
        self.assertIn("gone", after["urn:x"]["error"])

    def test_links_by_path(self):
        snap, _ = file_ops.link_snapshot([self.files["tube_a"]], data_tree.PathResolver())
        self.assertEqual(file_ops.links_by_path(snap, ["urn:tube-a"]),
                         [{"id": "urn:tube-a", "path": "Car/Chassis/Front/Tube", "used_by": ["Car/Assembly"]}])


# -- Renames -----------------------------------------------------------------


class FolderRenameTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(file_ops, "_wait")
        self.wait = patcher.start()
        self.addCleanup(patcher.stop)
        self.car, _, _ = _sample()
        self.chassis = self.car.rootFolder._folders[0]
        self.drive = self.car.rootFolder._folders[1]

    def _plan(self, folder, name):
        return file_ops.plan_folder_rename(folder, name, data_tree.PathResolver())

    def test_sibling_differing_only_by_case_refused(self):
        plan, problems = self._plan(self.chassis, "Drivetrain")
        self.assertEqual([p["problem"] for p in problems], ["name_collision"])
        self.assertEqual(problems[0]["existing"], [{"path": "Car/drivetrain", "id": self.drive.id}])

    def test_plain_rename_is_one_step(self):
        plan, problems = self._plan(self.chassis, "Frame")
        self.assertEqual((problems, plan["steps"]), ([], ["Frame"]))
        self.assertEqual(file_ops.execute_folder_rename(self.chassis, plan["steps"]),
                         {"steps_done": ["Frame"], "name_now": "Frame"})

    def test_case_only_rename_goes_through_temp_name(self):
        self.car.rootFolder.folder("Drivetrain" + file_ops._TEMP_SUFFIX)
        plan, problems = self._plan(self.drive, "Drivetrain")
        self.assertEqual(problems, [])
        self.assertTrue(plan["case_only"])
        self.assertEqual(plan["steps"], ["Drivetrain" + file_ops._TEMP_SUFFIX + "2", "Drivetrain"])
        out = file_ops.execute_folder_rename(self.drive, plan["steps"])
        self.assertEqual(out["name_now"], "Drivetrain")
        self.assertEqual(self.drive.name_log, plan["steps"])

    def test_failed_second_step_restores_original(self):
        with mock.patch.object(FakeFolder, "name", new_callable=mock.PropertyMock) as prop:
            state = {"v": "drivetrain", "calls": 0}

            def setter_or_getter(*args):
                if not args:
                    return state["v"]
                state["calls"] += 1
                if state["calls"] == 2:
                    raise RuntimeError("cloud error")
                state["v"] = args[0]

            prop.side_effect = setter_or_getter
            out = file_ops.execute_folder_rename(self.drive, ["tmp", "Drivetrain"])
        self.assertEqual(out["steps_done"], ["tmp"])
        self.assertIn("cloud error", out["failed"]["error"])
        self.assertTrue(out["restored"])
        self.assertEqual(out["name_now"], "drivetrain")

    def _case_rename(self, refusals, on_refuse=lambda: None, server_log=None):
        steps = ["drivetrain_tmp", "Drivetrain"]
        with RefusingFolder(self.drive, refusals, on_refuse) as folder:
            out = file_ops.execute_folder_rename(self.drive, steps, server_log)
        return out, folder.attempts

    def test_server_lock_is_waited_out(self):
        # Live: step 2 got POST 423 LOCK_RESOURCE_IS_LOCKED about 0.5 s after step 1.
        with TempLog() as log:
            out, attempts = self._case_rename(lambda name, n: n == 2, lambda: log.write(lock_entry(self.drive.id)),
                                              log.log)
        self.assertEqual(attempts, ["drivetrain_tmp", "Drivetrain", "Drivetrain"])
        self.assertEqual(out, {"steps_done": ["drivetrain_tmp", "Drivetrain"], "name_now": "Drivetrain",
                               "lock_retries": 1})
        self.assertEqual(self.wait.call_args_list[0], mock.call(file_ops._STEP_SETTLE_S))

    def test_lock_that_never_clears_is_bounded_and_said_plainly(self):
        with TempLog() as log:
            out, attempts = self._case_rename(lambda name, n: name == "Drivetrain",
                                              lambda: log.write(lock_entry(self.drive.id)), log.log)
        tries = 1 + len(file_ops._LOCK_RETRY_DELAYS_S)
        self.assertEqual(attempts.count("Drivetrain"), tries)
        failed = out["failed"]
        self.assertEqual((failed["attempts"], failed["to"]), (tries, "Drivetrain"))
        self.assertEqual(failed["error"], f"RuntimeError: {OPAQUE}")
        self.assertEqual(failed["server"]["status"], 423)
        self.assertEqual(failed["server"]["error_code"], "LOCK_RESOURCE_IS_LOCKED")
        self.assertTrue(out["restored"])
        self.assertEqual(out["name_now"], "drivetrain")
        self.assertIn("Resource is locked by other operation", out["message"])
        self.assertIn("restored", out["message"])

    def test_restore_also_waits_out_the_lock(self):
        with TempLog() as log:
            out, attempts = self._case_rename(
                lambda name, n: name == "Drivetrain" or (name == "drivetrain" and attempts_seen(n)),
                lambda: log.write(lock_entry(self.drive.id)), log.log)
        self.assertTrue(out["restored"])
        self.assertEqual(attempts[-2:], ["drivetrain", "drivetrain"])

    def test_restore_failure_reported(self):
        with TempLog() as log:
            out, _ = self._case_rename(lambda name, n: n > 1, lambda: log.write(lock_entry(self.drive.id)), log.log)
        self.assertFalse(out["restored"])
        self.assertEqual(out["name_now"], "drivetrain_tmp")
        self.assertEqual(out["restore_error"]["server"]["status"], 423)
        self.assertIn("now named 'drivetrain_tmp'", out["message"])

    def test_other_server_refusals_are_not_retried(self):
        url = f"https://api/storage/v3/entities/{self.drive.id}/rename?newName=Sub"
        entry = log_entry(409, url, "CONTAINER_CHILD_NAME_EXISTS", "Another object with the same name already exists")
        with TempLog() as log:
            out, attempts = self._case_rename(lambda name, n: n == 2, lambda: log.write(entry), log.log)
        self.assertEqual(len(attempts), 3, "the failed step once, then the restore")
        self.assertEqual(out["failed"]["attempts"], 1)
        self.assertEqual(out["failed"]["reason"], "Another object with the same name already exists")

    def test_no_log_evidence_still_retries_and_says_so(self):
        out, attempts = self._case_rename(lambda name, n: name == "Drivetrain")
        self.assertEqual(attempts.count("Drivetrain"), 1 + len(file_ops._LOCK_RETRY_DELAYS_S))
        self.assertIsNone(out["failed"]["server"])
        self.assertIn("No matching failed request", out["failed"]["note"])

    def test_other_errors_are_not_retried(self):
        with mock.patch.object(FakeFolder, "name", new_callable=mock.PropertyMock) as prop:
            prop.side_effect = ["drivetrain", RuntimeError("cloud error"), "drivetrain"]
            out = file_ops.execute_folder_rename(self.drive, ["Frame"])
        self.assertEqual(out["failed"]["attempts"], 1)
        self.assertNotIn("server", out["failed"])

    def test_read_back_mismatch_fails(self):
        with mock.patch.object(FakeFolder, "name", new_callable=mock.PropertyMock) as prop:
            prop.return_value = "drivetrain"
            out = file_ops.execute_folder_rename(self.drive, ["Frame"])
        self.assertIn("reads back", out["failed"]["error"])

    def test_folder_is_refetched_between_steps(self):
        fresh = mock.Mock(id=self.drive.id)
        fresh.name = "x"
        parent = self.drive.parentFolder
        with mock.patch.object(file_ops.cloud, "items", return_value=[fresh]):
            self.assertIs(file_ops._refetch_folder(self.drive), fresh)
        self.assertIs(parent, self.drive.parentFolder)

    def test_refetch_falls_back_to_the_held_folder(self):
        with mock.patch.object(file_ops.cloud, "items", side_effect=RuntimeError("offline")), \
                mock.patch.object(file_ops, "log"):
            self.assertIs(file_ops._refetch_folder(self.drive), self.drive)
        with mock.patch.object(file_ops.cloud, "items", return_value=[]):
            self.assertIs(file_ops._refetch_folder(self.drive), self.drive)

    def test_no_change_and_root(self):
        plan, problems = self._plan(self.drive, "drivetrain")
        self.assertTrue(plan["no_change"])
        _, problems = self._plan(self.car.rootFolder, "X")
        self.assertEqual(problems[0]["problem"], "root_folder")


class FileRenameTests(unittest.TestCase):
    def setUp(self):
        self.car, _, self.files = _sample()

    def _plan(self, f, name, open_ids=None):
        return file_ops.plan_file_rename(f, name, open_ids or {}, data_tree.PathResolver())

    def test_clean_and_collision_and_open_and_extension(self):
        self.assertEqual(self._plan(self.files["tube_a"], "Tube Front")[1], [])
        self.files["tube_a"].parentFolder.file("tube front", "urn:x")
        kinds = [p["problem"] for p in self._plan(self.files["tube_a"], "Tube Front",
                                                  {"urn:tube-a": {"document": "Tube", "visible": True}})[1]]
        self.assertEqual(kinds, ["open_in_fusion", "name_collision"])
        self.assertEqual(self._plan(self.files["asm"], "Asm.f3d")[1][0]["problem"], "extension_in_name")
        self.assertTrue(self._plan(self.files["asm"], "Assembly")[0]["no_change"])


# -- remove_empty_folders ----------------------------------------------------


class EmptyFolderTests(unittest.TestCase):
    def test_finds_nested_empties_bottom_up_excluding_start(self):
        car, _, _ = _sample()
        car.rootFolder.folder("Blank")
        empty, complete = file_ops.find_empty_folders(car.rootFolder, "Car")
        self.assertTrue(complete)
        self.assertEqual([p for _, p in empty], ["Car/Blank", "Car/Chassis/Empty/Deeper", "Car/Chassis/Empty"])
        self.assertEqual(file_ops.top_level_paths(empty), ["Car/Blank", "Car/Chassis/Empty"])

    def test_empty_start_folder_is_not_listed(self):
        car = FakeProject("Car", "p1")
        empty, _ = file_ops.find_empty_folders(car.rootFolder, "Car")
        self.assertEqual(empty, [])

    def test_time_budget_never_counts_unvisited_as_empty(self):
        car, _, _ = _sample()
        ticks = iter([0.0, 0.0, 0.0] + [100.0] * 50)
        empty, complete = file_ops.find_empty_folders(car.rootFolder, "Car", budget_s=10, clock=lambda: next(ticks))
        self.assertFalse(complete)
        self.assertEqual(empty, [])

    def test_delete_bottom_up(self):
        car, _, _ = _sample()
        empty, _ = file_ops.find_empty_folders(car.rootFolder, "Car")
        out = file_ops.execute_delete_folders(empty)
        self.assertEqual(out, {"deleted": ["Car/Chassis/Empty/Deeper", "Car/Chassis/Empty"]})
        self.assertEqual([f.name for f in car.rootFolder._folders[0]._folders], ["Front", "Rear"])

    def test_delete_stops_when_folder_gained_content(self):
        car, _, _ = _sample()
        empty, _ = file_ops.find_empty_folders(car.rootFolder, "Car")
        empty[1][0].file("New", "urn:new")
        out = file_ops.execute_delete_folders(empty)
        self.assertEqual(out["deleted"], ["Car/Chassis/Empty/Deeper"])
        self.assertIn("no longer empty", out["failed"]["error"])

    def test_delete_me_false_stops(self):
        car, _, _ = _sample()
        empty, _ = file_ops.find_empty_folders(car.rootFolder, "Car")
        with mock.patch.object(FakeFolder, "deleteMe", return_value=False):
            out = file_ops.execute_delete_folders(empty)
        self.assertEqual(out["deleted"], [])
        self.assertEqual(out["not_attempted"], ["Car/Chassis/Empty"])


# -- Handlers ----------------------------------------------------------------


class HandlerTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("_wait", mock.Mock()), ("current_log", lambda: app_log.ServerLog())):
            patcher = mock.patch.object(file_ops, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.car, self.shop, self.files = _sample()
        self.data = FakeData([self.car, self.shop])
        self.docs = []

    def _call(self, handler, arguments):
        app = mock.Mock(data=self.data, documents=self.docs)
        with mock.patch.object(file_ops, "get_app", return_value=app), mock.patch.object(file_ops, "log"):
            result = handler(arguments)
        try:
            return result, json.loads(result["content"][0]["text"])
        except ValueError:
            return result, result["content"][0]["text"]

    def test_validation_errors_touch_nothing(self):
        cases = [
            (file_ops.move_files, {"project": "Car", "files": [], "to_folder": ""}, "'files'"),
            (file_ops.move_files, {"project": "Car", "files": ["a"]}, "'to_folder'"),
            (file_ops.move_files, {"project": "Car", "files": ["a"] * 51, "to_folder": ""}, "at most"),
            (file_ops.move_files, {"project": "Car", "files": ["a"], "to_folder": "", "dry_run": "no"}, "dry_run"),
            (file_ops.move_files, {"project": "Car", "files": [""], "to_folder": ""}, "non-empty"),
            (file_ops.move_files, {"project": "Car", "files": ["a"], "to_folder": "", "create_missing": 1},
             "create_missing"),
            (file_ops.rename_folder, {"project": "Car", "folder": "", "new_name": "x"}, "'folder'"),
            (file_ops.rename_folder, {"project": "Car", "folder": "a", "new_name": "b/c"}, "new_name"),
            (file_ops.rename_file, {"project": "Car", "new_name": "x"}, "'file'"),
            (file_ops.remove_empty_folders, {"folder": "a"}, "'project'"),
            (file_ops.remove_empty_folders, {"project": "Car", "bogus": 1}, "bogus"),
        ]
        for handler, arguments, needle in cases:
            with self.subTest(arguments=arguments), mock.patch.object(file_ops, "get_app") as get_app:
                result = handler(arguments)
                get_app.assert_not_called()
            self.assertTrue(result["isError"])
            self.assertIn(needle, result["content"][0]["text"])

    def test_move_dry_run_changes_nothing(self):
        result, payload = self._call(file_ops.move_files,
                                     {"project": "Car", "files": ["Chassis/Front/Tube"], "to_folder": "drivetrain"})
        self.assertFalse(result["isError"])
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["changed"])
        self.assertEqual(self.files["tube_a"].parentFolder.name, "Front")
        self.assertEqual(payload["link_check"]["links"][0]["used_by"], ["Car/Assembly"])

    def test_move_applies_and_link_check_passes(self):
        result, payload = self._call(file_ops.move_files, {
            "project": "Car", "files": ["urn:tube-a"], "to_folder": "Drivetrain/Tubes",
            "create_missing": True, "dry_run": False})
        self.assertFalse(result["isError"], payload)
        self.assertEqual(payload["result"]["moved"], ["urn:tube-a"])
        self.assertTrue(payload["link_check"]["ok"])
        self.assertEqual(payload["link_check"]["path_changes"][0]["after"], "Car/drivetrain/Tubes/Tube")

    def test_move_cross_project(self):
        result, payload = self._call(file_ops.move_files, {
            "project": "Car", "files": ["urn:shaft"], "to_folder": "parts", "to_project": "shop", "dry_run": False})
        self.assertFalse(result["isError"], payload)
        self.assertEqual(payload["plan"]["cross_project"], {"from": "Car", "to": "Shop"})
        self.assertEqual(self.files["shaft"].parentProject.id, "p2")

    def test_move_refuses_open_file_and_never_closes(self):
        doc = FakeDoc("Tube v2", self.files["tube_a"])
        doc.close = mock.Mock()
        self.docs.append(doc)
        result, payload = self._call(file_ops.move_files, {
            "project": "Car", "files": ["urn:tube-a"], "to_folder": "drivetrain", "dry_run": False})
        self.assertTrue(result["isError"])
        self.assertTrue(payload["refused"])
        self.assertEqual(payload["problems"][0]["problem"], "open_in_fusion")
        doc.close.assert_not_called()
        self.assertEqual(self.files["tube_a"].parentFolder.name, "Front")

    def test_move_unknown_projects(self):
        result, text = self._call(file_ops.move_files, {"project": "Boat", "files": ["a"], "to_folder": ""})
        self.assertIn("No project named", text)
        result, text = self._call(file_ops.move_files,
                                  {"project": "Car", "files": ["Assembly"], "to_folder": "", "to_project": "Boat"})
        self.assertIn("to_project", text)

    def test_rename_folder_refuses_case_clash(self):
        result, payload = self._call(file_ops.rename_folder,
                                     {"project": "Car", "folder": "Chassis", "new_name": "Drivetrain",
                                      "dry_run": False})
        self.assertTrue(payload["refused"])
        self.assertEqual(self.car.rootFolder._folders[0].name, "Chassis")

    def test_rename_folder_case_only_applies_two_steps(self):
        result, payload = self._call(file_ops.rename_folder,
                                     {"project": "Car", "folder": "drivetrain", "new_name": "Drivetrain",
                                      "dry_run": False})
        self.assertFalse(result["isError"], payload)
        self.assertEqual(payload["result"]["steps_done"], ["Drivetrain__case_rename_tmp", "Drivetrain"])
        self.assertEqual(payload["link_check"]["path_changes"],
                         [{"id": "urn:shaft", "before": "Car/drivetrain/Shaft", "after": "Car/Drivetrain/Shaft",
                           "expected": True}])

    def test_rename_folder_dry_run(self):
        result, payload = self._call(file_ops.rename_folder,
                                     {"project": "Car", "folder": "chassis", "new_name": "Frame"})
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["link_check"]["files_to_check"], 3)  # 2 tubes + the assembly using them
        self.assertEqual(self.car.rootFolder._folders[0].name, "Chassis")

    def test_rename_file_applies(self):
        result, payload = self._call(file_ops.rename_file, {"project": "Car", "file": "Chassis/Rear/Tube",
                                                            "new_name": "Tube Rear", "dry_run": False})
        self.assertFalse(result["isError"], payload)
        self.assertEqual(self.files["tube_b"].name, "Tube Rear")
        self.assertTrue(payload["link_check"]["ok"])

    def test_rename_file_ambiguous(self):
        self.car.rootFolder.file("Assembly", "urn:asm-2")
        result, payload = self._call(file_ops.rename_file, {"project": "Car", "file": "Assembly", "new_name": "X"})
        self.assertTrue(payload["refused"])
        self.assertEqual(len(payload["problems"][0]["candidates"]), 2)

    def test_remove_empty_dry_run_then_apply(self):
        result, payload = self._call(file_ops.remove_empty_folders, {"project": "Car", "folder": "chassis"})
        self.assertEqual(payload["plan"]["top_level"], ["Car/Chassis/Empty"])
        self.assertEqual(len(self.car.rootFolder._folders[0]._folders), 3)
        result, payload = self._call(file_ops.remove_empty_folders, {"project": "Car", "dry_run": False})
        self.assertEqual(payload["result"]["deleted"], ["Car/Chassis/Empty/Deeper", "Car/Chassis/Empty"])
        result, payload = self._call(file_ops.remove_empty_folders, {"project": "Car"})
        self.assertEqual(payload["message"], "No empty folders found.")


class SchemaTests(unittest.TestCase):
    def test_schemas_match_handlers(self):
        expected = {
            "move_files": file_ops.MOVE_ARGS,
            "rename_folder": file_ops.RENAME_FOLDER_ARGS,
            "rename_file": file_ops.RENAME_FILE_ARGS,
            "remove_empty_folders": file_ops.REMOVE_EMPTY_ARGS,
        }
        for name, args in expected.items():
            schema = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == name)["inputSchema"]
            with self.subTest(tool=name):
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["properties"]), args)
                self.assertIn("project", schema["required"])

    def test_move_limit_matches(self):
        schema = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "move_files")["inputSchema"]
        self.assertIn(f"1 to {file_ops.MAX_FILES_PER_CALL}", schema["properties"]["files"]["description"])


if __name__ == "__main__":
    unittest.main()
