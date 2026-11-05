"""tail_log: line parsing, reverse chunked reading, entry grouping, filters,
presets, argument validation and off-main-thread routing.  Log files are
temporary fixtures built from anonymized real line formats."""

import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)
import settings  # noqa: F401  (resolves fusion_bridge relative imports)
from fusion_bridge import app_log, dispatch, operations, runtime, tool_surface

# Anonymized from a real AppLogFile (thread ids keep their optional "." prefix).
SAMPLE = [
    "20261104T170840 I .24563490 Thread ThreadPoolImpl0 has been started",
    "20261104T170845 W 24564101 FHQService: failed to get FLC URN, response code: ",
    "Request Content-Type: application/json",
    "Request Body: [Empty body]",
    'Response Body: {"httpStatus":404,"detailedMessage":"Not able to find any SPACE"}',
    "20261104T170846 E .24563490 Path does not exist = /Users/someone/Library/Lib",
    "20261104T170849 I 24564113 Workflow start: save",
    "20261104T170849 I 24564113 [CFSUploader] Starting upload of Bracket.f3d",
    "20261104T170850 D 24564113 [CFS] WIP succeeded, removing urn:adsk.wipprod:dm.lineage:AAAA from doc id map.",
    "20261104T170851 W 24564114 [CFS] Upload failed for: /Users/someone/W.login/F/_Bracket.f3d, count: 0",
    "20261104T170855 I 24564113 [CFSUploader] Uploaded document Bracket v12",
    "20261104T170900 I .24563490 GET 200 https://api.example.com/folders/x/content - workflow:getFolderContents dt=0.73",
]
NOW = datetime(2026, 11, 4, 17, 10, 0)


def _text(result):
    return result["content"][0]["text"]


def _body_lines(result):
    return _text(result).split("\n")[2:]


