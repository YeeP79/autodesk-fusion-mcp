"""Report whether Fusion's main thread can take work, from outside it.

This tool runs on the HTTP server thread (see
``tool_surface.DIRECT_TOOL_NAMES``) so it answers while Fusion is busy, and
for the same reason it must never call the Fusion API. Responsiveness is
measured by ``dispatch.probe_main_thread``; everything else is read from the
bridge's own state, the end of Fusion's app log and ``/dev/fd``.

The log is only a hint. Fusion's main-thread lines (thread id with a leading
dot) go quiet both when it is idle and when it is stuck, so silence proves
nothing; what helps is recognizing the work named by the latest lines.
Entry format: ``20261105T113244 I .24563490 Begin:\tOpen the Document ...``
(local time, level, thread, message). Other lines continue the entry above.
"""

import glob
import math
import os
import re
import time
from datetime import datetime, timedelta

from . import dispatch
from .common.results import error_result, ok_result

DEFAULT_TIMEOUT_SECONDS = 2.0
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 10.0
# Stays under the server's 120 s tool timeout, so the report always gets back.
MAX_WAIT_SECONDS = 100.0
POLL_INTERVAL_SECONDS = 0.25

LOG_GLOB = os.path.join("*", "logs", "AppLogFile*.log")
LOG_TAIL_BYTES = 256 * 1024
LOG_WINDOW = timedelta(seconds=60)
MAX_EVIDENCE_LINES = 5
MAX_EVIDENCE_CHARS = 200

# First match wins for a line; the activity with the most lines wins overall.
ACTIVITIES = (
    ("loading a document", re.compile(
        r"ComponentIdCache|ModelIdCache|\[RG\]|\[Open\]|Open the Document|Opening the document"
        r"|Download Version|Workflow start: open|(?<![Pp])references?\b", re.IGNORECASE)),
    ("saving a document", re.compile(r"CFSUploader|Workflow start: save")),
    ("closing a document", re.compile(r"Document closing|documentPreClose|DeferredSessionReleaseTask")),
)

_ARGUMENTS = {"timeout_seconds", "wait_until_idle_seconds"}
_HEADER = re.compile(r"^(\d{8}T\d{6}) [A-Z] \S+ ?(.*)$")
_TS_FORMAT = "%Y%m%dT%H%M%S"


# -- Arguments -----------------------------------------------------------------


def _number(arguments, name, default, minimum, maximum):
    value = arguments.get(name, default)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return float(value)


