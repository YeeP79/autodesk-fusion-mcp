"""save_document / open_document: decision logic and handlers against fakes."""

import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import document_io as dio
from fusion_bridge import tool_surface


def _info(name, file_id="F1", version=3, active=False, modified=True, read_only=False, visible=True):
    data_file = None
    if file_id is not None:
        data_file = {"id": file_id, "name": name.rsplit(" v", 1)[0], "version_number": version,
                     "is_read_only": read_only, "project": "P", "folder": "Root"}
    return {"name": name, "is_active": active, "is_visible": visible, "is_modified": modified,
            "data_file": data_file}


# -- Fakes for the Fusion object model ----------------------------------------


class FakeCollection:
    def __init__(self, items):
        self._items = list(items)

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


class FakeDataFile:
    def __init__(self, file_id="F1", name="Knuckle", version=3, latest=None, read_only=False, ext="f3d"):
        self.id = file_id
        self.name = name
        self.versionNumber = version
        self.latestVersionNumber = latest if latest is not None else version
        self.isReadOnly = read_only
        self.fileExtension = ext
        self.parentProject = type("Project", (), {"name": "P"})()
        self.parentFolder = type("Folder", (), {"name": "Root"})()
        self.versions = FakeCollection([])


class FakeDocument:
    def __init__(self, data_file, active=False, modified=True, visible=True, on_save=None, app=None):
        self.dataFile = data_file
        self.name = f"{data_file.name} v{data_file.versionNumber}" if data_file else "Untitled"
        self.isActive = active
        self.isModified = modified
        self.isVisible = visible
        self._on_save = on_save
        self._app = app
        self.saved_with = None

    def save(self, description):
        self.saved_with = description
        if self._on_save:
            self._on_save(self)
        return True

    def activate(self):
        for doc in self._app.docs:
            doc.isActive = doc is self
        return True


class FakeCloud:
    def __init__(self, files, projects=()):
        self.files = {f.id: f for f in files}
        self.dataProjects = FakeCollection(projects)

    def findFileById(self, file_id):
        return self.files.get(file_id)


class FakeDocuments:
    def __init__(self, app):
        self._app = app
        self.opened = []

    @property
    def count(self):
        return len(self._app.docs)

    def item(self, i):
        return self._app.docs[i]

    def open(self, data_file, visible):
        doc = FakeDocument(data_file, active=False, modified=False, app=self._app)
        self._app.docs.append(doc)
        self.opened.append(data_file)
        return doc


class FakeApp:
    def __init__(self, docs, cloud):
        self.docs = docs
        for doc in docs:
            doc._app = self
        self.data = cloud
        self.documents = FakeDocuments(self)


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _patched(app, log_path=None):
    return [
        mock.patch.object(dio, "get_app", return_value=app),
        mock.patch.object(dio.adsk, "doEvents", create=True),
        mock.patch.object(dio.time, "sleep"),
        mock.patch.object(dio, "newest_app_log", return_value=log_path),
        mock.patch.object(dio, "log"),
    ]


class _Patches:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


# -- Schema ------------------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def _schema(self, name):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == name)["inputSchema"]

    def test_save_schema(self):
        schema = self._schema("save_document")
        self.assertEqual(schema["required"], ["description"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"description", "name", "data_file_id", "timeout_seconds"})
        self.assertLessEqual(schema["properties"]["timeout_seconds"]["maximum"], 110)

    def test_open_schema(self):
        schema = self._schema("open_document")
        self.assertNotIn("required", schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"data_file_id", "project", "path", "version"})

    def test_timeout_cap_inside_bridge_limit(self):
        from _addin_root import settings
        self.assertLess(dio.MAX_SAVE_TIMEOUT_S, settings.MCP_MAIN_THREAD_TIMEOUT)


