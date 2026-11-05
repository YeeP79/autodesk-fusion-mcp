"""Tests for delete_files: ordering, log parsing, planning and per-file execution, using fakes."""

import json
import os
import tempfile
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import delete_files as df
from fusion_bridge import tool_surface

URN = "urn:adsk.wipprod:dm.lineage:WdIvJR0nQ2y5tk4dex7OPw"
OTHER_URN = "urn:adsk.wipprod:dm.lineage:AAAAJR0nQ2y5tk4dex7OPw"


def failure_lines(urn=URN, stamp="20261104T174146", request="ef95f0dfd13c49528271f631"):
    """The real shape of a refused delete in Fusion's app log."""
    return (
        f"{stamp} E .24563490 POST 400 https://api.aps.usa.autodesk.com/wipdata-serv/storage/v3/lineages/delete"
        f" - workflow:WIPRequest2 attempt:0 requestId:f360_WIPRequest2__1-6aebd0c9-{request} dt=0.36\n"
        "Request Content-Type: application/json\n"
        f'Request Body: {{"lineages":[{{"urn":"{urn}"}}]}}\n'
        "Response Content-Type: application/json\n"
        'Response Body: {"errorCode":"GEN_RELATED_OBJECTS","errorClass":"ApiError",'
        '"message":"The object being acted on has relationships."}\n'
        f"{stamp} I 24564405 MDS: In fireWorkEvents\n"
    )


SUCCESS_LINE = (
    "20261104T174145 I .24563490 POST 200 https://api.aps.usa.autodesk.com/wipdata-serv/storage/v3/lineages/delete"
    " - workflow:WIPRequest2 attempt:0 requestId:f360_WIPRequest2__1-6aebd0c8-848a dt=0.49\n"
)


class FakeProject:
    def __init__(self, name="Buggy", pid="a.1"):
        self.name, self.id = name, pid
        self.rootFolder = FakeFolder(name, project=self, root=True)


class FakeFolder:
    def __init__(self, name, parent=None, project=None, root=False):
        self.name, self.id = name, f"folder:{name}"
        self.parentFolder, self.parentProject, self.isRoot = parent, project, root
        self.dataFiles, self.dataFolders = [], []
        self.deleted, self.delete_error = False, None
        if parent is not None:
            parent.dataFolders.append(self)
            self.parentProject = parent.parentProject

    def deleteMe(self):
        if self.delete_error:
            raise self.delete_error
        self.deleted = True
        return True


class FakeFile:
    def __init__(self, name, folder, fid=None, error=None, result=True):
        self.name, self.id = name, fid or f"urn:adsk.wipprod:dm.lineage:{name}"
        self.parentFolder, self.parentProject = folder, folder.parentProject
        self.fileExtension, self.versionNumber = "f3d", 3
        self.parents, self.error, self.result = [], error, result
        self.delete_calls = 0
        self.on_delete = None
        folder.dataFiles.append(self)

    @property
    def parentReferences(self):
        if isinstance(self.parents, Exception):
            raise self.parents
        return list(self.parents)

    def deleteMe(self):
        self.delete_calls += 1
        if self.on_delete:
            self.on_delete()
        if self.error:
            raise self.error
        if self.result:
            self.parentFolder.dataFiles.remove(self)
        return self.result


class FakeReader:
    """Appends *text* to the log when a delete runs; mark/since behave like LogReader."""

    def __init__(self, text=""):
        self.text = text

    def mark(self):
        return len(self.text)

    def since(self, offset):
        return "" if offset is None else self.text[offset:]


def lookup_for(*files):
    table = {f.id: f for f in files}
    return table.get


def run(work_files, *, opened=None, reader=None, lookup=None, out_of_time=lambda: False):
    return df.execute_delete(
        {f.id: f for f in work_files}, lookup=lookup or lookup_for(*work_files),
        open_ids=lambda: opened or {}, resolver=df.PathResolver(), reader=reader or FakeReader(),
        out_of_time=out_of_time, sleep=lambda s: None)


def plan(project, specs=(), folder=None, opened=None, find=None, **kwargs):
    return df.plan_delete(project, list(specs), folder, find_by_id=find or (lambda i: None),
                          open_ids=opened or {}, resolver=df.PathResolver(), **kwargs)


