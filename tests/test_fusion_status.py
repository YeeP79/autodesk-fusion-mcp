"""fusion_status: argument checks, the report, log hints, and the probe's queue hygiene."""

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from fusion_bridge import dispatch, fusion_status, operations, runtime, tool_surface
from test_http_protocols import HTTPFixture, sse_message

NOW = datetime(2026, 11, 5, 11, 33, 0)
ANSWERED = {"responsive": True, "latency_ms": 12.34, "stopped": None}
SILENT = {"responsive": False, "latency_ms": None, "stopped": None}


def report_of(result):
    return json.loads(result["content"][0]["text"])


def in_thread(fn, *args):
    """Run *fn* on a worker thread, as the HTTP server does; return its result."""
    box = []
    worker = threading.Thread(target=lambda: box.append(fn(*args)), daemon=True)
    worker.start()
    worker.join(15)
    assert box, "worker did not finish"
    return box[0]


class FakeClock:
    """Time passes only when a probe or pause says so."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def pause(self, seconds):
        self.now += seconds


class ArgumentTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(fusion_status.parse_arguments({}), (2.0, 0.0))

    def test_limits_are_inclusive(self):
        self.assertEqual(
            fusion_status.parse_arguments({"timeout_seconds": 10, "wait_until_idle_seconds": 100}),
            (10.0, 100.0))
        self.assertEqual(fusion_status.parse_arguments({"timeout_seconds": 0.1}), (0.1, 0.0))

    def test_rejects_bad_values(self):
        for arguments in (
            {"timeout_seconds": 0}, {"timeout_seconds": -1}, {"timeout_seconds": 10.01},
            {"timeout_seconds": True}, {"timeout_seconds": "2"}, {"timeout_seconds": None},
            {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")},
            {"wait_until_idle_seconds": -0.1}, {"wait_until_idle_seconds": 100.5},
            {"wait_until_idle_seconds": False}, {"wait": 5}, None, [],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                fusion_status.parse_arguments(arguments)

    def test_handler_reports_bad_arguments_without_probing(self):
        with patch.object(dispatch, "probe_main_thread") as probe:
            result = fusion_status.fusion_status({"timeout_seconds": 11})
        self.assertTrue(result["isError"])
        self.assertIn("timeout_seconds must be between 0.1 and 10", result["content"][0]["text"])
        probe.assert_not_called()


class PollingTests(unittest.TestCase):
    def poll(self, answers, timeout, wait):
        clock = FakeClock()
        asked = []

        def probe(seconds):
            asked.append(seconds)
            answer = answers.pop(0) if answers else SILENT
            if not answer["responsive"]:
                clock.now += seconds
            return answer

        result, attempts, waited = fusion_status.poll_until_responsive(
            probe, timeout, wait, clock, clock.pause)
        return result, attempts, waited, asked

    def test_no_wait_is_one_probe(self):
        result, attempts, waited, asked = self.poll([SILENT, ANSWERED], 2.0, 0.0)
        self.assertEqual((result, attempts, asked), (SILENT, 1, [2.0]))
        self.assertEqual(waited, 2.0)

    def test_retries_until_answered(self):
        result, attempts, waited, _ = self.poll([SILENT, SILENT, ANSWERED], 2.0, 30.0)
        self.assertEqual((result, attempts), (ANSWERED, 3))
        self.assertEqual(waited, 4.5)

    def test_last_probe_is_clipped_to_the_wait(self):
        result, _, waited, asked = self.poll([], 2.0, 5.0)
        self.assertEqual(result, SILENT)
        self.assertEqual(asked, [2.0, 2.0, 0.5])
        self.assertLessEqual(waited, 5.0)

    def test_wait_shorter_than_timeout_bounds_the_probe(self):
        _, attempts, waited, asked = self.poll([], 2.0, 1.0)
        self.assertEqual((attempts, asked, waited), (1, [1.0], 1.0))

    def test_stopped_probe_ends_the_wait(self):
        stopped = {"responsive": False, "latency_ms": None, "stopped": "cancelled"}
        result, attempts, _, _ = self.poll([stopped, ANSWERED], 2.0, 30.0)
        self.assertEqual((result, attempts), (stopped, 1))


class LogSummaryTests(unittest.TestCase):
    def test_recognizes_a_load_from_live_log_lines(self):
        lines = [
            "20261105T113244 I .24563490 Begin:\tOpen the Document with Lineage URI : urn:x",
            "20261105T113244 I .24563490 Begin:\t[RG] Download : urn:x",
            "20261105T113245 W .24563490 [ComponentIdCache] componentId cache miss for abc",
            "20261105T113245 I 24564216 Loaded reference to Knuckle",
            "20261105T113246 I 24563660 PUT 200 https://example/sessions - workflow:SLM_heartbeat",
        ]
        hint = fusion_status.summarize_log(lines, NOW)
        self.assertEqual(hint["activity"], "loading a document")
        self.assertEqual((hint["matching"], hint["recent"]), (4, 5))
        self.assertEqual(hint["lines"], lines[:4])

    def test_preferences_are_not_references(self):
        hint = fusion_status.summarize_log(
            ["20261105T113245 I .24563490 Periodic Preferences Save: running",
             "20261105T113245 I 1 Thread PreferencesSaveCyclicSchedulerTask has been ended"], NOW)
        self.assertIsNone(hint["activity"])

    def test_most_frequent_activity_wins(self):
        hint = fusion_status.summarize_log(
            ["20261105T113245 I 1 [ComponentIdCache] miss"]
            + ["20261105T113246 I 1 [CFSUploader] Starting upload"] * 2, NOW)
        self.assertEqual(hint["activity"], "saving a document")

    def test_ties_go_to_the_first_listed_activity(self):
        hint = fusion_status.summarize_log(
            ["20261105T113245 I 1 AutoConstraint: Document closing, draining",
             "20261105T113246 I 1 [ComponentIdCache] miss"], NOW)
        self.assertEqual(hint["activity"], "loading a document")

    def test_old_entries_do_not_count(self):
        hint = fusion_status.summarize_log(
            ["20261105T113159 I 1 [ComponentIdCache] one second too old",
             "20261105T113200 I 1 unrelated, exactly at the edge"], NOW)
        self.assertIsNone(hint["activity"])
        self.assertEqual(hint["recent"], 1)

    def test_no_activity_still_shows_latest_lines(self):
        lines = [f"20261105T113250 I 1 noise {n}" for n in range(9)]
        hint = fusion_status.summarize_log(lines, NOW)
        self.assertEqual(hint["lines"], lines[-fusion_status.MAX_EVIDENCE_LINES:])

    def test_ignores_continuations_and_bad_stamps(self):
        hint = fusion_status.summarize_log(
            ["    at documentOpened (reference.js:1)", "", "20261399T999999 I 1 [ComponentIdCache] x"], NOW)
        self.assertEqual((hint["activity"], hint["recent"], hint["lines"]), (None, 0, []))

    def test_evidence_lines_are_trimmed(self):
        hint = fusion_status.summarize_log(["20261105T113250 I 1 " + "x" * 5000], NOW)
        self.assertEqual(len(hint["lines"][0]), fusion_status.MAX_EVIDENCE_CHARS)

    def test_empty_log(self):
        self.assertEqual(fusion_status.summarize_log([], NOW),
                         {"activity": None, "matching": 0, "recent": 0, "lines": []})


class LogFileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name

    def write_log(self, user, name, data, mtime):
        folder = os.path.join(self.root, user, "logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(data)
        os.utime(path, (mtime, mtime))
        return path

    def test_newest_by_mtime_across_users(self):
        self.write_log("A", "AppLogFile20261105T000000.log", b"", 1000)
        newest = self.write_log("B", "AppLogFile20261101T000000.log", b"", 2000)
        self.write_log("B", "Other.log", b"", 3000)
        self.assertEqual(fusion_status.newest_log([self.root]), newest)

    def test_no_logs(self):
        self.assertIsNone(fusion_status.newest_log([self.root]))
        self.assertIsNone(fusion_status.read_log_hint([self.root], NOW))
        self.assertIsNone(fusion_status.newest_log([os.path.join(self.root, "missing")]))

    def test_tail_drops_the_partial_first_line(self):
        path = self.write_log("A", "AppLogFile1.log", b"aaaa\nbbbb\ncccc\n", 1000)
        self.assertEqual(fusion_status.read_tail_lines(path, 7), ["cccc"])

    def test_short_file_keeps_its_first_line(self):
        path = self.write_log("A", "AppLogFile1.log", b"aaaa\nbbbb", 1000)
        self.assertEqual(fusion_status.read_tail_lines(path), ["aaaa", "bbbb"])

    def test_tail_survives_undecodable_bytes(self):
        path = self.write_log("A", "AppLogFile1.log", b"\xff\xfe bad\ngood\n", 1000)
        self.assertEqual(len(fusion_status.read_tail_lines(path)), 2)

    def test_hint_reads_the_log(self):
        self.write_log("A", "AppLogFile1.log", b"20261105T113250 I 1 [CFSUploader] Starting upload\n", 1000)
        hint = fusion_status.read_log_hint([self.root], NOW)
        self.assertEqual(hint["activity"], "saving a document")

    def test_unreadable_log_gives_no_hint(self):
        self.write_log("A", "AppLogFile1.log", b"x", 1000)
        with patch.object(fusion_status, "read_tail_lines", side_effect=PermissionError("no")):
            self.assertIsNone(fusion_status.read_log_hint([self.root], NOW))


class OpenFileTests(unittest.TestCase):
    def test_counts_this_process(self):
        count = fusion_status.count_open_files()
        self.assertIsInstance(count, int)
        with open(__file__, "rb"):
            self.assertEqual(fusion_status.count_open_files(), count + 1)

    def test_subtracts_the_listing_itself(self):
        with patch.object(os, "listdir", return_value=["0", "1", "2", "3"]):
            self.assertEqual(fusion_status.count_open_files(), 3)

    def test_counts_past_the_select_limit(self):
        with patch.object(os, "listdir", return_value=[str(n) for n in range(1501)]):
            self.assertEqual(fusion_status.count_open_files(), 1500)

    def test_missing_directory(self):
        self.assertIsNone(fusion_status.count_open_files("/no/such/fd/dir"))


class BusyReasonTests(unittest.TestCase):
    TOOL = {"name": "execute_python", "running_seconds": 42.5}
    LOAD = {"activity": "loading a document", "matching": 37, "recent": 80, "lines": []}
    QUIET = {"activity": None, "matching": 0, "recent": 3, "lines": []}

    def test_tool_and_log(self):
        reason = fusion_status.describe_busy(self.TOOL, None, self.LOAD)
        self.assertIn("execute_python has been running on the main thread for 42.5 s", reason)
        self.assertIn("loading a document (37 of 80 entries in the last 60 s)", reason)

    def test_log_alone(self):
        self.assertNotIn("Bridge tool", fusion_status.describe_busy(None, None, self.LOAD))

    def test_nothing_known_points_at_a_dialog(self):
        for hint in (None, self.QUIET):
            with self.subTest(hint=hint):
                self.assertIn("modal dialog", fusion_status.describe_busy(None, None, hint))

    def test_stopped(self):
        self.assertIn("stopping", fusion_status.describe_busy(self.TOOL, "shutdown", self.LOAD))
        self.assertIn("cancelled", fusion_status.describe_busy(None, "cancelled", None))


class ReportTests(unittest.TestCase):
    def collect(self, answers, timeout=2.0, wait=0.0, hint=None, running=None):
        clock = FakeClock()
        hints = []

        def probe(seconds):
            answer = answers.pop(0)
            if not answer["responsive"]:
                clock.now += seconds
            return answer

        def log_hint():
            hints.append(1)
            return hint

        report = fusion_status.collect_status(
            timeout, wait, probe=probe, running=lambda: running, queued=lambda: 3,
            log_hint=log_hint, open_files=lambda: 1234, clock=clock, pause=clock.pause)
        return report, hints

    def test_responsive(self):
        report, hints = self.collect([dict(ANSWERED)])
        self.assertEqual(report, {
            "main_thread_responsive": True, "main_thread_latency_ms": 12.3,
            "probe_timeout_seconds": 2.0, "in_flight_tool": None, "queued_calls": 3,
            "open_file_count": 1234, "busy_reason": None,
        })
        self.assertEqual(hints, [])  # The log is only read when it can help.

    def test_unresponsive(self):
        hint = {"activity": "saving a document", "matching": 2, "recent": 4, "lines": ["evidence"]}
        running = {"name": "execute_python", "running_seconds": 9.0}
        report, _ = self.collect([dict(SILENT)], hint=hint, running=running)
        self.assertFalse(report["main_thread_responsive"])
        self.assertIsNone(report["main_thread_latency_ms"])
        self.assertEqual(report["in_flight_tool"], running)
        self.assertIn("saving a document", report["busy_reason"])
        self.assertEqual(report["busy_log_lines"], ["evidence"])
        self.assertNotIn("wait", report)

    def test_unresponsive_without_a_log(self):
        report, _ = self.collect([dict(SILENT)])
        self.assertEqual(report["busy_log_lines"], [])
        self.assertIn("modal dialog", report["busy_reason"])

    def test_wait_that_succeeds(self):
        report, _ = self.collect([dict(SILENT), dict(ANSWERED)], wait=30.0)
        self.assertTrue(report["main_thread_responsive"])
        self.assertEqual(report["wait"], {"requested_seconds": 30.0, "waited_seconds": 2.25,
                                          "attempts": 2, "timed_out": False})

    def test_wait_that_runs_out(self):
        report, _ = self.collect([dict(SILENT)], wait=1.0)
        self.assertEqual(report["wait"], {"requested_seconds": 1.0, "waited_seconds": 1.0,
                                          "attempts": 1, "timed_out": True})

    def test_stopped_probe_skips_the_log_and_is_not_a_timeout(self):
        stopped = {"responsive": False, "latency_ms": None, "stopped": "shutdown"}
        report, hints = self.collect([stopped], wait=5.0)
        self.assertFalse(report["wait"]["timed_out"])
        self.assertEqual(hints, [])
        self.assertIn("stopping", report["busy_reason"])


class DispatchFixture:
    def setUp(self):
        self.executed = []
        dispatch._halt.clear()
        for target, attribute, value in (
            (dispatch, "get_app", lambda: SimpleNamespace(fireCustomEvent=lambda _: True)),
            (dispatch, "_callback_impl", self.execute),
            (dispatch.futil, "log", lambda *a, **kw: None),
        ):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        dispatch._halt.clear()
        dispatch._flush_pending()
        self.assertTrue(dispatch._pending.empty())
        self.assertIsNone(dispatch.running_tool())

    def execute(self, call):
        self.executed.append(call["params"]["name"])
        return {"content": [{"type": "text", "text": "done"}]}

    @staticmethod
    def queued():
        with dispatch._pending.mutex:
            return list(dispatch._pending.queue)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "timed out waiting for dispatcher state")
            time.sleep(0.005)

    def submit(self, name):
        """Queue a real call from a worker, as a client would."""
        worker = threading.Thread(
            target=dispatch.dispatch_to_main_thread,
            args=({"params": {"name": name, "arguments": {}}},), daemon=True)
        worker.start()
        self.wait_for(lambda: any(
            e["payload"]["params"]["name"] == name for e in self.queued()))
        return worker


class ProbeTests(DispatchFixture, unittest.TestCase):
    def test_timed_out_probe_leaves_no_job_queued(self):
        result = in_thread(dispatch.probe_main_thread, 0.1)
        self.assertEqual(result, {"responsive": False, "latency_ms": None, "stopped": None})
        self.assertEqual(self.queued(), [])
        self.assertEqual(dispatch._inflight, {})
        dispatch._flush_pending()
        self.assertEqual(self.executed, [])

    def test_answered_probe_measures_the_round_trip(self):
        box = []
        worker = threading.Thread(target=lambda: box.append(dispatch.probe_main_thread(2)), daemon=True)
        worker.start()
        self.wait_for(self.queued)
        time.sleep(0.05)
        dispatch._flush_pending()
        worker.join(2)
        self.assertTrue(box[0]["responsive"])
        self.assertGreaterEqual(box[0]["latency_ms"], 40)
        self.assertLess(box[0]["latency_ms"], 2000)
        self.assertEqual(self.executed, [])  # Answered without reaching the tool handler.

    def test_answered_even_without_a_tool_handler(self):
        box = []
        with patch.object(dispatch, "_callback_impl", None):
            worker = threading.Thread(target=lambda: box.append(dispatch.probe_main_thread(2)), daemon=True)
            worker.start()
            self.wait_for(self.queued)
            dispatch._flush_pending()
            worker.join(2)
        self.assertTrue(box[0]["responsive"])

    def test_on_the_main_thread_it_answers_at_once(self):
        result = dispatch.probe_main_thread(5)
        self.assertEqual(result, {"responsive": True, "latency_ms": 0.0, "stopped": None})
        self.assertEqual(self.queued(), [])

    def test_real_work_behind_a_stale_probe_still_runs_in_order(self):
        box = []
        prober = threading.Thread(target=lambda: box.append(dispatch.probe_main_thread(0.2)), daemon=True)
        prober.start()
        self.wait_for(self.queued)
        worker = self.submit("real_tool")
        prober.join(2)
        self.assertFalse(box[0]["responsive"])
        self.assertEqual([e["payload"]["params"]["name"] for e in self.queued()], ["real_tool"])
        dispatch._flush_pending()
        worker.join(2)
        self.assertEqual(self.executed, ["real_tool"])

    def test_probe_behind_queued_work_answers_after_it(self):
        worker = self.submit("real_tool")
        box = []
        prober = threading.Thread(target=lambda: box.append(dispatch.probe_main_thread(2)), daemon=True)
        prober.start()
        self.wait_for(lambda: len(self.queued()) == 2)
        dispatch._flush_pending()
        prober.join(2)
        worker.join(2)
        self.assertTrue(box[0]["responsive"])
        self.assertEqual(self.executed, ["real_tool"])

    def test_dequeued_at_the_deadline_uses_the_answer(self):
        # _flush_pending took the envelope just as the wait ended: nothing is
        # left to remove and the reply is about to land.
        def lost_race(envelope):
            with dispatch._pending.mutex:
                dispatch._pending.queue.remove(envelope)
            threading.Timer(0.02, envelope["reply"].put_nowait,
                            args=({"content": [{"type": "text", "text": "ready"}]},)).start()
            return False

        with patch.object(dispatch, "_try_remove", lost_race):
            result = in_thread(dispatch.probe_main_thread, 0.05)
        self.assertTrue(result["responsive"])
        self.assertEqual(self.queued(), [])

    def test_dequeued_without_an_answer_reports_unresponsive(self):
        def lost_race(envelope):
            with dispatch._pending.mutex:
                dispatch._pending.queue.remove(envelope)
            return False

        with patch.object(dispatch, "_try_remove", lost_race), \
                patch.object(dispatch, "_PROBE_DEQUEUED_GRACE", 0.01):
            result = in_thread(dispatch.probe_main_thread, 0.05)
        self.assertFalse(result["responsive"])

    def test_cancel_abandons_the_probe(self):
        cancel = threading.Event()
        threading.Timer(0.05, cancel.set).start()
        started = time.monotonic()
        result = in_thread(dispatch.probe_main_thread, 5, cancel)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["stopped"], "cancelled")
        self.assertEqual(self.queued(), [])

    def test_shutdown_abandons_the_probe(self):
        threading.Timer(0.05, dispatch._halt.set).start()
        result = in_thread(dispatch.probe_main_thread, 5)
        self.assertEqual(result["stopped"], "shutdown")
        self.assertEqual(self.queued(), [])

    def test_shutdown_drain_answer_is_not_responsive(self):
        box = []
        worker = threading.Thread(target=lambda: box.append(dispatch.probe_main_thread(5)), daemon=True)
        worker.start()
        self.wait_for(self.queued)
        with patch.object(dispatch._scheduler_active, "clear"):
            dispatch.request_main_thread_shutdown()
        worker.join(2)
        self.assertEqual(box[0], {"responsive": False, "latency_ms": None, "stopped": "shutdown"})

    def test_scheduler_tick_wakes_the_main_thread_for_a_probe(self):
        fired = []
        box = []
        worker = threading.Thread(target=lambda: box.append(dispatch.probe_main_thread(0.3)), daemon=True)
        with patch.object(dispatch, "get_app",
                          lambda: SimpleNamespace(fireCustomEvent=fired.append)), \
                patch.object(dispatch, "_last_fire_time", time.time()):
            worker.start()
            self.wait_for(self.queued)
            self.assertEqual(fired, [])  # The probe itself makes no Fusion API call.
            dispatch._fire_event_if_needed()
            self.assertEqual(fired, [dispatch.CALLBACK_EVENT_ID])
            worker.join(2)


class BridgeStateTests(DispatchFixture, unittest.TestCase):
    def test_queued_count_ignores_probes(self):
        self.assertEqual(dispatch.queued_call_count(), 0)
        worker = self.submit("real_tool")
        prober = threading.Thread(target=dispatch.probe_main_thread, args=(0.2,), daemon=True)
        prober.start()
        self.wait_for(lambda: len(self.queued()) == 2)
        self.assertEqual(dispatch.queued_call_count(), 1)
        prober.join(2)
        dispatch._flush_pending()
        worker.join(2)
        self.assertEqual(dispatch.queued_call_count(), 0)

    def run_blocking_tool(self, execute):
        """Run one queued call on a stand-in main thread; returns that thread."""
        worker = self.submit("slow_tool")
        with patch.object(dispatch, "_callback_impl", execute):
            flusher = threading.Thread(target=dispatch._flush_pending, daemon=True)
            flusher.start()
            self.wait_for(dispatch.running_tool)
        self.addCleanup(worker.join, 2)
        return flusher

    def test_running_tool_blocks_the_probe_and_is_reported(self):
        release = threading.Event()
        flusher = self.run_blocking_tool(lambda call: release.wait(5) and {"content": []})
        time.sleep(0.15)

        result = in_thread(dispatch.probe_main_thread, 0.1)
        running = dispatch.running_tool()

        self.assertFalse(result["responsive"])
        self.assertEqual(running["name"], "slow_tool")
        self.assertGreaterEqual(running["running_seconds"], 0.1)
        self.assertEqual(self.queued(), [])
        release.set()
        flusher.join(2)
        self.assertIsNone(dispatch.running_tool())

    def test_tracking_clears_when_the_tool_raises(self):
        def boom(call):
            raise RuntimeError("no")

        worker = self.submit("bad_tool")
        with patch.object(dispatch, "_callback_impl", boom):
            dispatch._flush_pending()
        worker.join(2)
        self.assertIsNone(dispatch.running_tool())

    def test_tracking_clears_when_a_script_exits(self):
        def leave(call):
            raise SystemExit(1)

        worker = self.submit("exiting_tool")
        with patch.object(dispatch, "_callback_impl", leave), self.assertRaises(SystemExit):
            dispatch._flush_pending()
        self.assertIsNone(dispatch.running_tool())
        dispatch._halt.set()  # Its reply never comes; release the waiting client.
        worker.join(2)


class HandlerTests(DispatchFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(fusion_status, "read_log_hint", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timed_out_status_is_a_report_not_an_error(self):
        result = in_thread(fusion_status.fusion_status, {"timeout_seconds": 0.1})
        self.assertFalse(result["isError"])
        self.assertFalse(report_of(result)["main_thread_responsive"])
        self.assertEqual(self.queued(), [])

    def test_waits_for_the_main_thread_to_come_back(self):
        threading.Timer(0.4, dispatch._flush_pending).start()
        result = in_thread(fusion_status.fusion_status,
                           {"timeout_seconds": 0.5, "wait_until_idle_seconds": 5})
        report = report_of(result)
        self.assertTrue(report["main_thread_responsive"])
        self.assertFalse(report["wait"]["timed_out"])
        self.assertLess(report["wait"]["waited_seconds"], 5)

    def test_cancel_ends_a_long_wait(self):
        cancel = threading.Event()
        threading.Timer(0.1, cancel.set).start()
        started = time.monotonic()
        report = report_of(in_thread(fusion_status.fusion_status,
                                     {"wait_until_idle_seconds": 60}, cancel))
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(report["wait"]["timed_out"])
        self.assertEqual(self.queued(), [])


class SurfaceTests(unittest.TestCase):
    def tool(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "fusion_status")

    def test_schema_matches_the_handler(self):
        schema = self.tool()["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), fusion_status._ARGUMENTS)
        timeout = schema["properties"]["timeout_seconds"]
        wait = schema["properties"]["wait_until_idle_seconds"]
        self.assertEqual((timeout["minimum"], timeout["maximum"]),
                         (fusion_status.MIN_TIMEOUT_SECONDS, fusion_status.MAX_TIMEOUT_SECONDS))
        self.assertEqual(wait["maximum"], fusion_status.MAX_WAIT_SECONDS)

    def test_wait_fits_inside_the_server_tool_timeout(self):
        self.assertLess(fusion_status.MAX_WAIT_SECONDS, 120.0)

    def test_fusion_status_is_direct_and_the_rest_are_not(self):
        self.assertIn("fusion_status", tool_surface.DIRECT_TOOL_NAMES)
        handlers = runtime.server_tool_handlers(operations.TOOL_HANDLERS)
        for name, handler in handlers.items():
            expected = (runtime.handle_any_tool if name in tool_surface.DIRECT_TOOL_NAMES
                        else dispatch.dispatch_to_main_thread)
            with self.subTest(tool=name):
                self.assertIs(handler, expected)

    def test_cancel_event_reaches_the_handler(self):
        seen = []
        wrapped = operations._wrap_cancellable(lambda arguments, cancel: seen.append((arguments, cancel)))
        event = threading.Event()
        wrapped({"params": {"arguments": {"a": 1}}, "_cancel_event": event})
        wrapped({"params": {}})
        self.assertEqual(seen, [({"a": 1}, event), ({}, None)])


class DirectOverHTTPTests(HTTPFixture, DispatchFixture, unittest.TestCase):
    """The whole path: the server answers while the main thread services nothing."""

    def setUp(self):
        DispatchFixture.setUp(self)
        patcher = patch.object(fusion_status, "read_log_hint", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        definition = next(t for t in tool_surface.TOOL_DEFINITIONS if t["name"] == "fusion_status")
        self.start_server(tools=[definition],
                          tool_handlers=runtime.server_tool_handlers(["fusion_status"]))

    def test_answers_with_the_main_thread_blocked(self):
        response = self.rpc("tools/call", {"name": "fusion_status",
                                           "arguments": {"timeout_seconds": 0.1}})
        result = sse_message(response)["result"]
        response.read()
        report = report_of(result)
        self.assertFalse(report["main_thread_responsive"])
        self.assertIsInstance(report["open_file_count"], int)
        self.assertEqual(self.queued(), [])


if __name__ == "__main__":
    unittest.main()