# -- Validation --------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    def test_save_requires_description(self):
        for bad in ({}, {"description": ""}, {"description": "  "}, {"description": 5}):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                dio.validate_save(bad)

    def test_save_defaults(self):
        self.assertEqual(dio.validate_save({"description": " fix "}), ("fix", None, None, 60.0))

    def test_save_selectors(self):
        self.assertEqual(dio.validate_save({"description": "d", "name": "A"})[1:3], ("name", "A"))
        self.assertEqual(dio.validate_save({"description": "d", "data_file_id": "X"})[1:3], ("data_file_id", "X"))
        with self.assertRaises(ValueError):
            dio.validate_save({"description": "d", "name": "A", "data_file_id": "X"})
        with self.assertRaises(ValueError):
            dio.validate_save({"description": "d", "name": ""})

    def test_save_timeout_bounds(self):
        for bad in (0, -1, 111, "10", True):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                dio.validate_save({"description": "d", "timeout_seconds": bad})
        self.assertEqual(dio.validate_save({"description": "d", "timeout_seconds": 110})[3], 110.0)

    def test_unknown_argument(self):
        with self.assertRaises(ValueError):
            dio.validate_save({"description": "d", "force": True})
        with self.assertRaises(ValueError):
            dio.validate_open({"data_file_id": "X", "close_others": True})

    def test_open_selector_rules(self):
        self.assertEqual(dio.validate_open({"data_file_id": "X"}), ("X", None, None, None))
        self.assertEqual(dio.validate_open({"project": "P", "path": "/A/B/File/"}), (None, "P", ["A", "B", "File"], None))
        for bad in ({}, {"project": "P"}, {"path": "A"}, {"data_file_id": "X", "project": "P"},
                    {"project": "P", "path": "///"}, {"data_file_id": "X", "version": 0},
                    {"data_file_id": "X", "version": True}, {"data_file_id": "X", "version": "3"}):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                dio.validate_open(bad)

    def test_handlers_return_errors_without_fusion(self):
        with mock.patch.object(dio, "get_app", side_effect=AssertionError("touched Fusion")):
            self.assertTrue(dio.save_document({})["isError"])
            self.assertTrue(dio.open_document({"version": 2})["isError"])


# -- Pure decisions ----------------------------------------------------------


class InstanceSuffixTests(unittest.TestCase):
    def test_suffix(self):
        self.assertEqual(dio.instance_suffix("Knuckle - SD 2005-08 (stock)(1)"), 1)
        self.assertEqual(dio.instance_suffix("Knuckle (2) v12"), 2)
        self.assertIsNone(dio.instance_suffix("Knuckle - SD 2005-08 (stock) v12"))
        self.assertIsNone(dio.instance_suffix(None))


class PickTargetTests(unittest.TestCase):
    def test_active_default(self):
        infos = [_info("A v1"), _info("B v2", file_id="F2", active=True)]
        self.assertEqual(dio.pick_target(infos, None, None), (1, None))

    def test_no_active(self):
        index, error = dio.pick_target([_info("A v1")], None, None)
        self.assertIsNone(index)
        self.assertIn("No document is active", error["error"])

    def test_name_matches_document_or_file_name(self):
        infos = [_info("A v1", file_id="F1"), _info("B v2", file_id="F2")]
        self.assertEqual(dio.pick_target(infos, "name", "B")[0], 1)
        self.assertEqual(dio.pick_target(infos, "name", "B v2")[0], 1)

    def test_name_across_different_files_is_ambiguous(self):
        infos = [_info("A v1", file_id="F1"), _info("A v4", file_id="F9")]
        index, error = dio.pick_target(infos, "name", "A")
        self.assertIsNone(index)
        self.assertEqual(len(error["candidates"]), 2)

    def test_duplicates_prefer_active_then_newest(self):
        infos = [_info("A v11", version=11), _info("A v12", version=12), _info("A v12", version=12)]
        self.assertEqual(dio.pick_target(infos, "data_file_id", "F1")[0], 1)
        infos[0]["is_active"] = True
        self.assertEqual(dio.pick_target(infos, "data_file_id", "F1")[0], 0)

    def test_no_match(self):
        index, error = dio.pick_target([_info("A v1")], "data_file_id", "nope")
        self.assertIsNone(index)
        self.assertEqual(error["open_documents"][0]["data_file_id"], "F1")


