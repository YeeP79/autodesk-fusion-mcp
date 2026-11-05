"""close_document and update_references: schema, validation, decisions and glue.

The fakes model the live behavior: close() on an instance held by an open parent
returns without closing, closing a parent takes its unheld children with it, and
isOutOfDate can stay False while the cloud is ahead.
"""

import json
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import document_lifecycle as lifecycle
from fusion_bridge import tool_surface


def info(name, file_id=None, version=1, *, active=False, modified=False, saved=True, references=(), file_name=None):
    return {"name": name, "data_file_id": file_id, "file_name": file_name, "version": version,
            "is_active": active, "is_modified": modified, "is_saved": saved, "references": set(references)}


def payload(response):
    return json.loads(response["content"][0]["text"])


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Collection:
    def __init__(self, items):
        self.items = items

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]


class FakeFile:
    def __init__(self, file_id, name, version=1, latest=None, folder="Parts"):
        self.id = file_id
        self.name = name
        self.versionNumber = version
        self.latestVersionNumber = version if latest is None else latest
        self.isReadOnly = False
        root = mock.Mock(isRoot=True, parentFolder=None)
        root.name = "root"
        self.parentFolder = mock.Mock(isRoot=False, parentFolder=root)
        self.parentFolder.name = folder


class FakeRef:
    def __init__(self, data_file, version=None, out_of_date=False, on_update=None):
        self.dataFile = data_file
        self.version = data_file.versionNumber if version is None else version
        self.isOutOfDate = out_of_date
        self.on_update = on_update
        self.calls = 0

    def getLatestVersion(self):
        self.calls += 1
        return self.on_update(self) if self.on_update else True


class FakeDoc:
    def __init__(self, app, data_file=None, *, name=None, modified=False, saved=True, refs=()):
        self.app = app
        self.dataFile = data_file
        self.name = name or f"{data_file.name} v{data_file.versionNumber}"
        self.isModified = modified
        self.isSaved = saved
        self.isUpToDate = True
        self.refs = list(refs)
        self.isValid = True
        self.close_args = []
        app.docs.append(self)

    @property
    def isActive(self):
        return self.app.activeDocument is self

    @property
    def documentReferences(self):
        return Collection(self.refs)

    @property
    def allDocumentReferences(self):
        return Collection(self.refs)

    def close(self, save_changes):
        self.close_args.append(save_changes)
        self.app.try_close(self)
        return True


class FakeApp:
    def __init__(self):
        self.docs = []
        self.activeDocument = None
        self.cloud = {}
        self.data = mock.Mock()
        self.data.findFileById.side_effect = lambda file_id: self.cloud.get(file_id)

    @property
    def documents(self):
        return Collection(self.docs)

    def _held(self, doc):
        file_id = doc.dataFile.id if doc.dataFile else None
        return any(ref.dataFile.id == file_id for other in self.docs if other is not doc for ref in other.refs)

    def try_close(self, doc):
        if self._held(doc):
            return
        self.docs.remove(doc)
        doc.isValid = False
        for ref in doc.refs:
            for child in [d for d in self.docs if d.dataFile and d.dataFile.id == ref.dataFile.id]:
                self.try_close(child)
        if self.activeDocument is doc:
            self.activeDocument = self.docs[0] if self.docs else None