def kinds(problems):
    return [p["problem"] for p in problems]


class OrderRoundsTests(unittest.TestCase):
    def test_no_links_is_one_round_in_input_order(self):
        self.assertEqual(df.order_rounds(["a", "b"], {}), ([["a", "b"]], []))

    def test_empty(self):
        self.assertEqual(df.order_rounds([], {}), ([], []))

    def test_parent_before_child_whatever_the_input_order(self):
        rounds, stuck = df.order_rounds(["child", "mid", "top"], {"child": ["mid"], "mid": ["top"]})
        self.assertEqual(rounds, [["top"], ["mid"], ["child"]])
        self.assertEqual(stuck, [])

    def test_shared_child_waits_for_every_parent(self):
        rounds, _ = df.order_rounds(["c", "p1", "p2"], {"c": ["p1", "p2"], "p2": ["p1"]})
        self.assertEqual(rounds, [["p1"], ["p2"], ["c"]])

    def test_parents_outside_the_set_are_ignored(self):
        self.assertEqual(df.order_rounds(["a"], {"a": ["elsewhere"]}), ([["a"]], []))

    def test_self_link_does_not_stick(self):
        self.assertEqual(df.order_rounds(["a"], {"a": ["a"]}), ([["a"]], []))

    def test_cycle_and_what_hangs_below_it_are_stuck(self):
        rounds, stuck = df.order_rounds(["a", "b", "c", "d"], {"a": ["b"], "b": ["a"], "c": ["a"]})
        self.assertEqual(rounds, [["d"]])
        self.assertEqual(stuck, ["a", "b", "c"])


class LogParsingTests(unittest.TestCase):
    def test_real_failure_entry(self):
        entries = df.parse_delete_entries(SUCCESS_LINE + failure_lines())
        self.assertEqual([e["status"] for e in entries], [200, 400])
        self.assertIn(URN, entries[1]["request_body"])
        self.assertIn("GEN_RELATED_OBJECTS", entries[1]["response_body"])
        self.assertTrue(entries[1]["request_id"].startswith("f360_WIPRequest2__"))

    def test_reason_from_real_lines(self):
        reason = df.server_reason(SUCCESS_LINE + failure_lines(), URN)
        self.assertEqual(reason["status"], 400)
        self.assertEqual(reason["error_code"], "GEN_RELATED_OBJECTS")
        self.assertEqual(reason["message"], "The object being acted on has relationships.")

    def test_another_files_failure_is_never_borrowed(self):
        self.assertIsNone(df.server_reason(failure_lines(urn=OTHER_URN), URN))

    def test_success_entry_is_not_a_reason(self):
        self.assertIsNone(df.server_reason(SUCCESS_LINE, URN))

    def test_empty_log(self):
        self.assertIsNone(df.server_reason("", URN))

    def test_latest_failure_wins(self):
        text = failure_lines(request="first").replace("has relationships", "old") + failure_lines(request="second")
        self.assertTrue(df.server_reason(text, URN)["request_id"].endswith("second"))

    def test_lines_after_the_next_timestamp_do_not_join_the_entry(self):
        text = (failure_lines().split("Response Body")[0]
                + "20261104T174146 I 1 something else\nResponse Body: {\"errorCode\":\"WRONG\"}\n")
        reason = df.server_reason(text, URN)
        self.assertIsNone(reason["error_code"])

    def test_non_json_response_body_is_kept_as_message(self):
        text = failure_lines().replace(
            'Response Body: {"errorCode":"GEN_RELATED_OBJECTS","errorClass":"ApiError",'
            '"message":"The object being acted on has relationships."}', "Response Body: Bad Gateway")
        reason = df.server_reason(text, URN)
        self.assertIsNone(reason["error_code"])
        self.assertEqual(reason["message"], "Bad Gateway")

    def test_request_body_cut_short_still_matches_the_urn(self):
        text = failure_lines().replace(f'{{"lineages":[{{"urn":"{URN}"}}]}}', f'{{"lineages":[{{"urn":"{URN}"')
        self.assertIsNotNone(df.server_reason(text, URN))

    def test_batch_request_body_matches_each_urn(self):
        body = json.dumps({"lineages": [{"urn": OTHER_URN}, {"urn": URN}]}, separators=(",", ":"))
        text = failure_lines().replace(f'{{"lineages":[{{"urn":"{URN}"}}]}}', body)
        self.assertIsNotNone(df.server_reason(text, URN))

    def test_log_reader_reads_only_past_the_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AppLogFile.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(failure_lines(request="stale"))
            reader = df.LogReader(path)
            offset = reader.mark()
            self.assertIsNone(df.server_reason(reader.since(offset), URN))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(failure_lines(request="fresh"))
            self.assertTrue(df.server_reason(reader.since(offset), URN)["request_id"].endswith("fresh"))

    def test_log_reader_without_a_log(self):
        reader = df.LogReader(None)
        self.assertIsNone(reader.mark())
        self.assertEqual(reader.since(None), "")

    def test_newest_log_picks_latest_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = os.path.join(tmp, "USER", "logs")
            os.makedirs(logs)
            older, newer = (os.path.join(logs, f"AppLogFile{n}.log") for n in ("2", "1"))
            for path, stamp in ((older, 100), (newer, 200)):
                open(path, "w", encoding="utf-8").close()
                os.utime(path, (stamp, stamp))
            self.assertEqual(df.newest_log([tmp]), newer)
            self.assertIsNone(df.newest_log([os.path.join(tmp, "missing")]))

    def test_find_reason_waits_for_a_late_log_line(self):
        reader = FakeReader()
        offset = reader.mark()

        def late(_seconds):
            reader.text += failure_lines()

        self.assertIsNotNone(df.find_reason(reader, offset, URN, sleep=late))

    def test_find_reason_gives_up(self):
        naps = []
        self.assertIsNone(df.find_reason(FakeReader(), 0, URN, polls=3, sleep=naps.append))
        self.assertEqual(len(naps), 2)