class PreflightTests(unittest.TestCase):
    def test_single_clean_instance_ok(self):
        self.assertEqual(dio.save_preflight([_info("A v3", active=True)], 0), (None, []))

    def test_read_only_refused(self):
        refusal, _ = dio.save_preflight([_info("A v3", active=True, read_only=True)], 0)
        self.assertEqual(refusal["reason"], "read_only")

    def test_never_saved_refused(self):
        refusal, _ = dio.save_preflight([_info("Untitled", file_id=None, active=True)], 0)
        self.assertEqual(refusal["reason"], "never_saved")

    def test_unknown_read_only_warns(self):
        refusal, warnings = dio.save_preflight([_info("A v3", active=True, read_only="unknown")], 0)
        self.assertIsNone(refusal)
        self.assertIn("isReadOnly", warnings[0])

    def test_older_duplicate_refused(self):
        infos = [_info("A v12", version=12), _info("A v11", version=11, active=True)]
        refusal, _ = dio.save_preflight(infos, 1)
        self.assertEqual(refusal["reason"], "older_duplicate")
        self.assertIn("v11", refusal["error"])
        self.assertIn("v12", refusal["error"])
        self.assertEqual(len(refusal["instances"]), 2)

    def test_newest_duplicate_allowed_with_warning(self):
        infos = [_info("A v12", version=12, active=True), _info("A v11", version=11)]
        refusal, warnings = dio.save_preflight(infos, 0)
        self.assertIsNone(refusal)
        self.assertTrue(any("2 instances" in w for w in warnings))

    def test_tied_newest_warns_with_count(self):
        infos = [_info("A v12", version=12, active=True), _info("A v11", version=11), _info("A v12", version=12)]
        refusal, warnings = dio.save_preflight(infos, 0)
        self.assertIsNone(refusal)
        self.assertIn("2 at v12", warnings[0])

    def test_inactive_target_warns(self):
        _, warnings = dio.save_preflight([_info("A v3")], 0)
        self.assertIn("not the active document", warnings[0])


class SaveVerifierTests(unittest.TestCase):
    def test_needs_both_clean_and_new_version(self):
        verifier = dio.SaveVerifier(11, 10)
        self.assertFalse(verifier.observe(0.1, is_modified=False, latest_version=11))
        self.assertEqual(verifier.stage, "clean_locally_but_no_new_version")
        self.assertFalse(verifier.observe(0.2, is_modified=True, latest_version=12))
        self.assertEqual(verifier.stage, "new_version_but_still_modified")
        self.assertTrue(verifier.observe(0.3, is_modified=False))
        self.assertTrue(verifier.saved)
        self.assertEqual(verifier.stage, "saved")

    def test_missing_reading_keeps_previous(self):
        verifier = dio.SaveVerifier(11, 10)
        verifier.observe(0.1, is_modified=True, latest_version=12)
        verifier.observe(0.2, is_modified=False, latest_version=None)
        self.assertTrue(verifier.saved)
        self.assertEqual(verifier.latest_version, 12)

    def test_timeout_when_nothing_changes(self):
        verifier = dio.SaveVerifier(11, 1.0)
        self.assertFalse(verifier.observe(0.5, True, 11))
        self.assertTrue(verifier.observe(1.0, True, 11))
        self.assertTrue(verifier.timed_out)
        self.assertFalse(verifier.saved)
        self.assertEqual(verifier.stage, "nothing_happened")

    def test_unknown_baseline_never_succeeds(self):
        verifier = dio.SaveVerifier(None, 1.0)
        verifier.observe(0.1, False, 5)
        self.assertFalse(verifier.saved)

    def test_bool_is_not_a_version(self):
        verifier = dio.SaveVerifier(0, 1.0)
        verifier.observe(0.1, False, True)
        self.assertFalse(verifier.saved)

    def test_done_is_sticky(self):
        verifier = dio.SaveVerifier(1, 1.0)
        verifier.observe(0.1, False, 2)
        verifier.observe(5.0, True, 1)
        self.assertTrue(verifier.saved)
        self.assertEqual(verifier.latest_version, 2)


STALL_LOG = [
    "20261104T101500 I .1 Workflow start: save 1-old",
    "20261104T101501 I 2 [CFSUploader] Uploaded document Knuckle, url: ...version=11",
    "20261104T102000 I .1 Workflow start: save 1-abc",
    "    continuation line mentioning CFSUploader with no stamp",
    "20261104T102001 I 3 Some unrelated line",
]
GOOD_LOG = [
    "20261104T103000 I .1 Workflow start: save 1-def",
    "20261104T103001 I 2 [CFSUploader] Starting upload. Original File: _Knuckle",
    "20261104T103003 I 2 [CFSUploader] Uploaded document Knuckle, Original File: _Knuckle, url: ...version=12",
    "20261104T103003 W 2 [CFS] Upload failed for: x, Reason: PIM data publish is not finished.",
]


class LogFilterTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(dio.parse_log_time(GOOD_LOG[0]), datetime(2026, 11, 4, 10, 30, 0))
        self.assertIsNone(dio.parse_log_time("no stamp"))
        self.assertIsNone(dio.parse_log_time("20261399T999999 I bad"))

    def test_filters_by_time_and_keyword(self):
        lines = dio.filter_log_lines(STALL_LOG, datetime(2026, 11, 4, 10, 20, 0, 500000))
        self.assertEqual(lines, [STALL_LOG[2], STALL_LOG[3]])

    def test_nothing_before_first_stamp(self):
        self.assertEqual(dio.filter_log_lines(["CFSUploader orphan"], datetime(2000, 1, 1)), [])

    def test_limit_keeps_newest(self):
        lines = [f"20261104T1030{i:02d} I CFSUploader {i}" for i in range(30)]
        kept = dio.filter_log_lines(lines, datetime(2026, 11, 4), limit=5)
        self.assertEqual(kept, lines[-5:])

    def test_long_lines_truncated(self):
        kept = dio.filter_log_lines(["20261104T103000 I Upload failed " + "x" * 2000], datetime(2026, 11, 4))
        self.assertEqual(len(kept[0]), dio._LOG_LINE_MAX_CHARS)

    def test_summary_stall(self):
        lines = dio.filter_log_lines(STALL_LOG, datetime(2026, 11, 4, 10, 20))
        summary = dio.summarize_log(lines, "Knuckle")
        self.assertTrue(summary["save_workflow_started"])
        self.assertFalse(summary["upload_started"])
        self.assertIn("no upload began", summary["verdict"])

    def test_summary_success(self):
        summary = dio.summarize_log(GOOD_LOG, "Knuckle")
        self.assertTrue(summary["uploaded_this_file"])
        self.assertEqual(summary["verdict"], "upload reported finished")

    def test_summary_other_file(self):
        self.assertIn("none for this file", dio.summarize_log(GOOD_LOG, "Frame")["verdict"])

    def test_summary_empty(self):
        self.assertIn("no save activity", dio.summarize_log([], "Knuckle")["verdict"])

    def test_read_tail_drops_partial_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AppLogFile1.log")
            with open(path, "w") as handle:
                handle.write("first line\nsecond line\nthird line\n")
            self.assertEqual(dio.read_log_tail(path, max_bytes=1000), ["first line", "second line", "third line"])
            self.assertEqual(dio.read_log_tail(path, max_bytes=15), ["third line"])

    def test_newest_log_by_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = os.path.join(tmp, "AppLogFile2.log"), os.path.join(tmp, "AppLogFile1.log")
            for path, mtime in ((old, 100), (new, 200)):
                open(path, "w").close()
                os.utime(path, (mtime, mtime))
            self.assertEqual(dio.newest_app_log(os.path.join(tmp, "AppLogFile*.log")), new)
            self.assertIsNone(dio.newest_app_log(os.path.join(tmp, "none*.log")))


class RecommendationTests(unittest.TestCase):
    def test_duplicates_get_close_and_reopen(self):
        self.assertEqual(dio.save_recommendation("clean_locally_but_no_new_version", [0, 1], False),
                         dio.STUCK_INSTANCE_FIX)
        self.assertEqual(dio.save_recommendation("nothing_happened", [0], False), dio.STUCK_INSTANCE_FIX)

    def test_read_only_first(self):
        self.assertIn("read-only", dio.save_recommendation("nothing_happened", [0, 1], True))

    def test_queued_upload(self):
        self.assertIn("queued", dio.save_recommendation("clean_locally_but_no_new_version", [0], False))


class PlanOpenTests(unittest.TestCase):
    def test_reuse_exact_version(self):
        infos = [_info("A v11", version=11), _info("A v12", version=12)]
        self.assertEqual(dio.plan_open(infos, "F1", 12), {"action": "reuse", "index": 1, "others": [0]})

    def test_reuse_prefers_active(self):
        infos = [_info("A v12", version=12), _info("A v12", version=12, active=True)]
        self.assertEqual(dio.plan_open(infos, "F1", 12)["index"], 1)
        self.assertEqual(dio.plan_open(infos, "F1", 12)["others"], [0])

    def test_open_when_only_older_loaded(self):
        infos = [_info("A v11", version=11), _info("B v2", file_id="F2")]
        self.assertEqual(dio.plan_open(infos, "F1", 12), {"action": "open", "others": [0]})

    def test_open_fresh(self):
        self.assertEqual(dio.plan_open([], "F1", 1), {"action": "open", "others": []})


