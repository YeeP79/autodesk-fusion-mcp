"""fusion_dialogs: AppleScript generation, output parsing, button choice,
permission errors and off-main-thread routing.  No macOS calls are made: the
osascript runner is a fake."""

import json
import threading
import unittest

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)
import settings  # noqa: F401  (resolves fusion_bridge relative imports)
from fusion_bridge import dialogs, dispatch, operations, runtime, tool_surface

US, RS = "\x1f", "\x1e"


def _proc_line(name="Autodesk Fusion", bundle="com.autodesk.fusion360", pid="5740", how="pid"):
    return US.join(["P", name, bundle, pid, how])


def _win_line(kind="W", wi=1, si=0, title="Main", role="AXWindow", subrole="AXDialog",
              modal="false", buttons=()):
    return US.join([kind, str(wi), str(si), title, role, subrole, modal, RS.join(buttons)])


MAIN = _win_line(title="Knuckle - Autodesk Fusion")
PREFS = _win_line(wi=2, title="Preferences", modal="true", buttons=("Restore Defaults", "Cancel", "OK"))


class FakeRunner:
    """Returns queued (code, stdout, stderr) answers and records each script.

    The permission preflight is answered from ``preflight`` (default: granted)
    and recorded separately, so ``scripts`` holds only the list/press scripts.
    """

    def __init__(self, *answers, preflight=((0, "true\n", ""),)):
        self.answers = list(answers)
        self.preflight = list(preflight)
        self.scripts = []
        self.preflights = 0
        self.timeouts = []

    def __call__(self, script, timeout):
        self.timeouts.append(timeout)
        if script == dialogs.PREFLIGHT_SCRIPT:
            self.preflights += 1
            return self.preflight.pop(0) if len(self.preflight) > 1 else self.preflight[0]
        self.scripts.append(script)
        return self.answers.pop(0)