class ValidationTests(unittest.TestCase):
    def bad(self, arguments, fragment):
        with self.assertRaises(ValueError) as ctx:
            df.validate_arguments(arguments)
        self.assertIn(fragment, str(ctx.exception))

    def test_defaults_to_a_dry_run(self):
        options = df.validate_arguments({"project": "P", "files": ["a"]})
        self.assertTrue(options["dry_run"])
        self.assertFalse(options["confirm"])

    def test_real_run_needs_confirm(self):
        self.bad({"project": "P", "files": ["a"], "dry_run": False}, "confirm")
        self.bad({"project": "P", "files": ["a"], "dry_run": False, "confirm": False}, "confirm")
        self.assertFalse(df.validate_arguments(
            {"project": "P", "files": ["a"], "dry_run": False, "confirm": True})["dry_run"])

    def test_flags_must_be_real_booleans(self):
        for name in ("dry_run", "confirm", "include_empty_folder"):
            self.bad({"project": "P", "files": ["a"], name: "false"}, name)

    def test_project_required(self):
        self.bad({"files": ["a"]}, "project")
        self.bad({"project": "  ", "files": ["a"]}, "project")

    def test_needs_something_to_delete(self):
        self.bad({"project": "P"}, "pass 'files'")
        self.bad({"project": "P", "files": []}, "pass 'files'")

    def test_bad_files(self):
        self.bad({"project": "P", "files": "a"}, "list")
        self.bad({"project": "P", "files": [""]}, "non-empty")
        self.bad({"project": "P", "files": [1]}, "non-empty")
        self.bad({"project": "P", "files": ["a"] * (df.MAX_FILES_PER_CALL + 1)}, "at most")

    def test_project_root_is_not_a_folder_target(self):
        self.bad({"project": "P", "folder": ""}, "DataProject has no delete")
        self.bad({"project": "P", "folder": "/"}, "DataProject has no delete")

    def test_dot_segments_refused(self):
        self.bad({"project": "P", "folder": "a/../b"}, "..")

    def test_include_empty_folder_needs_folder(self):
        self.bad({"project": "P", "files": ["a"], "include_empty_folder": True}, "needs 'folder'")

    def test_unknown_argument(self):
        self.bad({"project": "P", "files": ["a"], "force": True}, "force")

    def test_not_an_object(self):
        self.bad([], "object")

    def test_handler_reports_errors_without_touching_fusion(self):
        with mock.patch.object(df, "get_app", side_effect=AssertionError("touched Fusion")):
            result = df.delete_files({"project": "P", "files": ["a"], "dry_run": False})
        self.assertTrue(result["isError"])
        self.assertIn("confirm", result["content"][0]["text"])


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict_and_only_project_is_required(self):
        tool = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "delete_files")
        schema = tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["project"])
        self.assertEqual(set(schema["properties"]), df._KNOWN_ARGS)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.project = FakeProject()
        self.root = self.project.rootFolder
        self.archive = FakeFolder("Archive", parent=self.root)

    def test_folder_plan_orders_parents_first_with_sizes(self):
        child = FakeFile("child", self.archive)
        parent = FakeFile("parent", self.archive)
        child.parents = [parent]
        result, problems, work = plan(self.project, folder=self.archive)
        self.assertEqual(problems, [])
        self.assertEqual(list(work), [parent.id, child.id])
        self.assertEqual(result["summary"]["files"], 2)
        self.assertEqual(result["summary"]["files_per_round"], [1, 1])
        self.assertEqual(result["order"][0]["path"], "Buggy/Archive/parent")
        self.assertEqual(result["order"][1]["linked_from_in_set"][0]["id"], parent.id)

    def test_subfolders_are_reported_not_included(self):
        sub = FakeFolder("Sub", parent=self.archive)
        FakeFile("deep", sub)
        result, _, work = plan(self.project, folder=self.archive)
        self.assertEqual(work, {})
        self.assertEqual(result["folder"]["subfolders_not_included"], ["Buggy/Archive/Sub"])

    def test_path_and_id_for_one_file_count_once(self):
        target = FakeFile("a", self.archive)
        result, problems, _ = plan(self.project, ["Archive/a", target.id, "archive/A.f3d"], find=lookup_for(target))
        self.assertEqual(problems, [])
        self.assertEqual(result["summary"]["files"], 1)

    def test_outside_link_lists_who_links_it_with_path_and_id(self):
        target = FakeFile("a", self.archive)
        keeper = FakeFile("keeper", self.root)
        target.parents = [keeper]
        result, problems, _ = plan(self.project, ["Archive/a"])
        self.assertEqual(kinds(problems), ["linked_from_outside"])
        self.assertEqual(problems[0]["linked_from"], [{"path": "Buggy/keeper", "id": keeper.id}])
        self.assertEqual(result["outside_links"][0]["id"], target.id)
        self.assertEqual(result["summary"]["files_linked_from_outside"], 1)

    def test_adding_the_linker_to_the_set_clears_the_refusal(self):
        target = FakeFile("a", self.archive)
        linker = FakeFile("b", self.archive)
        target.parents = [linker]
        _, problems, work = plan(self.project, ["Archive/a", "Archive/b"])
        self.assertEqual(problems, [])
        self.assertEqual(list(work), [linker.id, target.id])

    def test_linker_with_unreadable_path_still_shows_its_id(self):
        target = FakeFile("a", self.archive)
        linker = mock.Mock(id="urn:elsewhere")
        type(linker).parentFolder = mock.PropertyMock(side_effect=RuntimeError("no access"))
        target.parents = [linker]
        _, problems, _ = plan(self.project, ["Archive/a"])
        self.assertEqual(problems[0]["linked_from"], [{"path": None, "id": "urn:elsewhere"}])

    def test_open_file_refused(self):
        target = FakeFile("a", self.archive)
        _, problems, _ = plan(self.project, ["Archive/a"],
                              opened={target.id: {"document": "a v3", "visible": False}})
        self.assertEqual(kinds(problems), ["open_in_fusion"])
        self.assertFalse(problems[0]["visible"])

    def test_ambiguous_path_lists_every_candidate(self):
        one = FakeFile("dup", self.archive, fid="urn:1")
        two = FakeFile("DUP", self.archive, fid="urn:2")
        _, problems, _ = plan(self.project, ["Archive/dup"])
        self.assertEqual(kinds(problems), ["ambiguous"])
        self.assertEqual([c["id"] for c in problems[0]["candidates"]], [one.id, two.id])

    def test_ambiguous_folder_in_a_path(self):
        FakeFolder("Twin", parent=self.root)
        FakeFolder("TWIN", parent=self.root)
        _, problems, _ = plan(self.project, ["twin/a"])
        self.assertEqual(kinds(problems), ["ambiguous"])

    def test_missing_path_and_id(self):
        _, problems, _ = plan(self.project, ["Archive/nope", "Nowhere/a", "urn:gone"])
        self.assertEqual(kinds(problems), ["not_found"] * 3)

    def test_lookup_that_raises_is_not_found(self):
        def boom(_):
            raise RuntimeError("bad id")

        _, problems, _ = plan(self.project, ["urn:x"], find=boom)
        self.assertEqual(kinds(problems), ["not_found"])

    def test_id_from_another_project_refused(self):
        other = FakeProject("Other", "a.2")
        stranger = FakeFile("x", other.rootFolder)
        _, problems, _ = plan(self.project, [stranger.id], find=lookup_for(stranger))
        self.assertEqual(kinds(problems), ["wrong_project"])

    def test_unreadable_links_refuse_rather_than_assume_none(self):
        target = FakeFile("a", self.archive)
        target.parents = RuntimeError("timeout")
        _, problems, _ = plan(self.project, ["Archive/a"])
        self.assertEqual(kinds(problems), ["links_unreadable"])

    def test_running_out_of_time_refuses_an_incomplete_plan(self):
        FakeFile("a", self.archive)
        _, problems, _ = plan(self.project, folder=self.archive, out_of_time=lambda: True)
        self.assertEqual(kinds(problems), ["time_budget"])

    def test_every_problem_is_reported_together(self):
        target = FakeFile("a", self.archive)
        target.parents = [FakeFile("keeper", self.root)]
        _, problems, _ = plan(self.project, ["Archive/a", "Archive/nope"],
                              opened={target.id: {"document": "a", "visible": True}})
        self.assertEqual(sorted(kinds(problems)), ["linked_from_outside", "not_found", "open_in_fusion"])

    def test_cycle_is_reported_and_kept_for_not_attempted(self):
        a, b = FakeFile("a", self.archive), FakeFile("b", self.archive)
        a.parents, b.parents = [b], [a]
        result, problems, work = plan(self.project, folder=self.archive)
        self.assertEqual(problems, [])
        self.assertEqual({row["id"] for row in result["unorderable"]}, {a.id, b.id})
        self.assertEqual(set(work), {a.id, b.id})