class MatchNamedTests(unittest.TestCase):
    ROWS = [{"name": "Knuckle", "ext": "f3d", "id": "1"}, {"name": "Knuckle", "ext": "f2d", "id": "2"},
            {"name": "Frame", "ext": "f3d", "id": "3"}]

    def test_unique(self):
        self.assertEqual(dio.match_named(self.ROWS, "Frame", "file")["id"], "3")

    def test_extension_disambiguates(self):
        self.assertEqual(dio.match_named(self.ROWS, "Knuckle.f2d", "file")["id"], "2")

    def test_ambiguous(self):
        with self.assertRaisesRegex(ValueError, "2 files named"):
            dio.match_named(self.ROWS, "Knuckle", "file")

    def test_missing(self):
        with self.assertRaisesRegex(ValueError, "No folder named"):
            dio.match_named([], "X", "folder")


# -- Handlers against fakes --------------------------------------------------


class SaveDocumentHandlerTests(unittest.TestCase):
    def _app(self, on_save, extra_docs=(), latest=11):
        cloud_file = FakeDataFile(version=latest, latest=latest)
        target = FakeDocument(FakeDataFile(version=latest), active=True, on_save=on_save)
        app = FakeApp([target, *extra_docs], FakeCloud([cloud_file]))
        return app, target, cloud_file

    def test_verified_save(self):
        holder = {}

        def land(doc):
            doc.isModified = False
            holder["cloud"].latestVersionNumber = 12

        app, target, cloud = self._app(land)
        holder["cloud"] = cloud
        with _Patches(_patched(app)):
            result = dio.save_document({"description": "fix knuckle"})
        payload = _payload(result)
        self.assertFalse(result["isError"])
        self.assertTrue(payload["saved"])
        self.assertEqual((payload["version_before"], payload["version_after"]), (11, 12))
        self.assertEqual(target.saved_with, "fix knuckle")

    def test_stalled_save_reports_failure_and_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "AppLogFile1.log")
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            with open(log_path, "w") as handle:
                handle.write(f"{stamp} I .1 Workflow start: save 1-abc\n")
            dup = FakeDocument(FakeDataFile(version=11), visible=False, modified=False)
            dup.name = "Knuckle v11 (1)"
            app, _, _ = self._app(None, extra_docs=[dup])
            with _Patches(_patched(app, log_path)):
                result = dio.save_document({"description": "d", "timeout_seconds": 0.05})
        payload = _payload(result)
        self.assertTrue(result["isError"])
        self.assertFalse(payload["saved"])
        self.assertTrue(payload["save_returned"])
        self.assertEqual(payload["stage"], "nothing_happened")
        diagnosis = payload["diagnosis"]
        self.assertEqual(diagnosis["instances_open"], 2)
        self.assertEqual(diagnosis["instances"][1]["title_suffix"], 1)
        self.assertFalse(diagnosis["instances"][1]["is_visible"])
        self.assertIn("no upload began", diagnosis["app_log"]["summary"]["verdict"])
        self.assertEqual(payload["recommendation"], dio.STUCK_INSTANCE_FIX)

    def test_read_only_refused_without_saving(self):
        app, target, _ = self._app(None)
        target.dataFile.isReadOnly = True
        with _Patches(_patched(app)):
            payload = _payload(dio.save_document({"description": "d"}))
        self.assertEqual(payload["reason"], "read_only")
        self.assertFalse(payload["attempted"])
        self.assertIsNone(target.saved_with)

    def test_older_duplicate_refused_without_saving(self):
        newer = FakeDocument(FakeDataFile(version=12))
        app, target, _ = self._app(None, extra_docs=[newer])
        with _Patches(_patched(app)):
            payload = _payload(dio.save_document({"description": "d"}))
        self.assertEqual(payload["reason"], "older_duplicate")
        self.assertIn("recommendation", payload)
        self.assertIsNone(target.saved_with)

    def test_unmodified_is_not_saved(self):
        app, target, _ = self._app(None)
        target.isModified = False
        with _Patches(_patched(app)):
            result = dio.save_document({"description": "d"})
        self.assertFalse(result["isError"])
        self.assertEqual(_payload(result)["reason"], "not_modified")
        self.assertIsNone(target.saved_with)