class FakeClock:
    """Advances 0.25 s per reading, so each run measures 250 ms."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.25
        return self.now


def _run(arguments, runner, platform="darwin"):
    result = dialogs.fusion_dialogs(arguments, runner=runner, platform=platform, pid=5740,
                                    sleep=lambda s: None, clock=FakeClock())
    return result, result["content"][0]["text"]


TIMEOUT = (dialogs.TIMEOUT_RETURNCODE, "", "osascript timed out after 15s")


class AppleScriptStringTests(unittest.TestCase):
    def test_escapes_quotes_backslashes_and_controls(self):
        self.assertEqual(dialogs.applescript_string('a"b\\c\nd\re\tf'), '"a\\"b\\\\c\\nd\\re\\tf"')

    def test_injection_attempt_stays_inside_literal(self):
        literal = dialogs.applescript_string('x" & (do shell script "rm -rf ~") & "')
        self.assertTrue(literal.startswith('"') and literal.endswith('"'))
        inner = literal[1:-1]
        # Every quote inside is escaped, so the literal never closes early.
        self.assertNotIn('"', inner.replace('\\"', ""))

    def test_plain_text_unchanged(self):
        self.assertEqual(dialogs.applescript_string("Cancel"), '"Cancel"')


class ScriptGenerationTests(unittest.TestCase):
    def test_list_script_matches_pid_then_bundle_then_names(self):
        script = dialogs.build_list_script(5740)
        pid_at = script.index("whose unix id is 5740")
        bundle_at = script.index('whose bundle identifier is "com.autodesk.fusion360"')
        name_at = script.index('whose name is "Autodesk Fusion"')
        self.assertLess(pid_at, bundle_at)
        self.assertLess(bundle_at, name_at)

    def test_list_script_without_pid(self):
        self.assertNotIn("whose unix id", dialogs.build_list_script(None))

    def test_press_script_escapes_title_and_button(self):
        dialog = {"kind": "window", "window_index": 2, "sheet_index": 0, "title": 'Save "x"?'}
        script = dialogs.build_press_script(5740, dialog, 'Can"cel')
        self.assertIn('"Save \\"x\\"?"', script)
        self.assertIn('"Can\\"cel"', script)
        self.assertIn("set el to window 2 of p", script)
        self.assertIn("click e", script)
        self.assertNotIn("key code", script)

    def test_sheet_target(self):
        dialog = {"kind": "sheet", "window_index": 1, "sheet_index": 3, "title": ""}
        self.assertIn("set el to sheet 3 of window 1 of p", dialogs.build_escape_script(5740, dialog))

    def test_escape_script_sends_escape(self):
        dialog = {"kind": "window", "window_index": 2, "sheet_index": 0, "title": "Prefs"}
        script = dialogs.build_escape_script(5740, dialog)
        self.assertIn("key code 53", script)
        self.assertIn("set frontmost of p to true", script)
        self.assertNotIn("click", script)

    def test_indices_and_pid_forced_to_int(self):
        dialog = {"kind": "window", "window_index": "2 of p\nerror", "sheet_index": 0, "title": ""}
        with self.assertRaises(ValueError):
            dialogs.build_press_script(5740, dialog, "Cancel")
        with self.assertRaises(ValueError):
            dialogs.build_list_script("1; do shell script")


class ParseTests(unittest.TestCase):
    def test_parses_process_and_windows(self):
        parsed = dialogs.parse_list_output("\n".join([_proc_line(), MAIN, PREFS]) + "\n")
        self.assertEqual(parsed["process"], {"name": "Autodesk Fusion", "bundle_id": "com.autodesk.fusion360",
                                             "pid": 5740, "matched_by": "pid"})
        main, prefs = parsed["windows"]
        self.assertFalse(main["looks_modal"])  # Fusion's main window reports AXDialog
        self.assertTrue(prefs["looks_modal"])
        self.assertIs(prefs["modal"], True)
        self.assertEqual(prefs["buttons"], ["Restore Defaults", "Cancel", "OK"])

    def test_no_process(self):
        self.assertEqual(dialogs.parse_list_output("N"), {"process": None, "windows": []})

    def test_empty_and_short_lines(self):
        parsed = dialogs.parse_list_output("\n\nP\nW" + US + "x\nZ" + US + "junk\n")
        self.assertEqual(parsed["process"]["name"], "")
        self.assertIsNone(parsed["process"]["pid"])
        self.assertEqual(len(parsed["windows"]), 1)
        self.assertEqual(parsed["windows"][0]["window_index"], 0)
        self.assertIsNone(parsed["windows"][0]["modal"])
        self.assertEqual(parsed["windows"][0]["buttons"], [])

    def test_sheet_and_system_dialog_look_modal(self):
        sheet = _win_line(kind="S", wi=1, si=1, subrole="", modal="")
        system = _win_line(wi=3, subrole="AXSystemDialog")
        parsed = dialogs.parse_list_output("\n".join([_proc_line(), sheet, system]))
        self.assertEqual([w["looks_modal"] for w in parsed["windows"]], [True, True])
        self.assertEqual(parsed["windows"][0]["kind"], "sheet")

    def test_none_input(self):
        self.assertEqual(dialogs.parse_list_output(None), {"process": None, "windows": []})


class ChooseButtonTests(unittest.TestCase):
    def test_prefers_cancel_over_close(self):
        self.assertEqual(dialogs.choose_button(["Close", "Cancel"]), "Cancel")

    def test_normalizes_case_space_and_ellipsis(self):
        self.assertEqual(dialogs.choose_button(["  CANCEL… "]), "  CANCEL… ")
        self.assertEqual(dialogs.choose_button(["close..."]), "close...")

    def test_never_picks_unsafe_buttons(self):
        unsafe = ["Save", "Delete", "Yes", "Replace", "OK", "Continue", "Don't Save", "No",
                  "Cancel Upload and Delete", "Close Without Saving", ""]
        self.assertIsNone(dialogs.choose_button(unsafe))

    def test_empty(self):
        self.assertIsNone(dialogs.choose_button([]))


class SelectDialogsTests(unittest.TestCase):
    def _windows(self, *lines):
        return dialogs.parse_list_output("\n".join([_proc_line(), *lines]))["windows"]

    def test_sheets_first_and_non_modal_excluded(self):
        sheet = _win_line(kind="S", wi=1, si=1, title="Save changes?", modal="")
        chosen = dialogs.select_dialogs(self._windows(MAIN, PREFS, sheet))
        self.assertEqual([d["title"] for d in chosen], ["Save changes?", "Preferences"])

    def test_title_filter_case_insensitive(self):
        chosen = dialogs.select_dialogs(self._windows(MAIN, PREFS), "prefer")
        self.assertEqual([d["title"] for d in chosen], ["Preferences"])
        self.assertEqual(dialogs.select_dialogs(self._windows(PREFS), "Knuckle"), [])


class ErrorClassificationTests(unittest.TestCase):
    def test_accessibility_errors(self):
        for stderr in ("execution error: System Events got an error: osascript is not allowed assistive access. (-1719)",
                       "error -25211", "Not allowed assistive access."):
            with self.subTest(stderr=stderr):
                message = dialogs.classify_osascript_error(1, stderr)
                self.assertIn("Accessibility", message)
                self.assertIn("Privacy & Security", message)
                self.assertIn("Fusion", message)

    def test_automation_error(self):
        message = dialogs.classify_osascript_error(
            1, "Not authorized to send Apple events to System Events. (-1743)")
        self.assertIn("Automation", message)

    def test_generic_error_keeps_detail(self):
        message = dialogs.classify_osascript_error(2, "syntax error: boom")
        self.assertIn("exit 2", message)
        self.assertIn("boom", message)

    def test_changed_dialog(self):
        self.assertIn("changed", dialogs.classify_osascript_error(1, "error FUSION_DIALOG_CHANGED (9001)"))


class HandlerTests(unittest.TestCase):
    def test_rejects_bad_arguments_without_running(self):
        runner = FakeRunner()
        for args, word in (({"action": "close"}, "action"), ({"title_contains": ""}, "title_contains"),
                           ({"title_contains": 5}, "title_contains"), ({"bogus": 1}, "bogus")):
            with self.subTest(args=args):
                result, text = _run(args, runner)
                self.assertTrue(result["isError"])
                self.assertIn(word, text)
        self.assertEqual(runner.scripts, [])

    def test_non_macos(self):
        runner = FakeRunner()
        result, text = _run({}, runner, platform="win32")
        self.assertTrue(result["isError"])
        self.assertIn("not supported on this platform", text)
        self.assertEqual(runner.scripts, [])

    def test_list(self):
        runner = FakeRunner((0, "\n".join([_proc_line(), MAIN, PREFS]), ""))
        result, text = _run({}, runner)
        self.assertFalse(result["isError"])
        payload = json.loads(text)
        self.assertEqual(payload["process"]["matched_by"], "pid")
        self.assertEqual([d["title"] for d in payload["dialogs"]], ["Preferences"])
        self.assertEqual(payload["other_windows"][0]["title"], "Knuckle - Autodesk Fusion")
        self.assertIn("whose unix id is 5740", runner.scripts[0])

    def test_list_permission_error(self):
        runner = FakeRunner((1, "", "osascript is not allowed assistive access. (-1719)"))
        result, text = _run({"action": "list"}, runner)
        self.assertTrue(result["isError"])
        self.assertIn("System Settings > Privacy & Security > Accessibility", text)

    def test_no_fusion_process(self):
        result, text = _run({}, FakeRunner((0, "N", "")))
        self.assertTrue(result["isError"])
        self.assertIn("no Fusion process", text)

    def test_dismiss_presses_cancel_and_relists(self):
        runner = FakeRunner((0, "\n".join([_proc_line(), MAIN, PREFS]), ""), (0, "OK", ""),
                            (0, "\n".join([_proc_line(), MAIN]), ""))
        result, text = _run({"action": "dismiss"}, runner)
        self.assertFalse(result["isError"])
        payload = json.loads(text)
        self.assertEqual(payload["pressed"], "Cancel")
        self.assertEqual(payload["dialog"]["title"], "Preferences")
        self.assertEqual(payload["remaining_dialogs"], [])
        self.assertIn('"Cancel"', runner.scripts[1])
        self.assertNotIn('is "OK"', runner.scripts[1])
        self.assertIn("unix id is 5740", runner.scripts[1])

    def test_dismiss_falls_back_to_escape(self):
        box = _win_line(wi=2, title="Error", modal="true", buttons=("OK",))
        runner = FakeRunner((0, "\n".join([_proc_line(), box]), ""), (0, "OK", ""),
                            (0, _proc_line(), ""))
        payload = json.loads(_run({"action": "dismiss"}, runner)[1])
        self.assertEqual(payload["pressed"], "Escape key")
        self.assertIn("key code 53", runner.scripts[1])
        self.assertIn("Escape", payload["note"])

    def test_dismiss_with_title_filter_and_nothing_matching(self):
        runner = FakeRunner((0, "\n".join([_proc_line(), MAIN, PREFS]), ""))
        result, text = _run({"action": "dismiss", "title_contains": "Parameters"}, runner)
        self.assertFalse(result["isError"])
        self.assertIsNone(json.loads(text)["pressed"])
        self.assertEqual(len(runner.scripts), 1)

    def test_dismiss_press_failure(self):
        runner = FakeRunner((0, "\n".join([_proc_line(), PREFS]), ""),
                            (1, "", "error \"FUSION_DIALOG_CHANGED\" number 9001"))
        result, text = _run({"action": "dismiss"}, runner)
        self.assertTrue(result["isError"])
        self.assertIn("changed or closed", text)

    def test_dismiss_relist_failure_still_reports_press(self):
        runner = FakeRunner((0, "\n".join([_proc_line(), PREFS]), ""), (0, "OK", ""),
                            TIMEOUT, TIMEOUT)
        result, text = _run({"action": "dismiss"}, runner)
        self.assertFalse(result["isError"])
        payload = json.loads(text)
        self.assertEqual(payload["pressed"], "Cancel")
        self.assertIsNone(payload["remaining_dialogs"])
        self.assertIn("timed out", payload["note"])


class RetryAndPreflightTests(unittest.TestCase):
    LISTING = (0, "\n".join([_proc_line(), MAIN, PREFS]), "")

    def test_list_reports_elapsed_ms_per_attempt(self):
        payload = json.loads(_run({}, FakeRunner(self.LISTING))[1])
        self.assertEqual(payload["osascript_attempts"], [
            {"step": "preflight", "attempt": 1, "elapsed_ms": 250, "outcome": "ok"},
            {"step": "list", "attempt": 1, "elapsed_ms": 250, "outcome": "ok"},
        ])

    def test_list_timeout_is_retried_once(self):
        runner = FakeRunner(TIMEOUT, self.LISTING)
        result, text = _run({}, runner)
        self.assertFalse(result["isError"])
        attempts = json.loads(text)["osascript_attempts"]
        self.assertEqual([(a["step"], a["attempt"], a["outcome"]) for a in attempts],
                         [("preflight", 1, "ok"), ("list", 1, "timeout"), ("list", 2, "ok")])

    def test_two_timeouts_fail_with_timings(self):
        runner = FakeRunner(TIMEOUT, TIMEOUT)
        result, text = _run({}, runner)
        self.assertTrue(result["isError"])
        self.assertIn("timed out", text)
        self.assertIn("list#1 timeout in 250 ms, list#2 timeout in 250 ms", text)
        self.assertEqual(len(runner.scripts), 2)

    def test_non_timeout_failure_not_retried(self):
        runner = FakeRunner((2, "", "syntax error: boom"))
        result, text = _run({}, runner)
        self.assertTrue(result["isError"])
        self.assertEqual(len(runner.scripts), 1)
        self.assertIn("list#1 exit 2", text)

    def test_preflight_false_fails_fast(self):
        runner = FakeRunner(preflight=((0, "false\n", ""),))
        result, text = _run({}, runner)
        self.assertTrue(result["isError"])
        self.assertIn("Privacy & Security > Accessibility", text)
        self.assertEqual(runner.scripts, [])

    def test_preflight_automation_error_fails_fast(self):
        runner = FakeRunner(preflight=((1, "", "Not authorized to send Apple events to System Events. (-1743)"),))
        result, text = _run({"action": "dismiss"}, runner)
        self.assertTrue(result["isError"])
        self.assertIn("Automation", text)
        self.assertEqual(runner.scripts, [])

    def test_preflight_uses_short_timeout(self):
        runner = FakeRunner(self.LISTING)
        _run({}, runner)
        self.assertEqual(runner.timeouts, [dialogs.PREFLIGHT_TIMEOUT_S, dialogs.OSASCRIPT_TIMEOUT_S])
        self.assertLess(dialogs.PREFLIGHT_TIMEOUT_S, dialogs.OSASCRIPT_TIMEOUT_S)

    def test_preflight_timeout_or_odd_error_does_not_block(self):
        for preflight in ((TIMEOUT,), ((2, "", "weird"),)):
            with self.subTest(preflight=preflight):
                runner = FakeRunner(self.LISTING, preflight=preflight)
                result, _text = _run({}, runner)
                self.assertFalse(result["isError"])
                self.assertEqual(len(runner.scripts), 1)
        runner = FakeRunner(self.LISTING, preflight=(TIMEOUT, TIMEOUT))
        _run({}, runner)
        self.assertEqual(runner.preflights, 2)  # timed-out preflight retried once too

    def test_dismiss_timeout_is_not_retried(self):
        runner = FakeRunner(self.LISTING, TIMEOUT)
        result, text = _run({"action": "dismiss"}, runner)
        self.assertTrue(result["isError"])
        self.assertIn("not retried", text)
        self.assertIn("dismiss#1 timeout", text)
        self.assertEqual(len(runner.scripts), 2)

    def test_dismiss_reports_attempts_including_relist(self):
        runner = FakeRunner(self.LISTING, (0, "OK", ""), (0, "\n".join([_proc_line(), MAIN]), ""))
        payload = json.loads(_run({"action": "dismiss"}, runner)[1])
        self.assertEqual([a["step"] for a in payload["osascript_attempts"]],
                         ["preflight", "list", "dismiss", "relist"])

    def test_escape_note_mentions_qt(self):
        box = _win_line(wi=2, title="Preferences", modal="true", buttons=())
        runner = FakeRunner((0, "\n".join([_proc_line(), box]), ""), (0, "OK", ""), (0, _proc_line(), ""))
        payload = json.loads(_run({"action": "dismiss"}, runner)[1])
        self.assertEqual(payload["pressed"], "Escape key")
        self.assertIn("Qt", payload["note"])


class SchemaTests(unittest.TestCase):
    def _schema(self):
        return next(t for t in tool_surface.TOOL_DEFINITIONS
                    if t["name"] == tool_surface.FUSION_DIALOGS)["inputSchema"]

    def test_schema(self):
        schema = self._schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["action"]["enum"], ["list", "dismiss"])
        self.assertEqual(set(schema["properties"]), {"action", "title_contains"})
        self.assertNotIn("required", schema)


class DirectRoutingTests(unittest.TestCase):
    def test_only_fusion_dialogs_is_direct(self):
        self.assertIn("fusion_dialogs", tool_surface.DIRECT_TOOL_NAMES)
        handlers = runtime.server_tool_handlers(operations.TOOL_HANDLERS)
        self.assertIs(handlers["fusion_dialogs"], runtime.handle_any_tool)
        for name, handler in handlers.items():
            if name not in tool_surface.DIRECT_TOOL_NAMES:
                with self.subTest(tool=name):
                    self.assertIs(handler, dispatch.dispatch_to_main_thread)

    def test_direct_tool_runs_on_calling_thread(self):
        seen = []
        saved = operations.TOOL_HANDLERS["fusion_dialogs"]
        operations.TOOL_HANDLERS["fusion_dialogs"] = lambda cd: (
            seen.append(threading.current_thread()) or {"content": [], "isError": False})
        try:
            handler = runtime.server_tool_handlers(["fusion_dialogs"])["fusion_dialogs"]
            worker = threading.Thread(target=handler, args=({"params": {"name": "fusion_dialogs"}},))
            worker.start()
            worker.join(5)
        finally:
            operations.TOOL_HANDLERS["fusion_dialogs"] = saved
        self.assertEqual(seen, [worker])


if __name__ == "__main__":
    unittest.main()