class OpenFilesTests(unittest.TestCase):
    def test_hidden_counted_unsaved_skipped_unreadable_named(self):
        hidden = mock.Mock(isVisible=False, dataFile=mock.Mock(id="urn:1"))
        hidden.name = "hidden"
        unsaved = mock.Mock(dataFile=None)
        broken = mock.Mock()
        broken.name = "broken"
        type(broken).dataFile = mock.PropertyMock(side_effect=RuntimeError("x"))
        opened, unreadable = df.open_file_ids([hidden, unsaved, broken])
        self.assertEqual(opened, {"urn:1": {"document": "hidden", "visible": False}})
        self.assertEqual(unreadable, ["broken"])


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.project = FakeProject()
        self.folder = FakeFolder("Archive", parent=self.project.rootFolder)

    def file(self, name, **kwargs):
        return FakeFile(name, self.folder, **kwargs)

    def test_parents_first_with_all_deleted(self):
        parent, child = self.file("p"), self.file("c")
        child.parents = [parent]
        calls = []
        parent.on_delete = lambda: calls.append("p")
        child.on_delete = lambda: calls.append("c")
        outcome = run([child, parent])  # wrong order on purpose: the rounds must correct it
        self.assertEqual(calls, ["p", "c"])
        self.assertEqual([r["id"] for r in outcome["deleted"]], [parent.id, child.id])
        self.assertEqual(outcome["failed"], [])
        self.assertEqual(outcome["not_attempted"], [])

    def test_failure_is_per_file_with_the_server_reason_and_data_panel_hint(self):
        reader = FakeReader("old noise\n" + failure_lines(urn=URN, request="stale"))
        bad = self.file("bad", fid=URN, error=RuntimeError("2 : InternalValidationError : res"))
        good = self.file("good")
        bad.on_delete = lambda: setattr(reader, "text", reader.text + failure_lines(request="fresh"))
        outcome = run([bad, good], reader=reader)
        self.assertEqual([r["id"] for r in outcome["deleted"]], [good.id])
        failure = outcome["failed"][0]
        self.assertEqual(failure["id"], URN)
        self.assertEqual(failure["reason"], "The object being acted on has relationships.")
        self.assertEqual(failure["server"]["error_code"], "GEN_RELATED_OBJECTS")
        self.assertTrue(failure["server"]["request_id"].endswith("fresh"))
        self.assertIn("Data Panel", failure["hint"])
        self.assertIn("InternalValidationError", failure["error"])

    def test_stale_log_entry_for_the_same_file_is_not_reported(self):
        bad = self.file("bad", fid=URN, error=RuntimeError("2 : InternalValidationError : res"))
        outcome = run([bad], reader=FakeReader(failure_lines()))
        failure = outcome["failed"][0]
        self.assertIsNone(failure["server"])
        self.assertIn("InternalValidationError", failure["reason"])
        self.assertNotIn("hint", failure)

    def test_returning_false_is_a_failure(self):
        outcome = run([self.file("a", result=False)])
        self.assertEqual(outcome["deleted"], [])
        self.assertIn("returned false", outcome["failed"][0]["error"])

    def test_child_of_a_failed_parent_is_left_alone(self):
        parent = self.file("p", error=RuntimeError("no"))
        child, free = self.file("c"), self.file("free")
        child.parents = [parent]
        outcome = run([parent, child, free])
        self.assertEqual(child.delete_calls, 0)
        self.assertEqual([r["id"] for r in outcome["deleted"]], [free.id])
        row = outcome["not_attempted"][0]
        self.assertEqual(row["id"], child.id)
        self.assertEqual(row["linked_from"][0]["id"], parent.id)

    def test_cached_reference_to_a_deleted_parent_does_not_block(self):
        parent, child = self.file("p"), self.file("c")
        child.parents = [parent]  # the fake never clears this, like a stale cache
        self.assertEqual(len(run([parent, child])["deleted"]), 2)

    def test_cycle_is_not_attempted(self):
        a, b = self.file("a"), self.file("b")
        a.parents, b.parents = [b], [a]
        outcome = run([a, b])
        self.assertEqual(a.delete_calls + b.delete_calls, 0)
        self.assertEqual(len(outcome["not_attempted"]), 2)

    def test_new_outside_link_stops_that_file_only(self):
        target, other = self.file("a"), self.file("b")
        newcomer = FakeFile("new", self.project.rootFolder)
        target.parents = [newcomer]
        outcome = run([target, other])
        self.assertEqual(target.delete_calls, 0)
        self.assertEqual(outcome["not_attempted"][0]["linked_from"][0]["id"], newcomer.id)
        self.assertEqual([r["id"] for r in outcome["deleted"]], [other.id])

    def test_file_opened_after_planning_is_left_alone(self):
        target = self.file("a")
        outcome = run([target], opened={target.id: {"document": "a", "visible": True}})
        self.assertEqual(target.delete_calls, 0)
        self.assertIn("opened", outcome["not_attempted"][0]["reason"])

    def test_file_gone_since_planning(self):
        target = self.file("a")
        outcome = run([target], lookup=lambda i: None)
        self.assertEqual(target.delete_calls, 0)
        self.assertIn("no longer found", outcome["not_attempted"][0]["reason"])

    def test_recheck_failure_leaves_the_file_alone_and_continues(self):
        broken, fine = self.file("a"), self.file("b")
        broken.parents = RuntimeError("timeout")
        outcome = run([broken, fine])
        self.assertEqual(broken.delete_calls, 0)
        self.assertIn("re-check failed", outcome["not_attempted"][0]["reason"])
        self.assertEqual([r["id"] for r in outcome["deleted"]], [fine.id])

    def test_time_budget_reports_the_rest(self):
        first, second = self.file("a"), self.file("b")
        checks = iter([False, True])
        outcome = run([first, second], out_of_time=lambda: next(checks))
        self.assertEqual([r["id"] for r in outcome["deleted"]], [first.id])
        self.assertEqual(second.delete_calls, 0)
        self.assertIn("time_budget", outcome["not_attempted"][0]["reason"])