class OpenDocumentHandlerTests(unittest.TestCase):
    def test_reuses_open_instance(self):
        cloud_file = FakeDataFile(version=12, latest=12)
        other = FakeDocument(FakeDataFile(file_id="F2", name="Frame", version=1), active=True)
        open_doc = FakeDocument(FakeDataFile(version=12))
        app = FakeApp([other, open_doc], FakeCloud([cloud_file]))
        with _Patches(_patched(app)):
            payload = _payload(dio.open_document({"data_file_id": "F1"}))
        self.assertTrue(payload["reused"])
        self.assertTrue(open_doc.isActive)
        self.assertEqual(app.documents.opened, [])
        self.assertEqual(payload["instances_open"], 1)

    def test_opens_latest_when_only_older_loaded_and_flags_duplicate(self):
        cloud_file = FakeDataFile(version=12, latest=12)
        older = FakeDocument(FakeDataFile(version=11), active=True)
        app = FakeApp([older], FakeCloud([cloud_file]))
        with _Patches(_patched(app)):
            payload = _payload(dio.open_document({"data_file_id": "F1"}))
        self.assertFalse(payload["reused"])
        self.assertEqual(app.documents.opened, [cloud_file])
        self.assertEqual(payload["instances_open"], 2)
        self.assertIn("v11", payload["warnings"][0])
        self.assertTrue(payload["document"]["is_active"])

    def test_opens_specific_version(self):
        v5 = FakeDataFile(version=5)
        cloud_file = FakeDataFile(version=12, latest=12)
        cloud_file.versions = FakeCollection([v5, cloud_file])
        app = FakeApp([], FakeCloud([cloud_file]))
        with _Patches(_patched(app)):
            payload = _payload(dio.open_document({"data_file_id": "F1", "version": 5}))
        self.assertEqual(app.documents.opened, [v5])
        self.assertEqual(payload["document"]["data_file"]["version_number"], 5)

    def test_version_newer_than_latest(self):
        app = FakeApp([], FakeCloud([FakeDataFile(version=3, latest=3)]))
        with _Patches(_patched(app)):
            result = dio.open_document({"data_file_id": "F1", "version": 4})
        self.assertTrue(result["isError"])

    def test_unknown_id(self):
        app = FakeApp([], FakeCloud([]))
        with _Patches(_patched(app)):
            self.assertTrue(dio.open_document({"data_file_id": "nope"})["isError"])

    def test_resolves_project_path(self):
        target = FakeDataFile(file_id="F7", name="Knuckle", version=2)
        sub = type("Folder", (), {"name": "Front", "id": "d2", "dataFolders": FakeCollection([]),
                                  "dataFiles": FakeCollection([target])})()
        root = type("Folder", (), {"name": "root", "id": "d1", "dataFolders": FakeCollection([sub]),
                                   "dataFiles": FakeCollection([])})()
        project = type("Project", (), {"name": "Chassis", "id": "p1", "rootFolder": root})()
        app = FakeApp([], FakeCloud([target], projects=[project]))
        with _Patches(_patched(app)):
            payload = _payload(dio.open_document({"project": "Chassis", "path": "Front/Knuckle"}))
        self.assertEqual(app.documents.opened, [target])
        self.assertEqual(payload["document"]["data_file"]["id"], "F7")
        with _Patches(_patched(app)):
            result = dio.open_document({"project": "Chassis", "path": "Rear/Knuckle"})
        self.assertTrue(result["isError"])
        self.assertIn("No folder named 'Rear'", result["content"][0]["text"])


class CurrentFileIdTests(unittest.TestCase):
    """A file saved for the first time has a local path as its id until the upload lands (seen live)."""

    class _File:
        def __init__(self, file_id):
            self.id = file_id

    class _Doc:
        def __init__(self, data_file):
            self.dataFile = data_file

    def test_reads_the_id_the_document_has_now(self):
        doc = self._Doc(self._File("urn:adsk.wipprod:dm.lineage:A"))
        self.assertEqual(dio._current_file_id(doc, "/local/_a.f3d"), "urn:adsk.wipprod:dm.lineage:A")

    def test_falls_back_when_the_document_has_no_data_file(self):
        self.assertEqual(dio._current_file_id(self._Doc(None), "/local/_a.f3d"), "/local/_a.f3d")


if __name__ == "__main__":
    unittest.main()