def parse_arguments(arguments):
    """``(timeout_seconds, wait_seconds)``; raises ValueError on bad input.

    The legacy transport does not validate against the schema, so this does.
    """
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    unknown = sorted(set(arguments) - _ARGUMENTS)
    if unknown:
        raise ValueError(f"Unknown argument(s): {', '.join(unknown)}")
    timeout = _number(arguments, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS,
                      MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    wait = _number(arguments, "wait_until_idle_seconds", 0, 0, MAX_WAIT_SECONDS)
    return timeout, wait


# -- Polling -------------------------------------------------------------------


def poll_until_responsive(probe, timeout_seconds, wait_seconds, clock, pause):
    """Probe until answered, abandoned, or *wait_seconds* run out.

    Returns ``(last probe result, attempts, seconds waited)``. No probe
    outlives the wait, so a call never takes much longer than *wait_seconds*.
    With no wait this is exactly one probe.
    """
    started = clock()
    attempts = 0
    while True:
        attempts += 1
        remaining = wait_seconds - (clock() - started)
        result = probe(min(timeout_seconds, remaining) if wait_seconds else timeout_seconds)
        if result["responsive"] or result["stopped"]:
            break
        if wait_seconds - (clock() - started) <= POLL_INTERVAL_SECONDS:
            break
        pause(POLL_INTERVAL_SECONDS)
    return result, attempts, clock() - started


# -- App log -------------------------------------------------------------------


def default_log_roots():
    """Directories that hold ``<user id>/logs/AppLogFile*.log``."""
    roots = [os.path.expanduser(
        os.path.join("~", "Library", "Application Support", "Autodesk", "Autodesk Fusion 360"))]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(os.path.join(local_app_data, "Autodesk", "Autodesk Fusion 360"))
    return roots


def newest_log(roots):
    """Path of the app log modified most recently, or None."""
    newest = None
    for root in roots:
        for path in glob.glob(os.path.join(glob.escape(root), LOG_GLOB)):
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest[0]:
                newest = (mtime, path)
    return newest[1] if newest else None


def read_tail_lines(path, max_bytes=LOG_TAIL_BYTES):
    """Complete lines from the last *max_bytes* of *path*; the log runs to tens of MB."""
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read(max_bytes)
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start else lines


def _classify(message):
    for label, pattern in ACTIVITIES:
        if pattern.search(message):
            return label
    return None


def summarize_log(lines, now):
    """Name the work the latest log entries point at.

    Only entries within ``LOG_WINDOW`` of *now* count: an old load at the end
    of a quiet log says nothing about why Fusion is stuck now. Returns
    ``{"activity", "matching", "recent", "lines"}``; ``activity`` is None
    when nothing recognizable was logged, and ``lines`` then holds the latest
    entries of any kind.
    """
    recent = []
    for line in lines:
        match = _HEADER.match(line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group(1), _TS_FORMAT)
        except ValueError:
            continue
        if stamp >= now - LOG_WINDOW:
            recent.append((line, _classify(match.group(2))))

    counts = {}
    for _, label in recent:
        if label:
            counts[label] = counts.get(label, 0) + 1
    # max keeps the first of equals, so ties go to the order of ACTIVITIES.
    activity = max((label for label, _ in ACTIVITIES if label in counts),
                   key=counts.get, default=None)
    evidence = [line for line, label in recent if activity is None or label == activity]
    return {
        "activity": activity,
        "matching": counts.get(activity, 0),
        "recent": len(recent),
        "lines": [line[:MAX_EVIDENCE_CHARS] for line in evidence[-MAX_EVIDENCE_LINES:]],
    }


def read_log_hint(roots=None, now=None):
    """Summarize the live app log; None if there is none or it cannot be read."""
    path = newest_log(default_log_roots() if roots is None else roots)
    if path is None:
        return None
    try:
        lines = read_tail_lines(path)
    except OSError:
        return None
    return summarize_log(lines, datetime.now() if now is None else now)


def describe_busy(running, stopped, hint):
    """One-sentence best guess at why the main thread did not answer."""
    if stopped == "shutdown":
        return "The add-in is stopping."
    if stopped == "cancelled":
        return "The request was cancelled before the main thread answered."
    parts = []
    if running:
        parts.append(f"Bridge tool {running['name']} has been running on the main thread "
                     f"for {running['running_seconds']:g} s.")
    if hint and hint["activity"]:
        parts.append(f"Fusion's log suggests it is {hint['activity']} "
                     f"({hint['matching']} of {hint['recent']} entries in the last "
                     f"{LOG_WINDOW.seconds} s).")
    if not parts:
        parts.append("No bridge tool is running and Fusion's log names no load, save or close; "
                     "a modal dialog or a long operation in the UI may be holding the main thread.")
    return " ".join(parts)


# -- File descriptors ----------------------------------------------------------


def count_open_files(fd_dir="/dev/fd"):
    """Open descriptors in this process, or None where *fd_dir* is unavailable.

    Fusion passes 1024 after a long session, which is what breaks select().
    """
    try:
        # The listing holds one descriptor of its own while it runs.
        return max(0, len(os.listdir(fd_dir)) - 1)
    except OSError:
        return None


# -- Tool ----------------------------------------------------------------------


def collect_status(timeout_seconds, wait_seconds, *, probe, running, queued, log_hint, open_files,
                   clock=time.monotonic, pause=time.sleep):
    """Assemble the report from injected sources (all Fusion-free)."""
    result, attempts, waited = poll_until_responsive(
        probe, timeout_seconds, wait_seconds, clock, pause)
    responsive = result["responsive"]
    latency = result["latency_ms"]
    in_flight = running()
    report = {
        "main_thread_responsive": responsive,
        "main_thread_latency_ms": None if latency is None else round(latency, 1),
        "probe_timeout_seconds": timeout_seconds,
        "in_flight_tool": in_flight,
        "queued_calls": queued(),
        "open_file_count": open_files(),
        "busy_reason": None,
    }
    if not responsive:
        hint = None if result["stopped"] else log_hint()
        report["busy_reason"] = describe_busy(in_flight, result["stopped"], hint)
        report["busy_log_lines"] = hint["lines"] if hint else []
    if wait_seconds:
        report["wait"] = {
            "requested_seconds": wait_seconds,
            "waited_seconds": round(waited, 2),
            "attempts": attempts,
            "timed_out": not responsive and not result["stopped"],
        }
    return report


def fusion_status(arguments, cancel_event=None):
    """Handler for the ``fusion_status`` tool."""
    try:
        timeout_seconds, wait_seconds = parse_arguments(arguments)
    except ValueError as exc:
        return error_result(f"Error: {exc}")

    def probe(seconds):
        return dispatch.probe_main_thread(seconds, cancel_event)

    def pause(seconds):
        if cancel_event is None:
            time.sleep(seconds)
        else:
            cancel_event.wait(seconds)

    return ok_result(collect_status(
        timeout_seconds, wait_seconds, probe=probe, running=dispatch.running_tool,
        queued=dispatch.queued_call_count, log_hint=read_log_hint,
        open_files=count_open_files, pause=pause,
    ))