class LogFixture:
    """A fake Fusion support dir: <root>/<user id>/logs/AppLogFile*.log."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.logs = os.path.join(self.root, "USERID", "logs")
        os.makedirs(self.logs)

    def write(self, name, lines, mtime=None):
        path = os.path.join(self.logs, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def call(self, **arguments):
        return app_log.tail_log(arguments, roots=[self.root], now=NOW)

    def close(self):
        self._tmp.cleanup()


class ParseTests(unittest.TestCase):
    def test_header_fields(self):
        self.assertEqual(
            app_log.parse_header(SAMPLE[7]),
            ("20261104T170849", "I", "24564113", "[CFSUploader] Starting upload of Bracket.f3d"))
        self.assertEqual(app_log.parse_header(SAMPLE[0])[2], ".24563490")

    def test_header_with_empty_message_tail(self):
        self.assertEqual(app_log.parse_header(SAMPLE[1])[3], "FHQService: failed to get FLC URN, response code: ")

    def test_continuation_and_junk_are_not_headers(self):
        for line in ("Request Body: [Empty body]", "", "2026 I x y", "20261104T170849 lowercase"):
            with self.subTest(line=line):
                self.assertIsNone(app_log.parse_header(line))

    def test_entry_time(self):
        self.assertEqual(app_log.entry_time("20261104T170849"), datetime(2026, 11, 4, 17, 8, 49))
        self.assertIsNone(app_log.entry_time("20261399T999999"))

    def test_parse_since_relative(self):
        self.assertEqual(app_log.parse_since("10m", NOW), datetime(2026, 11, 4, 17, 0, 0))
        self.assertEqual(app_log.parse_since(" 2H ", NOW), datetime(2026, 11, 4, 15, 10, 0))
        self.assertEqual(app_log.parse_since("30s", NOW), datetime(2026, 11, 4, 17, 9, 30))
        self.assertEqual(app_log.parse_since("1d", NOW), datetime(2026, 11, 3, 17, 10, 0))

    def test_parse_since_iso(self):
        self.assertEqual(app_log.parse_since("2026-11-04T17:08", NOW), datetime(2026, 11, 4, 17, 8))
        self.assertEqual(app_log.parse_since("20261104T170849", NOW), datetime(2026, 11, 4, 17, 8, 49))
        aware = app_log.parse_since("2026-11-04T17:08:00+00:00", NOW)
        self.assertIsNone(aware.tzinfo)

    def test_parse_since_rejects_garbage(self):
        for value in ("yesterday", "10x", "-5m"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app_log.parse_since(value, NOW)


class ReverseReadTests(unittest.TestCase):
    def _reverse(self, data, chunk, max_bytes=10**9):
        stats = {}
        lines = list(app_log.iter_lines_reverse(io.BytesIO(data), len(data), chunk, max_bytes, stats))
        return lines, stats

    def test_small_chunks_reassemble_lines(self):
        data = ("\n".join(SAMPLE) + "\n").encode()
        for chunk in (1, 7, 64, 4096):
            with self.subTest(chunk=chunk):
                lines, stats = self._reverse(data, chunk)
                self.assertEqual([l for l in reversed(lines) if l], SAMPLE)
                self.assertTrue(stats["exhausted"])
                self.assertEqual(stats["scanned"], len(data))

    def test_no_trailing_newline_and_crlf(self):
        lines, _ = self._reverse(b"a\r\nb\r\nc", 2)
        self.assertEqual(lines, ["c", "b", "a"])

    def test_scan_cap_drops_partial_first_line(self):
        data = b"first line\nsecond\nthird\n"
        lines, stats = self._reverse(data, 4, max_bytes=12)
        self.assertFalse(stats["exhausted"])
        self.assertEqual(stats["scanned"], 12)
        self.assertEqual([l for l in lines if l], ["third"])

    def test_empty_file(self):
        lines, stats = self._reverse(b"", 8)
        self.assertEqual(lines, [])
        self.assertTrue(stats["exhausted"])

    def test_invalid_utf8_is_replaced(self):
        lines, _ = self._reverse(b"ok\n\xff\xfe bad\n", 3)
        self.assertIn("�", lines[1])


class EntryTests(unittest.TestCase):
    def test_continuation_lines_attach_to_entry_above(self):
        entries = list(app_log.iter_entries_reverse(reversed(SAMPLE)))
        self.assertEqual(len(entries), 9)
        fhq = [e for e in entries if e["message"].startswith("FHQService")][0]
        self.assertEqual(fhq["level"], "W")
        self.assertEqual(fhq["extra"], SAMPLE[2:5])
        self.assertEqual(entries[0]["timestamp"], "20261104T170900")

    def test_orphan_continuations_at_window_start_dropped(self):
        entries = list(app_log.iter_entries_reverse(reversed(SAMPLE[2:7])))
        self.assertEqual([e["timestamp"] for e in entries], ["20261104T170849", "20261104T170846"])

    def test_contains_matches_continuation_lines(self):
        entries = list(app_log.iter_entries_reverse(reversed(SAMPLE)))
        hits = [e for e in entries if app_log.entry_matches(e, needles=("not able to find",))]
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["message"].startswith("FHQService"))

    def test_format_clips_long_lines_and_many_continuations(self):
        entry = {"line": "x" * (app_log.MAX_LINE_CHARS + 5),
                 "extra": [f"c{i}" for i in range(app_log.MAX_CONTINUATION_LINES + 3)]}
        out = app_log.format_entry(entry)
        self.assertTrue(out[0].endswith("...[5 more chars]"))
        self.assertEqual(out[1], "    c0")
        self.assertEqual(out[-1], "    ...[3 more continuation lines]")
        self.assertEqual(len(out), app_log.MAX_CONTINUATION_LINES + 2)


class TailLogTests(unittest.TestCase):
    def setUp(self):
        self.fx = LogFixture()
        self.fx.write("AppLogFile20261104T170800.log", SAMPLE, mtime=2_000_000_000)
        self.fx.write("AppLogFile20261101T100000.log", ["20261101T100000 I 1 old session"],
                      mtime=1_900_000_000)

    def tearDown(self):
        self.fx.close()

    def test_default_returns_latest_file_oldest_first(self):
        result = self.fx.call()
        self.assertFalse(result["isError"])
        header = _text(result).split("\n")[0]
        self.assertIn("AppLogFile20261104T170800.log", header)
        self.assertIn("9 entries", header)
        self.assertIn("read the whole file", header)
        body = _body_lines(result)
        self.assertEqual(body[0], SAMPLE[0])
        self.assertEqual(body[-1], SAMPLE[-1])
        self.assertIn("    Request Body: [Empty body]", body)

    def test_lines_limit_keeps_newest(self):
        result = self.fx.call(lines=2)
        self.assertEqual(_body_lines(result), SAMPLE[-2:])
        self.assertIn("2-entry limit", _text(result))

    def test_contains_any_match_case_insensitive(self):
        body = _body_lines(self.fx.call(contains=["thread", "PATH DOES"]))
        self.assertEqual(body, [SAMPLE[0], SAMPLE[5]])

    def test_level_filter(self):
        body = _body_lines(self.fx.call(level="e"))
        self.assertEqual(body, [SAMPLE[5]])
        body = _body_lines(self.fx.call(level="WE"))
        self.assertEqual([l for l in body if not l.startswith("    ")], [SAMPLE[1], SAMPLE[5], SAMPLE[9]])

    def test_preset_save(self):
        body = _body_lines(self.fx.call(preset="save"))
        self.assertEqual(body, SAMPLE[6:11])

    def test_preset_errors_combines_with_contains(self):
        body = _body_lines(self.fx.call(preset="errors", contains=["upload"]))
        self.assertEqual(body, [SAMPLE[9]])

    def test_preset_level_intersection(self):
        self.assertEqual(_body_lines(self.fx.call(preset="errors", level="EI")), [SAMPLE[5]])
        result = self.fx.call(preset="errors", level="I")
        self.assertTrue(result["isError"])

    def test_since_relative_and_iso(self):
        self.assertEqual(_body_lines(self.fx.call(since="2026-11-04T17:08:55")), SAMPLE[10:])
        self.assertEqual(_body_lines(self.fx.call(since="1m")), SAMPLE[-1:])

    def test_since_stops_scan_past_grace(self):
        lines = ["20261104T120000 I 1 hours earlier"] + SAMPLE
        self.fx.write("AppLogFile20261104T170800.log", lines, mtime=2_000_000_000)
        result = self.fx.call(since="2026-11-04T17:08:50")
        self.assertIn("reached `since`", _text(result))
        self.assertNotIn("hours earlier", _text(result))

    def test_no_match(self):
        result = self.fx.call(contains=["nothing like this"])
        self.assertFalse(result["isError"])
        self.assertIn("(no matching entries)", _text(result))
        self.assertIn("0 entries", _text(result))

    def test_scan_cap_reported(self):
        result = app_log.tail_log({"contains": ["nothing"]}, roots=[self.fx.root], now=NOW,
                                  max_scan_bytes=100)
        self.assertIn("scan cap", _text(result))

    def test_list_files(self):
        payload = json.loads(_text(self.fx.call(file="list")))
        names = [f["file"] for f in payload["files"]]
        self.assertEqual(names, ["AppLogFile20261104T170800.log", "AppLogFile20261101T100000.log"])
        self.assertTrue(payload["files"][0]["latest"])
        self.assertFalse(payload["files"][1]["latest"])
        self.assertEqual(payload["files"][0]["session_start"], "2026-11-04T17:08:00")
        self.assertGreater(payload["files"][0]["size_bytes"], 0)

    def test_named_file(self):
        body = _body_lines(self.fx.call(file="AppLogFile20261101T100000.log"))
        self.assertEqual(body, ["20261101T100000 I 1 old session"])

    def test_unknown_named_file_and_paths_rejected(self):
        self.assertTrue(self.fx.call(file="AppLogFileNope.log")["isError"])
        self.assertTrue(self.fx.call(file="../USERID/logs/AppLogFile20261101T100000.log")["isError"])

    def test_no_logs_found(self):
        result = app_log.tail_log({}, roots=[os.path.join(self.fx.root, "missing")], now=NOW)
        self.assertTrue(result["isError"])
        self.assertIn("no Fusion app log found", _text(result))


class ValidationTests(unittest.TestCase):
    def _error(self, **arguments):
        result = app_log.tail_log(arguments, roots=["/nonexistent"], now=NOW)
        self.assertTrue(result["isError"], arguments)
        return _text(result)

    def test_bad_arguments(self):
        cases = [
            ({"bogus": 1}, "unknown argument"),
            ({"lines": 0}, "'lines'"),
            ({"lines": 1001}, "'lines'"),
            ({"lines": True}, "'lines'"),
            ({"lines": "5"}, "'lines'"),
            ({"contains": "save"}, "'contains'"),
            ({"contains": [""]}, "'contains'"),
            ({"contains": [3]}, "'contains'"),
            ({"level": "X"}, "'level'"),
            ({"level": ""}, "'level'"),
            ({"preset": "everything"}, "'preset'"),
            ({"since": "whenever"}, "'since'"),
            ({"since": ""}, "'since'"),
            ({"file": ""}, "'file'"),
            ({"file": "/etc/passwd"}, "'file'"),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertIn(expected, self._error(**arguments))

    def test_none_arguments_are_defaults(self):
        result = app_log.tail_log(None, roots=["/nonexistent"], now=NOW)
        self.assertIn("no Fusion app log found", _text(result))


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict_and_matches_handler_arguments(self):
        tool = [t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "tail_log"][0]
        schema = tool["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), app_log._ARGUMENTS)
        self.assertEqual(schema["properties"]["lines"]["maximum"], app_log.MAX_LINES)
        self.assertEqual(set(schema["properties"]["preset"]["enum"]), set(app_log.PRESETS))


class DirectRoutingTests(unittest.TestCase):
    def test_only_tail_log_is_direct(self):
        self.assertEqual(tool_surface.DIRECT_TOOL_NAMES, {"tail_log"})
        handlers = runtime.server_tool_handlers(operations.TOOL_HANDLERS)
        self.assertIs(handlers["tail_log"], runtime.handle_any_tool)
        for name, handler in handlers.items():
            if name != "tail_log":
                with self.subTest(tool=name):
                    self.assertIs(handler, dispatch.dispatch_to_main_thread)

    def test_direct_tool_runs_on_calling_thread(self):
        seen = []
        saved = operations.TOOL_HANDLERS["tail_log"]
        operations.TOOL_HANDLERS["tail_log"] = lambda cd: (
            seen.append(threading.current_thread()) or {"content": [], "isError": False})
        try:
            handler = runtime.server_tool_handlers(["tail_log"])["tail_log"]
            worker = threading.Thread(target=handler, args=({"params": {"name": "tail_log"}},))
            worker.start()
            worker.join(5)
        finally:
            operations.TOOL_HANDLERS["tail_log"] = saved
        self.assertEqual(seen, [worker])


if __name__ == "__main__":
    unittest.main()
