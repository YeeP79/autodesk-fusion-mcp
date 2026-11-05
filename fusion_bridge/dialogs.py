"""List and dismiss modal dialogs that block Fusion's main thread.

A modal dialog (Preferences, a command dialog, a message box) stops Fusion
from servicing the bridge's work queue, so every other tool fails with
"Request cancelled" / "Timed out waiting for the Fusion main thread". This
tool therefore runs on the HTTP server thread (see
``tool_surface.DIRECT_TOOL_NAMES``) and never touches ``adsk``: it reads
Fusion's windows through macOS Accessibility (``osascript`` + System Events).

Dismissing only ever presses a button named Cancel or Close, or sends the
Escape key when the dialog has neither. It never presses Save, Delete, Yes,
OK, Replace, Continue or any other button. Seen live: Fusion's Qt dialogs
expose no button names to Accessibility at all, so in practice dismiss sends
Escape (which closed Preferences).

The Accessibility query only works off the main thread: run from inside
``execute_python`` (main thread) it sees 0 windows, because Fusion cannot
service AX requests while its main thread is running the script.

Each call starts with a fast preflight (``UI elements enabled``) so a missing
permission fails in a second with the System Settings path instead of after a
long timeout. Read-only scripts (preflight, list) are retried once when
osascript times out (seen live: an intermittent 15 s timeout followed by a
0.5 s success); the press/Escape script is not retried, because a timed-out
keypress may still have landed. Every osascript run is reported with its
``elapsed_ms``.

Script generation and output parsing are pure functions so they can be
tested without macOS; ``fusion_dialogs`` takes an injectable runner.
"""

import json
import os
import subprocess
import sys
import time

FUSION_BUNDLE_ID = "com.autodesk.fusion360"
FUSION_PROCESS_NAMES = ("Fusion", "Autodesk Fusion", "Autodesk Fusion 360")
ACTIONS = ("list", "dismiss")
# Compared after normalize_label(); anything else is never pressed.
SAFE_BUTTONS = ("cancel", "close")
# Fusion's main document window itself reports subrole AXDialog, so that
# subrole is not evidence of a modal; AXModal (set by AppKit while a window
# runs a modal session) and sheets are.
DIALOG_SUBROLES = ("AXSystemDialog",)
OSASCRIPT_TIMEOUT_S = 15.0
PREFLIGHT_TIMEOUT_S = 5.0
PREFLIGHT_SCRIPT = 'tell application "System Events" to get UI elements enabled'
TIMEOUT_RETURNCODE = 124  # run_osascript's code for subprocess.TimeoutExpired
_SETTLE_S = 0.4

_FIELD = "\x1f"  # unit separator between fields
_ITEM = "\x1e"   # record separator between button labels

# Error markers raised by the press/escape scripts.
_CHANGED_MARK = "FUSION_DIALOG_CHANGED"
_NO_BUTTON_MARK = "FUSION_BUTTON_NOT_FOUND"

_ACCESSIBILITY_HINTS = ("-1719", "-25211", "assistive access")
_AUTOMATION_HINTS = ("-1743", "not authorized to send apple events", "not allowed to send apple events")

ACCESSIBILITY_MESSAGE = (
    "Error: macOS Accessibility permission is missing, so the dialog list could not be read. "
    "osascript runs inside Fusion (the add-in lives in the Fusion process), so grant it to Fusion: "
    "System Settings > Privacy & Security > Accessibility, enable \"Autodesk Fusion\" "
    "(add it with + from /Applications or ~/Applications if it is not listed), then retry. "
    "Fusion may need a restart before the change takes effect."
)
AUTOMATION_MESSAGE = (
    "Error: macOS blocked Fusion from controlling System Events (Apple Events automation). "
    "Grant it in System Settings > Privacy & Security > Automation: under \"Autodesk Fusion\" "
    "enable \"System Events\", then retry."
)