class RemoveFolderTests(unittest.TestCase):
    def setUp(self):
        self.project = FakeProject()
        self.folder = FakeFolder("Archive", parent=self.project.rootFolder)

    def test_removed_when_empty(self):
        result = df.remove_folder(self.folder, "Buggy/Archive", set())
        self.assertTrue(result["removed"])
        self.assertTrue(self.folder.deleted)

    def test_stale_listing_of_deleted_files_does_not_block(self):
        stale = FakeFile("a", self.folder)
        self.assertTrue(df.remove_folder(self.folder, "p", {stale.id})["removed"])

    def test_remaining_file_keeps_the_folder(self):
        FakeFile("a", self.folder)
        result = df.remove_folder(self.folder, "p", set())
        self.assertFalse(result["removed"])
        self.assertFalse(self.folder.deleted)
        self.assertIn("1 files", result["reason"])

    def test_subfolder_keeps_the_folder(self):
        FakeFolder("Sub", parent=self.folder)
        self.assertFalse(df.remove_folder(self.folder, "p", set())["removed"])
        self.assertFalse(self.folder.deleted)

    def test_error_is_reported_not_raised(self):
        self.folder.delete_error = RuntimeError("nope")
        result = df.remove_folder(self.folder, "p", set())
        self.assertFalse(result["removed"])
        self.assertIn("nope", result["reason"])


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.project = FakeProject()
        self.folder = FakeFolder("Archive", parent=self.project.rootFolder)
        self.parent = FakeFile("p", self.folder)
        self.child = FakeFile("c", self.folder)
        self.child.parents = [self.parent]
        files = {f.id: f for f in (self.parent, self.child)}
        self.app = mock.Mock(documents=[])
        self.app.data.dataProjects = [self.project]
        self.app.data.findFileById = files.get
        patcher = mock.patch.object(df, "get_app", return_value=self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        log_patch = mock.patch.object(df, "newest_log", return_value=None)
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def call(self, **arguments):
        result = df.delete_files(dict({"project": "Buggy"}, **arguments))
        return result, json.loads(result["content"][0]["text"])

    def test_dry_run_deletes_nothing(self):
        result, body = self.call(folder="Archive", include_empty_folder=True)
        self.assertFalse(result["isError"])
        self.assertFalse(body["changed"])
        self.assertEqual(self.parent.delete_calls + self.child.delete_calls, 0)
        self.assertEqual([r["id"] for r in body["plan"]["order"]], [self.parent.id, self.child.id])
        self.assertFalse(self.folder.deleted)

    def test_real_run_deletes_and_removes_the_folder(self):
        result, body = self.call(folder="Archive", include_empty_folder=True, dry_run=False, confirm=True)
        self.assertFalse(result["isError"])
        self.assertTrue(body["complete"])
        self.assertEqual(len(body["deleted"]), 2)
        self.assertTrue(body["folder"]["removed"])
        self.assertIn("trash", body["trash"])

    def test_folder_is_kept_when_a_file_failed(self):
        self.child.error = RuntimeError("2 : InternalValidationError : res")
        result, body = self.call(folder="Archive", include_empty_folder=True, dry_run=False, confirm=True)
        self.assertTrue(result["isError"])
        self.assertTrue(body["changed"])
        self.assertFalse(body["complete"])
        self.assertFalse(body["folder"]["removed"])
        self.assertFalse(self.folder.deleted)

    def test_open_file_refuses_the_whole_call(self):
        doc = mock.Mock(isVisible=True, dataFile=mock.Mock(id=self.child.id))
        doc.name = "c"
        self.app.documents = [doc]
        result, body = self.call(folder="Archive", dry_run=False, confirm=True)
        self.assertTrue(result["isError"])
        self.assertTrue(body["refused"])
        self.assertEqual(self.parent.delete_calls + self.child.delete_calls, 0)

    def test_outside_link_refuses_but_dry_run_still_shows_the_plan(self):
        self.parent.parents = [FakeFile("keeper", self.project.rootFolder)]
        _, body = self.call(folder="Archive")
        self.assertTrue(body["refused"])
        self.assertEqual(len(body["plan"]["order"]), 2)
        self.assertEqual(body["plan"]["outside_links"][0]["linked_from"][0]["path"], "Buggy/keeper")

    def test_missing_folder_refused(self):
        result, body = self.call(folder="Nope")
        self.assertTrue(result["isError"])
        self.assertEqual(kinds(body["problems"]), ["not_found"])

    def test_missing_project(self):
        result = df.delete_files({"project": "Nope", "files": ["a"]})
        self.assertTrue(result["isError"])
        self.assertIn("No project", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