class GlueCase(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.clock = FakeClock()
        for patcher in (mock.patch.object(lifecycle, "get_app", return_value=self.app),
                        mock.patch.object(lifecycle, "time", self.clock),
                        mock.patch.object(lifecycle, "_pump", self.pump),
                        mock.patch.object(lifecycle, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def pump(self):
        self.clock.now += 0.01


class SchemaTests(unittest.TestCase):
    def definition(self, name):
        return next(tool for tool in tool_surface.TOOL_DEFINITIONS if tool["name"] == name)

    def test_close_document_schema_is_strict(self):
        schema = self.definition("close_document")["inputSchema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["properties"]), {"name", "data_file_id", "all_instances", "discard_unsaved"})

    def test_update_references_schema_is_strict(self):
        schema = self.definition("update_references")["inputSchema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["properties"]), {"name", "data_file_id", "apply", "files"})
        self.assertNotIn("required", schema)


class ValidationTests(unittest.TestCase):
    def test_close_needs_exactly_one_selector(self):
        for arguments in ({}, {"name": "a", "data_file_id": "b"}, {"name": "  "}, {"name": 3}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                lifecycle.validate_close(arguments)

    def test_close_flags_default_to_false(self):
        self.assertEqual(lifecycle.validate_close({"name": "a"}), ("name", "a", False, False))

    def test_discard_must_be_a_real_boolean(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lifecycle.validate_close({"name": "a", "discard_unsaved": value})

    def test_close_rejects_unknown_arguments(self):
        with self.assertRaisesRegex(ValueError, "force"):
            lifecycle.validate_close({"name": "a", "force": True})

    def test_references_default_to_read_only_on_the_active_document(self):
        self.assertEqual(lifecycle.validate_references({}), (None, False, None))

    def test_references_reject_bad_files(self):
        for files in ([], "a", [""], [3]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                lifecycle.validate_references({"files": files})

    def test_handlers_validate_without_touching_fusion(self):
        with mock.patch.object(lifecycle, "get_app") as get_app:
            self.assertTrue(lifecycle.close_document({})["isError"])
            self.assertTrue(lifecycle.update_references({"apply": "yes"})["isError"])
        get_app.assert_not_called()


class PickTargetsTests(unittest.TestCase):
    def test_matches_document_or_file_name(self):
        infos = [info("Frame v3", "f", file_name="Frame"), info("Other v1", "o", file_name="Other")]
        self.assertEqual(lifecycle.pick_targets(infos, "name", "Frame")[0], [0])
        self.assertEqual(lifecycle.pick_targets(infos, "name", "Frame v3")[0], [0])

    def test_no_match_lists_what_is_open(self):
        targets, error = lifecycle.pick_targets([info("A", "a")], "name", "B")
        self.assertIsNone(targets)
        self.assertEqual(error["open_documents"][0]["name"], "A")

    def test_duplicate_instances_need_all_instances(self):
        infos = [info("K v12", "k", 12), info("K v11", "k", 11)]
        targets, error = lifecycle.pick_targets(infos, "data_file_id", "k")
        self.assertIsNone(targets)
        self.assertIn("all_instances", error["error"])
        self.assertEqual(lifecycle.pick_targets(infos, "data_file_id", "k", True)[0], [0, 1])

    def test_all_instances_never_spans_two_files(self):
        infos = [info("A v1", "x", file_name="A"), info("A v1", "y", file_name="A")]
        targets, error = lifecycle.pick_targets(infos, "name", "A", True)
        self.assertIsNone(targets)
        self.assertIn("data_file_id", error["error"])

    def test_two_never_saved_documents_are_different_files(self):
        infos = [info("Untitled", saved=False), info("Untitled", saved=False)]
        self.assertIsNone(lifecycle.pick_targets(infos, "name", "Untitled", True)[0])


class PreflightTests(unittest.TestCase):
    def test_unsaved_reasons(self):
        self.assertIsNone(lifecycle.unsaved_reason(info("a")))
        self.assertEqual(lifecycle.unsaved_reason(info("a", modified=True)), "modified")
        self.assertEqual(lifecycle.unsaved_reason(info("a", saved=False)), "never_saved")

    def test_unreadable_state_counts_as_unsaved(self):
        self.assertEqual(lifecycle.unsaved_reason(info("a", modified=None)), "state_unreadable")
        self.assertEqual(lifecycle.unsaved_reason(info("a", saved=None)), "state_unreadable")

    def test_modified_child_of_the_target_blocks(self):
        infos = [info("P", "p", references={"c"}), info("C", "c", modified=True), info("X", "x", modified=True)]
        self.assertEqual(lifecycle.close_preflight(infos, [0]),
                         [{"name": "C", "role": "linked", "reason": "modified"}])

    def test_modified_parent_of_the_target_does_not_block(self):
        infos = [info("C", "c"), info("P", "p", modified=True, references={"c"})]
        self.assertEqual(lifecycle.close_preflight(infos, [0]), [])


class ReconcileTests(unittest.TestCase):
    def keys(self, *infos):
        return [lifecycle.instance_key(item) for item in infos]

    def test_splits_closed_still_open_and_side_effects(self):
        before = [info("P", "p"), info("C", "c"), info("X", "x")]
        self.assertEqual(lifecycle.reconcile(before, self.keys(before[2]), [0]), ([0], [], [1]))

    def test_no_op_close_leaves_target_open(self):
        before = [info("K", "k"), info("P", "p")]
        self.assertEqual(lifecycle.reconcile(before, self.keys(*before), [0]), ([], [0], []))

    def test_counts_identical_instances(self):
        before = [info("K", "k", 12), info("K", "k", 12)]
        self.assertEqual(lifecycle.reconcile(before, self.keys(before[0]), [0, 1]), ([1], [0], []))

    def test_versions_tell_instances_apart(self):
        before = [info("K", "k", 12), info("K", "k", 11)]
        self.assertEqual(lifecycle.reconcile(before, self.keys(before[1]), [0, 1]), ([0], [1], []))

    def test_disappearance_is_charged_to_the_target_not_a_twin(self):
        before = [info("K", "k", 12), info("K", "k", 12)]
        self.assertEqual(lifecycle.reconcile(before, self.keys(before[0]), [1]), ([1], [], []))

    def test_never_saved_documents_key_by_name(self):
        before = [info("Untitled", saved=False)]
        self.assertEqual(lifecycle.reconcile(before, [], [0]), ([0], [], []))

    def test_new_document_is_ignored(self):
        before = [info("K", "k")]
        self.assertEqual(lifecycle.reconcile(before, [("new", 1)], [0]), ([0], [], []))


class CloseWatchTests(unittest.TestCase):
    def setUp(self):
        self.before = [info("P", "p"), info("C", "c")]
        self.keys = [lifecycle.instance_key(item) for item in self.before]

    def test_waits_for_quiet_after_the_target_goes(self):
        watch = lifecycle.CloseWatch(self.before, [0], 0.0, timeout_s=3.0, quiet_s=0.5)
        self.assertFalse(watch.observe(0.1, self.keys[1:]))
        self.assertFalse(watch.observe(0.4, self.keys[1:]))
        self.assertFalse(watch.observe(0.5, []))  # the child went late: quiet restarts
        self.assertFalse(watch.observe(0.9, []))
        self.assertTrue(watch.observe(1.0, []))

    def test_held_target_runs_to_the_timeout(self):
        watch = lifecycle.CloseWatch(self.before, [0], 0.0, timeout_s=3.0, quiet_s=0.5)
        self.assertFalse(watch.observe(2.9, self.keys))
        self.assertTrue(watch.observe(3.0, self.keys))


class HoldTests(unittest.TestCase):
    def test_names_direct_parents(self):
        infos = [info("K", "k"), info("P", "p", modified=True, references={"k"})]
        hold = lifecycle.describe_hold(infos, "k")
        self.assertEqual(hold["held_by"], [{"name": "P", "data_file_id": "p", "is_modified": True}])
        self.assertIn("'P'", hold["how_to_free"])

    def test_another_instance_of_the_file_is_not_a_holder(self):
        self.assertEqual(lifecycle.holders_of([info("K", "k", references={"k"})], "k"), [])

    def test_how_to_free_climbs_to_the_top_parent(self):
        infos = [info("K", "k"), info("Sub", "s", references={"k"}), info("Top", "t", references={"s", "k"})]
        hold = lifecycle.describe_hold(infos, "k")
        self.assertEqual([item["name"] for item in hold["held_by"]], ["Sub", "Top"])
        self.assertIn("'Top'", hold["how_to_free"])
        self.assertNotIn("'Sub'", hold["how_to_free"])

    def test_reference_cycle_terminates(self):
        infos = [info("A", "a", references={"b"}), info("B", "b", references={"a"})]
        self.assertEqual(lifecycle.top_parents(infos, [0]), [0])

    def test_no_holder_says_so(self):
        hold = lifecycle.describe_hold([info("K", "k")], "k")
        self.assertEqual(hold["held_by"], [])
        self.assertEqual(hold["how_to_free"], lifecycle.UNHELD_NOTE)

    def test_never_saved_document_has_no_holders(self):
        self.assertEqual(lifecycle.holders_of([info("A", None, references={None})], None), [])


class ActiveWarningTests(unittest.TestCase):
    def test_names_the_new_active_document(self):
        warning = lifecycle.active_warning([info("A", "a", active=True)], [0], "B")
        self.assertIn("'A'", warning)
        self.assertIn("'B' is active now", warning)

    def test_says_when_nothing_is_active(self):
        self.assertIn("no document is active", lifecycle.active_warning([info("A", "a", active=True)], [0], None))

    def test_silent_when_the_active_document_stayed(self):
        self.assertIsNone(lifecycle.active_warning([info("A", "a"), info("B", "b", active=True)], [0], "B"))


class CloseDocumentTests(GlueCase):
    def stock(self, version=12):
        return FakeFile("stock", "Stock", version)

    def test_closes_and_verifies(self):
        doc = FakeDoc(self.app, FakeFile("a", "A"))
        keep = FakeDoc(self.app, FakeFile("b", "B"))
        self.app.activeDocument = keep
        response = lifecycle.close_document({"name": "A"})
        result = payload(response)
        self.assertFalse(response["isError"])
        self.assertEqual(result["status"], "closed")
        self.assertEqual([item["name"] for item in result["closed"]], ["A v1"])
        self.assertEqual(result["still_open"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(doc.close_args, [False])

    def test_held_instance_gets_one_attempt_and_names_its_parent(self):
        stock = self.stock()
        held = FakeDoc(self.app, stock)
        parent = FakeDoc(self.app, FakeFile("p", "Parent"), refs=[FakeRef(stock)])
        self.app.activeDocument = parent
        response = lifecycle.close_document({"data_file_id": "stock"})
        result = payload(response)
        self.assertTrue(response["isError"])
        self.assertEqual(result["status"], "not_closed")
        self.assertEqual(held.close_args, [False])  # one attempt, never a loop
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["still_open"][0]["held_by"][0]["name"], "Parent v1")
        self.assertIn("'Parent v1'", result["still_open"][0]["how_to_free"])

    def test_closing_a_parent_reports_its_children(self):
        stock = self.stock()
        FakeDoc(self.app, stock)
        FakeDoc(self.app, FakeFile("p", "Parent"), refs=[FakeRef(stock)])
        response = lifecycle.close_document({"data_file_id": "p"})
        result = payload(response)
        self.assertFalse(response["isError"])
        self.assertEqual([item["name"] for item in result["also_closed"]], ["Stock v12"])

    def test_closing_the_active_document_warns_and_names_the_new_one(self):
        doc = FakeDoc(self.app, FakeFile("a", "A"))
        FakeDoc(self.app, FakeFile("b", "B"))
        self.app.activeDocument = doc
        result = payload(lifecycle.close_document({"name": "A"}))
        self.assertEqual(result["active_document"], "B v1")
        self.assertIn("'B v1' is active now", result["warnings"][0])

    def test_active_child_closed_with_its_parent_also_warns(self):
        stock = self.stock()
        child = FakeDoc(self.app, stock)
        FakeDoc(self.app, FakeFile("p", "Parent"), refs=[FakeRef(stock)])
        self.app.activeDocument = child
        result = payload(lifecycle.close_document({"data_file_id": "p"}))
        self.assertIn("'Stock v12' was the active document", result["warnings"][0])

    def test_refuses_modified_document_without_attempting(self):
        doc = FakeDoc(self.app, FakeFile("a", "A"), modified=True)
        response = lifecycle.close_document({"name": "A"})
        result = payload(response)
        self.assertTrue(response["isError"])
        self.assertEqual(result["status"], "refused")
        self.assertIs(result["attempted"], False)
        self.assertEqual(doc.close_args, [])

    def test_refuses_never_saved_document(self):
        doc = FakeDoc(self.app, name="Untitled", saved=False)
        self.assertEqual(payload(lifecycle.close_document({"name": "Untitled"}))["unsaved"][0]["reason"], "never_saved")
        self.assertEqual(doc.close_args, [])

    def test_refuses_when_a_linked_child_would_lose_edits(self):
        stock = self.stock()
        FakeDoc(self.app, stock, modified=True)
        parent = FakeDoc(self.app, FakeFile("p", "Parent"), refs=[FakeRef(stock)])
        result = payload(lifecycle.close_document({"data_file_id": "p"}))
        self.assertEqual(result["unsaved"], [{"name": "Stock v12", "role": "linked", "reason": "modified"}])
        self.assertEqual(parent.close_args, [])

    def test_discard_closes_and_reports_what_was_lost(self):
        doc = FakeDoc(self.app, FakeFile("a", "A"), modified=True)
        result = payload(lifecycle.close_document({"name": "A", "discard_unsaved": True}))
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["discarded_changes"], ["A v1"])
        self.assertEqual(doc.close_args, [False])

    def test_all_instances_is_partial_when_one_is_held(self):
        # The fake's parent holds every instance of a file; live, one instance can be
        # free while another is held, so let close() work on the free one.
        stock = self.stock()
        free = FakeDoc(self.app, FakeFile("stock", "Stock", 11))
        held = FakeDoc(self.app, stock)
        FakeDoc(self.app, FakeFile("p", "Parent"), refs=[FakeRef(stock)])

        def try_close(doc, original=self.app.try_close):
            if doc is free:
                self.app.docs.remove(doc)
                doc.isValid = False
            else:
                original(doc)

        self.app.try_close = try_close
        response = lifecycle.close_document({"data_file_id": "stock", "all_instances": True})
        result = payload(response)
        self.assertTrue(response["isError"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["version"] for item in result["closed"]], [11])
        self.assertEqual([item["version"] for item in result["still_open"]], [12])
        self.assertEqual((free.close_args, held.close_args), ([False], [False]))

    def test_instance_gone_with_an_earlier_one_is_not_closed_twice(self):
        first = FakeDoc(self.app, FakeFile("k", "K", 12))
        second = FakeDoc(self.app, FakeFile("k", "K", 11))

        def try_close(doc):
            for item in (first, second):
                if item in self.app.docs:
                    self.app.docs.remove(item)
                item.isValid = False

        self.app.try_close = try_close
        result = payload(lifecycle.close_document({"data_file_id": "k", "all_instances": True}))
        self.assertEqual(result["status"], "closed")
        self.assertEqual(second.close_args, [])
        self.assertEqual(result["attempts"][1]["skipped"], "already_gone")

    def test_raising_close_is_reported_and_verified_anyway(self):
        doc = FakeDoc(self.app, FakeFile("a", "A"))
        doc.close = mock.Mock(side_effect=RuntimeError("busy"))
        response = lifecycle.close_document({"name": "A"})
        result = payload(response)
        self.assertTrue(response["isError"])
        self.assertIn("busy", result["attempts"][0]["error"])
        self.assertEqual(result["still_open"][0]["how_to_free"], lifecycle.UNHELD_NOTE)

    def test_return_value_is_not_trusted(self):
        doc = FakeDoc(self.app, FakeFile("a", "A"))
        doc.close = mock.Mock(return_value=True)  # says yes, stays open
        self.assertEqual(payload(lifecycle.close_document({"name": "A"}))["status"], "not_closed")
        doc.close.assert_called_once_with(False)

    def test_verification_gives_up_on_time(self):
        doc = FakeDoc(self.app, FakeFile("a", "A"))
        doc.close = mock.Mock(return_value=False)
        started = self.clock.now
        lifecycle.close_document({"name": "A"})
        self.assertLess(self.clock.now - started, lifecycle.CLOSE_VERIFY_TIMEOUT_S + 1)


class ClassifyTests(unittest.TestCase):
    def test_flagged_wins(self):
        self.assertEqual(lifecycle.classify_reference(True, 12, 12), lifecycle.OUT_OF_DATE)
        self.assertEqual(lifecycle.classify_reference(True, None, None), lifecycle.OUT_OF_DATE)

    def test_cloud_ahead_but_unflagged_is_stale(self):
        self.assertEqual(lifecycle.classify_reference(False, 11, 12), lifecycle.STALE_NOT_FLAGGED)
        self.assertEqual(lifecycle.classify_reference(None, 11, 12), lifecycle.STALE_NOT_FLAGGED)

    def test_same_version_is_current(self):
        self.assertEqual(lifecycle.classify_reference(False, 12, 12), lifecycle.CURRENT)

    def test_loaded_ahead_of_a_stale_latest_is_not_flagged_as_behind(self):
        self.assertEqual(lifecycle.classify_reference(False, 12, 11), lifecycle.CURRENT)

    def test_unreadable_versions_are_unknown_not_current(self):
        self.assertEqual(lifecycle.classify_reference(False, None, 12), lifecycle.UNKNOWN)
        self.assertEqual(lifecycle.classify_reference(False, 12, None), lifecycle.UNKNOWN)


class SelectionTests(unittest.TestCase):
    REFS = [{"name": "A", "data_file_id": "1", "status": "current"},
            {"name": "B", "data_file_id": "2", "status": "out_of_date"},
            {"name": "B", "data_file_id": "3", "status": "stale_not_flagged"},
            {"name": "C", "data_file_id": None, "status": "unknown"}]

    def test_all_by_default(self):
        self.assertEqual(lifecycle.resolve_files(self.REFS, None)[0], [0, 1, 2, 3])

    def test_by_name_or_id(self):
        self.assertEqual(lifecycle.resolve_files(self.REFS, ["A", "3"])[0], [0, 2])

    def test_unknown_file_is_refused_whole(self):
        selected, error = lifecycle.resolve_files(self.REFS, ["A", "nope"])
        self.assertIsNone(selected)
        self.assertIn("nope", error["error"])

    def test_name_shared_by_two_files_is_refused(self):
        selected, error = lifecycle.resolve_files(self.REFS, ["B"])
        self.assertIsNone(selected)
        self.assertEqual(len(error["references"]), 2)

    def test_two_references_to_one_file_are_both_chosen(self):
        refs = [{"name": "A", "data_file_id": "1"}, {"name": "A", "data_file_id": "1"}]
        self.assertEqual(lifecycle.resolve_files(refs, ["A"])[0], [0, 1])

    def test_current_is_skipped_everything_else_is_attempted(self):
        to_update, skipped = lifecycle.plan_updates(self.REFS, [0, 1, 2, 3])
        self.assertEqual(to_update, [1, 2, 3])
        self.assertEqual(skipped, [{"name": "A", "reason": "already_latest"}])


class JudgeUpdateTests(unittest.TestCase):
    def test_version_moved(self):
        self.assertEqual(lifecycle.judge_update(11, [12], True), lifecycle.UPDATED)

    def test_true_with_no_movement_is_no_change(self):
        self.assertEqual(lifecycle.judge_update(11, [11], True), lifecycle.NO_CHANGE)

    def test_version_outranks_false_and_errors(self):
        self.assertEqual(lifecycle.judge_update(11, [12], False), lifecycle.UPDATED)
        self.assertEqual(lifecycle.judge_update(11, [12], None, "boom"), lifecycle.UPDATED)

    def test_failure(self):
        self.assertEqual(lifecycle.judge_update(11, [11], False), lifecycle.FAILED)
        self.assertEqual(lifecycle.judge_update(11, [11], None, "boom"), lifecycle.FAILED)

    def test_unreadable_versions_are_unverified(self):
        self.assertEqual(lifecycle.judge_update(None, [12], True), lifecycle.UNVERIFIED)
        self.assertEqual(lifecycle.judge_update(11, [], True), lifecycle.UNVERIFIED)


class AdviceTests(unittest.TestCase):
    def test_nothing_to_say(self):
        self.assertEqual(lifecycle.reference_advice("P", [{"name": "A", "status": "current"}], [], False), [])

    def test_says_to_save_after_an_update(self):
        updates = [{"name": "A", "status": "updated"}]
        advice = lifecycle.reference_advice("P", [{"name": "A", "status": "current"}], updates, True)
        self.assertEqual(advice, [lifecycle.SAVE_FIX.format(document="'P'")])

    def test_no_save_nag_for_a_listing_of_a_modified_document(self):
        self.assertEqual(lifecycle.reference_advice("P", [], [], True), [])

    def test_stale_suggests_reopening_without_repeating_itself(self):
        refs = [{"name": "A", "status": "stale_not_flagged"}]
        advice = lifecycle.reference_advice("P", refs, [{"name": "A", "status": "no_change"}], False)
        self.assertEqual(len(advice), 1)
        self.assertIn("reopen", advice[0])

    def test_flagged_reference_that_would_not_move_suggests_reopening(self):
        refs = [{"name": "A", "status": "out_of_date"}]
        advice = lifecycle.reference_advice("P", refs, [{"name": "A", "status": "failed"}], False)
        self.assertIn("changed nothing for 'A'", advice[0])


class UpdateReferencesTests(GlueCase):
    def setup_parent(self, ref_kwargs=None, *, loaded=11, latest=12, on_update="bump"):
        """A parent linking one child that is *loaded* locally and *latest* in the cloud."""
        child = FakeFile("c", "Child", loaded, latest=loaded)
        self.app.cloud["c"] = FakeFile("c", "Child", latest)
        hook = self.bump if on_update == "bump" else on_update
        self.ref = FakeRef(child, on_update=hook, **(ref_kwargs or {}))
        self.parent = FakeDoc(self.app, FakeFile("p", "Parent"), refs=[self.ref])
        self.app.activeDocument = self.parent

    def bump(self, ref):
        ref.version = self.app.cloud["c"].versionNumber
        ref.isOutOfDate = False
        self.parent.isModified = True
        return True

    def test_listing_changes_nothing_by_default(self):
        self.setup_parent({"out_of_date": True})
        response = lifecycle.update_references({})
        result = payload(response)
        self.assertFalse(response["isError"])
        self.assertIs(result["applied"], False)
        self.assertEqual(self.ref.calls, 0)
        self.assertEqual(result["would_update"], ["Child"])
        self.assertEqual(result["references"][0], {
            "name": "Child", "data_file_id": "c", "path": "/Parts/Child", "loaded_version": 11,
            "latest_version": 12, "is_out_of_date": True, "status": "out_of_date"})

    def test_stale_but_unflagged_is_named_and_suggests_reopening(self):
        self.setup_parent()
        result = payload(lifecycle.update_references({}))
        self.assertEqual(result["stale_not_flagged"], ["Child"])
        self.assertEqual(result["out_of_date"], [])
        self.assertIn("reopen", result["recommendation"][0])

    def test_cached_latest_alone_does_not_hide_the_cloud(self):
        self.setup_parent()
        self.assertEqual(self.ref.dataFile.latestVersionNumber, 11)
        self.assertEqual(payload(lifecycle.update_references({}))["references"][0]["latest_version"], 12)

    def test_apply_updates_verifies_and_says_to_save(self):
        self.setup_parent({"out_of_date": True})
        response = lifecycle.update_references({"apply": True})
        result = payload(response)
        self.assertFalse(response["isError"])
        self.assertEqual(self.ref.calls, 1)
        update = result["updates"][0]
        self.assertEqual((update["status"], update["version_before"], update["version_after"]), ("updated", 11, 12))
        self.assertIs(result["document_modified"], True)
        self.assertIn("save it", result["recommendation"][0])
        self.assertEqual(result["references"][0]["status"], "current")

    def test_true_without_a_version_change_is_an_error_not_a_success(self):
        self.setup_parent({"out_of_date": True}, on_update=lambda ref: True)
        response = lifecycle.update_references({"apply": True})
        result = payload(response)
        self.assertTrue(response["isError"])
        self.assertEqual(result["updates"][0]["status"], "no_change")
        self.assertEqual(self.ref.calls, 1)  # waiting is reading, not retrying

    def test_stale_reference_gets_one_attempt_then_a_reopen_suggestion(self):
        self.setup_parent(on_update=lambda ref: True)
        result = payload(lifecycle.update_references({"apply": True}))
        self.assertEqual(self.ref.calls, 1)
        self.assertEqual(result["stale_not_flagged"], ["Child"])
        self.assertIn("reopen", result["recommendation"][0])

    def test_current_reference_is_not_touched(self):
        self.setup_parent(loaded=12, latest=12)
        result = payload(lifecycle.update_references({"apply": True}))
        self.assertEqual(self.ref.calls, 0)
        self.assertEqual(result["skipped"], [{"name": "Child", "reason": "already_latest"}])
        self.assertIs(result["document_modified"], False)

    def test_files_limits_the_update(self):
        self.setup_parent({"out_of_date": True})
        other = FakeRef(FakeFile("o", "Other", 1, latest=2), out_of_date=True)
        self.parent.refs.append(other)
        result = payload(lifecycle.update_references({"apply": True, "files": ["Child"]}))
        self.assertEqual((self.ref.calls, other.calls), (1, 0))
        self.assertEqual([item["name"] for item in result["updates"]], ["Child"])

    def test_unknown_file_changes_nothing(self):
        self.setup_parent({"out_of_date": True})
        response = lifecycle.update_references({"apply": True, "files": ["Nope"]})
        self.assertTrue(response["isError"])
        self.assertEqual(self.ref.calls, 0)

    def test_raising_update_is_reported_per_reference(self):
        def boom(ref):
            raise RuntimeError("no network")

        self.setup_parent({"out_of_date": True}, on_update=boom)
        other = FakeRef(FakeFile("o", "Other", 1, latest=2), out_of_date=True,
                        on_update=lambda ref: setattr(ref, "version", 2) or True)
        self.parent.refs.append(other)
        response = lifecycle.update_references({"apply": True})
        statuses = {item["name"]: item["status"] for item in payload(response)["updates"]}
        self.assertTrue(response["isError"])
        self.assertEqual(statuses, {"Child": "failed", "Other": "updated"})

    def test_reference_replaced_by_the_update_is_still_verified(self):
        self.setup_parent({"out_of_date": True}, on_update=None)

        def replace(ref):
            self.parent.refs[0] = FakeRef(FakeFile("c", "Child", 12))
            ref.dataFile = None  # the held object goes bad
            return True

        self.ref.on_update = replace
        self.assertEqual(payload(lifecycle.update_references({"apply": True}))["updates"][0]["status"], "updated")

    def test_later_references_are_re_found_after_an_earlier_update(self):
        self.setup_parent({"out_of_date": True})
        stale_held = FakeRef(FakeFile("o", "Other", 1, latest=2), out_of_date=True)
        fresh = FakeRef(FakeFile("o", "Other", 1, latest=2), out_of_date=True,
                        on_update=lambda ref: setattr(ref, "version", 2) or True)
        self.parent.refs.append(stale_held)
        original = self.ref.on_update

        def swap(ref):
            self.parent.refs[1] = fresh
            return original(ref)

        self.ref.on_update = swap
        lifecycle.update_references({"apply": True})
        self.assertEqual((stale_held.calls, fresh.calls), (0, 1))

    def test_time_budget_stops_further_calls(self):
        self.setup_parent({"out_of_date": True})
        other = FakeRef(FakeFile("o", "Other", 1, latest=2), out_of_date=True)
        self.parent.refs.append(other)
        original = self.ref.on_update

        def slow(ref):
            self.clock.now += lifecycle.APPLY_BUDGET_S + 1
            return original(ref)

        self.ref.on_update = slow
        response = lifecycle.update_references({"apply": True})
        statuses = {item["name"]: item["status"] for item in payload(response)["updates"]}
        self.assertEqual(statuses, {"Child": "updated", "Other": "not_attempted"})
        self.assertEqual(other.calls, 0)
        self.assertTrue(response["isError"])

    def test_lookup_budget_falls_back_and_warns(self):
        self.setup_parent()
        late = FakeFile("o", "Other", 1, latest=2)
        self.parent.refs.append(FakeRef(late))

        def slow_lookup(file_id):
            self.clock.now += lifecycle.LOOKUP_BUDGET_S + 1
            return self.app.cloud.get(file_id)

        self.app.data.findFileById.side_effect = slow_lookup
        result = payload(lifecycle.update_references({}))
        self.assertEqual(self.app.data.findFileById.call_count, 1)
        self.assertEqual(result["references"][1]["latest_version"], 2)
        self.assertIsNone(result["references"][1]["path"])
        self.assertTrue(any("1 file(s)" in item for item in result["warnings"]))

    def test_cloud_lookups_happen_once_per_file(self):
        self.setup_parent({"out_of_date": True})
        lifecycle.update_references({"apply": True})
        self.assertEqual(self.app.data.findFileById.call_count, 1)

    def test_failed_lookup_uses_the_loaded_files_latest(self):
        self.setup_parent()
        self.app.data.findFileById.side_effect = RuntimeError("offline")
        self.ref.dataFile.latestVersionNumber = 12
        reference = payload(lifecycle.update_references({}))["references"][0]
        self.assertEqual((reference["latest_version"], reference["path"]), (12, None))

    def test_broken_link_is_unknown_and_does_not_crash(self):
        self.setup_parent()
        self.parent.refs = [mock.Mock(dataFile=None, version=None, isOutOfDate=False)]
        self.assertEqual(payload(lifecycle.update_references({}))["references"][0]["status"], "unknown")

    def test_differing_data_file_version_is_shown(self):
        self.setup_parent({"version": 10})
        reference = payload(lifecycle.update_references({}))["references"][0]
        self.assertEqual((reference["loaded_version"], reference["data_file_version"]), (10, 11))

    def test_no_active_document(self):
        response = lifecycle.update_references({})
        self.assertTrue(response["isError"])
        self.assertIn("No document is active", response["content"][0]["text"])

    def test_named_document_warns_when_not_active(self):
        self.setup_parent()
        self.app.activeDocument = FakeDoc(self.app, FakeFile("x", "X"))
        result = payload(lifecycle.update_references({"data_file_id": "p"}))
        self.assertEqual(result["document"], "Parent v1")
        self.assertIn("not the active document", result["warnings"][0])

    def test_named_duplicates_are_refused(self):
        self.setup_parent()
        FakeDoc(self.app, FakeFile("p", "Parent"))
        response = lifecycle.update_references({"data_file_id": "p", "apply": True})
        self.assertTrue(response["isError"])
        self.assertEqual(self.ref.calls, 0)

    def test_read_only_parent_warns(self):
        self.setup_parent()
        self.parent.dataFile.isReadOnly = True
        self.assertTrue(any("read-only" in item for item in payload(lifecycle.update_references({}))["warnings"]))

    def test_sub_assembly_references_are_counted(self):
        self.setup_parent()
        deep = [self.ref, FakeRef(FakeFile("d", "Deep"))]
        with mock.patch.object(FakeDoc, "allDocumentReferences", Collection(deep)):
            warnings = payload(lifecycle.update_references({}))["warnings"]
        self.assertTrue(any("1 more reference" in item for item in warnings))


if __name__ == "__main__":
    unittest.main()