def _error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _ok(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": False}


# -- AppleScript generation ---------------------------------------------------


def applescript_string(value):
    """Return *value* as a double-quoted AppleScript string literal."""
    text = str(value)
    text = (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{text}"'


# Shared handlers: ``clean`` flattens text so one element fits one output line,
# ``label`` reads a button's title, falling back to its AX description.
_PRELUDE = r'''
on clean(v)
	if v is missing value then return ""
	try
		set s to v as text
	on error
		return ""
	end try
	set oldTID to AppleScript's text item delimiters
	repeat with ch in {linefeed, return, character id 30, character id 31}
		set AppleScript's text item delimiters to (contents of ch)
		set parts to text items of s
		set AppleScript's text item delimiters to " "
		set s to parts as text
	end repeat
	set AppleScript's text item delimiters to oldTID
	return s
end clean

on label(e)
	tell application "System Events"
		set t to ""
		try
			set t to my clean(name of e)
		end try
		if t is "" then
			try
				set t to my clean(value of attribute "AXDescription" of e)
			end try
		end if
	end tell
	return t
end label
'''

_DESCRIBE = r'''
on describe(kind, wi, si, el)
	set US to character id 31
	set RS to character id 30
	tell application "System Events"
		set t to ""
		try
			set t to my clean(name of el)
		end try
		set r to ""
		try
			set r to my clean(role of el)
		end try
		set sr to ""
		try
			set sr to my clean(subrole of el)
		end try
		set m to ""
		try
			set m to my clean(value of attribute "AXModal" of el)
		end try
		set btns to {}
		if kind is "S" or m is "true" or sr is "AXSystemDialog" then
			try
				repeat with e in (entire contents of el)
					try
						if role of e is "AXButton" then set end of btns to my label(e)
					end try
				end repeat
			end try
		end if
	end tell
	set oldTID to AppleScript's text item delimiters
	set AppleScript's text item delimiters to RS
	set b to btns as text
	set AppleScript's text item delimiters to oldTID
	return kind & US & wi & US & si & US & t & US & r & US & sr & US & m & US & b
end describe
'''


def _find_process_block(pid):
    """AppleScript that sets ``procs`` and ``how`` to the matched Fusion process.

    Matches by this process's pid first (the add-in runs inside Fusion), then
    by bundle id, then by exact process name.
    """
    lines = ["\tset procs to {}", '\tset how to ""']
    if pid is not None:
        lines += [
            "\ttry",
            f"\t\tset procs to every process whose unix id is {int(pid)}",
            '\t\tif (count of procs) > 0 then set how to "pid"',
            "\tend try",
        ]
    lines += [
        "\tif (count of procs) is 0 then",
        f"\t\tset procs to every process whose bundle identifier is {applescript_string(FUSION_BUNDLE_ID)}",
        '\t\tif (count of procs) > 0 then set how to "bundle_id"',
        "\tend if",
    ]
    for name in FUSION_PROCESS_NAMES:
        lines += [
            "\tif (count of procs) is 0 then",
            f"\t\tset procs to every process whose name is {applescript_string(name)}",
            '\t\tif (count of procs) > 0 then set how to "name"',
            "\tend if",
        ]
    return "\n".join(lines)


def build_list_script(pid=None):
    """Script printing one line per Fusion window/sheet (see parse_list_output)."""
    return f'''{_PRELUDE}
{_DESCRIBE}
set US to character id 31
set out to {{}}
tell application "System Events"
{_find_process_block(pid)}
	if (count of procs) is 0 then return "N"
	set p to item 1 of procs
	set end of out to "P" & US & my clean(name of p) & US & my clean(bundle identifier of p) & US & my clean(unix id of p) & US & how
	set wi to 0
	repeat with w in (every window of p)
		set wi to wi + 1
		set end of out to my describe("W", wi, 0, w)
		set si to 0
		try
			repeat with s in (every sheet of w)
				set si to si + 1
				set end of out to my describe("S", wi, si, s)
			end repeat
		end try
	end repeat
end tell
set AppleScript's text item delimiters to linefeed
return out as text
'''


def _target_block(pid, dialog):
    """AppleScript resolving ``p`` and ``el`` and checking the title is unchanged."""
    ref = f"window {int(dialog['window_index'])} of p"
    if dialog["kind"] == "sheet":
        ref = f"sheet {int(dialog['sheet_index'])} of {ref}"
    return f'''	set p to first process whose unix id is {int(pid)}
	set el to {ref}
	set t to ""
	try
		set t to my clean(name of el)
	end try
	considering case
		if t is not {applescript_string(dialog["title"])} then error "{_CHANGED_MARK}" number 9001
	end considering'''


def build_press_script(pid, dialog, button):
    """Script that clicks the button labelled *button* in *dialog*."""
    return f'''{_PRELUDE}
tell application "System Events"
{_target_block(pid, dialog)}
	set pressed to false
	repeat with e in (entire contents of el)
		try
			if role of e is "AXButton" then
				considering case
					set hit to (my label(e) is {applescript_string(button)})
				end considering
				if hit then
					click e
					set pressed to true
					exit repeat
				end if
			end if
		end try
	end repeat
	if not pressed then error "{_NO_BUTTON_MARK}" number 9002
end tell
return "OK"
'''


def build_escape_script(pid, dialog):
    """Script that brings Fusion and *dialog* to the front and presses Escape."""
    return f'''{_PRELUDE}
tell application "System Events"
{_target_block(pid, dialog)}
	set frontmost of p to true
	try
		perform action "AXRaise" of el
	end try
	delay 0.2
	key code 53
end tell
return "OK"
'''


# -- Output parsing and selection --------------------------------------------


def parse_list_output(text):
    """Parse build_list_script output into ``{"process": ..., "windows": [...]}``.

    ``process`` is None when no Fusion process matched.
    """
    process = None
    windows = []
    # Not splitlines(): it also splits on the record separator used for buttons.
    for line in (text or "").split("\n"):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        fields = line.split(_FIELD)
        tag = fields[0].strip()
        if tag == "P":
            fields += [""] * (5 - len(fields))
            pid = fields[3].strip()
            process = {
                "name": fields[1],
                "bundle_id": fields[2] or None,
                "pid": int(pid) if pid.isdigit() else None,
                "matched_by": fields[4].strip() or None,
            }
        elif tag in ("W", "S"):
            fields += [""] * (8 - len(fields))
            modal = fields[6].strip().lower()
            entry = {
                "kind": "sheet" if tag == "S" else "window",
                "window_index": int(fields[1]) if fields[1].strip().isdigit() else 0,
                "sheet_index": int(fields[2]) if fields[2].strip().isdigit() else 0,
                "title": fields[3],
                "role": fields[4],
                "subrole": fields[5],
                "modal": True if modal == "true" else False if modal == "false" else None,
                "buttons": [b for b in fields[7].split(_ITEM) if b] if fields[7] else [],
            }
            entry["looks_modal"] = looks_modal(entry)
            windows.append(entry)
    return {"process": process, "windows": windows}


def looks_modal(entry):
    """Sheets, AXModal windows and system dialogs count as modal."""
    return (entry["kind"] == "sheet" or entry["modal"] is True
            or entry["subrole"] in DIALOG_SUBROLES)


def normalize_label(label):
    text = (label or "").strip().rstrip(".\u2026").strip()
    return text.casefold()


def choose_button(buttons):
    """Return the button to press (first safe one by SAFE_BUTTONS order) or None."""
    for wanted in SAFE_BUTTONS:
        for label in buttons:
            if normalize_label(label) == wanted:
                return label
    return None


def select_dialogs(windows, title_contains=None):
    """Modal entries, sheets first (they sit on top), filtered by title."""
    needle = title_contains.casefold() if title_contains else None
    dialogs = [w for w in windows if w["looks_modal"]
               and (needle is None or needle in w["title"].casefold())]
    return sorted(dialogs, key=lambda w: (w["kind"] != "sheet", w["window_index"], w["sheet_index"]))


def classify_osascript_error(returncode, stderr):
    """Map an osascript failure to a user-facing error message."""
    lowered = (stderr or "").lower()
    if any(h in lowered for h in _ACCESSIBILITY_HINTS):
        return ACCESSIBILITY_MESSAGE
    if any(h in lowered for h in _AUTOMATION_HINTS):
        return AUTOMATION_MESSAGE
    if _CHANGED_MARK.lower() in lowered:
        return "Error: the dialog changed or closed before it could be dismissed; run list again."
    if _NO_BUTTON_MARK.lower() in lowered:
        return "Error: the chosen button is no longer in the dialog; run list again."
    detail = (stderr or "").strip() or "no error output"
    return f"Error: osascript failed (exit {returncode}): {detail}"


# -- Runner and handler ------------------------------------------------------


def run_osascript(script, timeout):
    """Run *script* with osascript; returns ``(returncode, stdout, stderr)``."""
    try:
        done = subprocess.run(["/usr/bin/osascript", "-"], input=script, capture_output=True,
                              text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMEOUT_RETURNCODE, "", f"osascript timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, "", f"could not run osascript: {exc}"
    return done.returncode, done.stdout, done.stderr


class _Runs:
    """Runs osascript through *runner*, timing every attempt."""

    def __init__(self, runner, clock):
        self.runner = runner
        self.clock = clock
        self.attempts = []

    def run(self, step, script, timeout, retry_on_timeout):
        tries = 2 if retry_on_timeout else 1
        for attempt in range(1, tries + 1):
            started = self.clock()
            code, out, err = self.runner(script, timeout)
            self.attempts.append({
                "step": step, "attempt": attempt,
                "elapsed_ms": int(round((self.clock() - started) * 1000)),
                "outcome": "ok" if code == 0 else
                           "timeout" if code == TIMEOUT_RETURNCODE else f"exit {code}",
            })
            if code != TIMEOUT_RETURNCODE:
                break
        return code, out, err

    def describe(self):
        return ", ".join(f"{a['step']}#{a['attempt']} {a['outcome']} in {a['elapsed_ms']} ms"
                         for a in self.attempts)

    def error(self, message):
        return _error(f"{message}\nosascript attempts: {self.describe()}")


def _preflight(runs):
    """Fail fast on a missing permission; None when it is fine or unknown.

    A preflight timeout is not treated as a failure (System Events may be cold
    starting); the list script that follows has its own timeout and retry.
    """
    code, out, err = runs.run("preflight", PREFLIGHT_SCRIPT, PREFLIGHT_TIMEOUT_S, True)
    if code == 0:
        return ACCESSIBILITY_MESSAGE if out.strip().lower() == "false" else None
    if code == TIMEOUT_RETURNCODE:
        return None
    message = classify_osascript_error(code, err)
    return message if message in (ACCESSIBILITY_MESSAGE, AUTOMATION_MESSAGE) else None


def _summary(entry):
    return {k: entry[k] for k in ("kind", "window_index", "sheet_index", "title",
                                  "role", "subrole", "modal", "buttons")}


def _list(runs, pid, step="list"):
    code, out, err = runs.run(step, build_list_script(pid), OSASCRIPT_TIMEOUT_S, True)
    if code != 0:
        return None, classify_osascript_error(code, err)
    parsed = parse_list_output(out)
    if parsed["process"] is None:
        return None, (f"Error: no Fusion process found (tried pid {pid}, bundle id "
                      f"{FUSION_BUNDLE_ID}, names {', '.join(FUSION_PROCESS_NAMES)})")
    return parsed, None


def fusion_dialogs(arguments, runner=None, platform=None, pid=None, sleep=time.sleep,
                   clock=time.monotonic):
    """List or dismiss Fusion modal dialogs. Runs off Fusion's main thread."""
    arguments = arguments or {}
    unknown = set(arguments) - {"action", "title_contains"}
    if unknown:
        return _error(f"Error: unknown argument(s): {', '.join(sorted(unknown))}")
    action = arguments.get("action", "list")
    if action not in ACTIONS:
        return _error(f"Error: 'action' must be one of {list(ACTIONS)}, got {action!r}")
    title_contains = arguments.get("title_contains")
    if title_contains is not None and (not isinstance(title_contains, str) or not title_contains.strip()):
        return _error("Error: 'title_contains' must be a non-empty string")

    platform = platform if platform is not None else sys.platform
    if platform != "darwin":
        return _error(f"Error: fusion_dialogs is not supported on this platform ({platform}); "
                      "it needs macOS Accessibility.")
    runs = _Runs(runner or run_osascript, clock)
    pid = pid if pid is not None else os.getpid()

    failure = _preflight(runs)
    if failure:
        return runs.error(failure)
    parsed, failure = _list(runs, pid)
    if failure:
        return runs.error(failure)
    dialogs = select_dialogs(parsed["windows"], title_contains)

    if action == "list":
        others = [w for w in parsed["windows"] if not w["looks_modal"]]
        return _ok({
            "process": parsed["process"],
            "dialogs": [_summary(d) for d in dialogs],
            "other_windows": [{k: w[k] for k in ("window_index", "title", "role", "subrole", "modal")}
                              for w in others],
            "osascript_attempts": runs.attempts,
        })

    if not dialogs:
        scope = f" with a title containing {title_contains!r}" if title_contains else ""
        return _ok({"process": parsed["process"], "pressed": None,
                    "note": f"No modal dialog found{scope}; nothing pressed.",
                    "osascript_attempts": runs.attempts})

    target = dialogs[0]
    button = choose_button(target["buttons"])
    target_pid = parsed["process"]["pid"] or pid
    if button is not None:
        script = build_press_script(target_pid, target, button)
        pressed = button
    else:
        script = build_escape_script(target_pid, target)
        pressed = "Escape key"
    code, _out, err = runs.run("dismiss", script, OSASCRIPT_TIMEOUT_S, False)
    if code != 0:
        message = classify_osascript_error(code, err)
        if code == TIMEOUT_RETURNCODE:
            message += "; not retried because the press may have landed. Run list to check."
        return runs.error(message)

    sleep(_SETTLE_S)
    after, failure = _list(runs, pid, "relist")
    remaining = None if failure else [_summary(d) for d in select_dialogs(after["windows"])]
    result = {"process": parsed["process"], "pressed": pressed, "dialog": _summary(target),
              "remaining_dialogs": remaining}
    if button is None:
        result["note"] = ("No Cancel or Close button found (Fusion's Qt dialogs expose no button "
                          "names), so Escape was sent after bringing Fusion to the front.")
    if failure:
        result["note"] = (result.get("note", "") + f" Could not re-list dialogs: {failure}").strip()
    result["osascript_attempts"] = runs.attempts
    return _ok(result)
