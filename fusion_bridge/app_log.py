"""The ``tail_log`` tool: read Fusion's own application log without the Fusion API.

The log is found and parsed by ``common.app_log``. One file spans the whole
session, so it is read backwards from the end in chunks.

The log is most useful when the main thread is stuck, so this tool runs on the
HTTP server thread (see ``tool_surface.DIRECT_TOOL_NAMES``) and never imports
``adsk``. The add-in has no log file of its own: ``futil.log`` writes only
error-level messages to Fusion's log, so those show up here too.
"""

import json
import os
import re
from datetime import datetime, timedelta

from .common.app_log import (
    LOG_GLOB, TIMESTAMP_FORMAT, default_log_roots, entry_time, find_log_files, parse_header
)
from .common.args import is_int, unknown_message
from .common.results import error_result, ok_result

DEFAULT_LINES = 50
MAX_LINES = 1000
LEVELS = "IWED"
PRESETS = {
    "save": {
        "contains": ("Workflow start: save", "CFSUploader", "Upload failed",
                     "Uploaded document", "CFS]"),
        "level": None,
    },
    "errors": {"contains": None, "level": "WE"},
}
CHUNK_BYTES = 256 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_LINE_CHARS = 2000
MAX_CONTINUATION_LINES = 20
# Threads interleave, so timestamps are only roughly ordered; keep reading this
# far past `since` before concluding everything older has been seen.
SINCE_GRACE = timedelta(seconds=60)

_ARGUMENTS = {"lines", "contains", "level", "since", "file", "preset"}
_RELATIVE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


# -- Describing log files -----------------------------------------------------


def _session_start(path):
    stem = os.path.basename(path)[len("AppLogFile"):-len(".log")]
    try:
        return datetime.strptime(stem, TIMESTAMP_FORMAT).isoformat()
    except ValueError:
        return None


def _human_size(size):
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


def describe_files(files):
    return {
        "files": [
            {
                "file": os.path.basename(path),
                "path": path,
                "size_bytes": size,
                "size": _human_size(size),
                "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                "session_start": _session_start(path),
                "latest": index == 0,
            }
            for index, (mtime, size, path) in enumerate(files)
        ]
    }


# -- Parsing -------------------------------------------------------------------


def parse_since(value, now):
    """Naive local datetime for ``10m`` / ``2h`` / an ISO time; raises ValueError."""
    relative = _RELATIVE.match(value)
    if relative:
        amount, unit = float(relative.group(1)), relative.group(2).lower()
        return now - timedelta(**{_UNITS[unit]: amount})
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def iter_lines_reverse(handle, size, chunk_bytes=CHUNK_BYTES, max_bytes=MAX_SCAN_BYTES, stats=None):
    """Yield the lines of a binary file handle from the end backwards.

    Reads at most *max_bytes*; ``stats["scanned"]`` / ``stats["exhausted"]``
    record how far it got (exhausted means the start of the file was reached).
    The first line of a window cut short by *max_bytes* is dropped as partial.
    """
    stats = stats if stats is not None else {}
    stats.update(scanned=0, exhausted=False)
    position = size
    remainder = b""
    while position > 0 and stats["scanned"] < max_bytes:
        step = min(chunk_bytes, position, max_bytes - stats["scanned"])
        position -= step
        handle.seek(position)
        data = handle.read(step) + remainder
        stats["scanned"] += step
        pieces = data.split(b"\n")
        remainder = pieces[0]
        for piece in reversed(pieces[1:]):
            yield piece.decode("utf-8", errors="replace").rstrip("\r")
    if position == 0:
        stats["exhausted"] = True
        if size:
            yield remainder.decode("utf-8", errors="replace").rstrip("\r")


def iter_entries_reverse(lines):
    """Group reversed lines into entries, newest first.

    Each entry is a dict with timestamp, time, level, thread, message and
    ``extra`` (continuation lines, in file order). Continuation lines older than
    the first header seen (a window cut mid-entry) are dropped.
    """
    pending = []
    for line in lines:
        header = parse_header(line)
        if header is None:
            if line.strip():
                pending.append(line)
            continue
        timestamp, level, thread, message = header
        extra = list(reversed(pending))
        pending = []
        yield {
            "timestamp": timestamp,
            "time": entry_time(timestamp),
            "level": level,
            "thread": thread,
            "message": message,
            "line": line,
            "extra": extra,
        }


def entry_matches(entry, levels=None, needles=(), required=()):
    """Level filter plus two any-of substring groups, all case-insensitive.

    *needles* are the caller's ``contains`` and *required* the preset's; when
    both are given an entry must match one of each.
    """
    if levels and entry["level"] not in levels:
        return False
    if needles or required:
        text = "\n".join([entry["line"], *entry["extra"]]).lower()
        if needles and not any(n in text for n in needles):
            return False
        if required and not any(n in text for n in required):
            return False
    return True


def _clip(line):
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[:MAX_LINE_CHARS] + f" ...[{len(line) - MAX_LINE_CHARS} more chars]"


def format_entry(entry):
    out = [_clip(entry["line"])]
    extra = entry["extra"]
    out.extend("    " + _clip(line) for line in extra[:MAX_CONTINUATION_LINES])
    if len(extra) > MAX_CONTINUATION_LINES:
        out.append(f"    ...[{len(extra) - MAX_CONTINUATION_LINES} more continuation lines]")
    return out


def collect(entries, limit, levels=None, needles=(), required=(), since=None):
    """Up to *limit* matching entries (newest first) and whether `since` stopped the scan."""
    matched = []
    for entry in entries:
        when = entry["time"]
        if since is not None and when is not None and when < since:
            if when < since - SINCE_GRACE:
                return matched, True
            continue
        if entry_matches(entry, levels, needles, required):
            matched.append(entry)
            if len(matched) >= limit:
                break
    return matched, False


# -- Tool ----------------------------------------------------------------------


def _validate(arguments, now):
    unknown = unknown_message(arguments, _ARGUMENTS)
    if unknown:
        return None, f"Error: {unknown}"

    lines = arguments.get("lines", DEFAULT_LINES)
    if not is_int(lines) or not 1 <= lines <= MAX_LINES:
        return None, f"Error: 'lines' must be an integer from 1 to {MAX_LINES}, got {lines!r}"

    contains = arguments.get("contains", [])
    if not isinstance(contains, list) or not all(isinstance(c, str) and c for c in contains):
        return None, "Error: 'contains' must be a list of non-empty strings"

    level = arguments.get("level")
    levels = None
    if level is not None:
        if not isinstance(level, str) or not level or any(c not in LEVELS for c in level.upper()):
            return None, f"Error: 'level' must be one or more of the letters {LEVELS} (e.g. \"WE\"), got {level!r}"
        levels = set(level.upper())

    preset = arguments.get("preset")
    required = ()
    if preset is not None:
        if preset not in PRESETS:
            return None, f"Error: 'preset' must be one of {sorted(PRESETS)}, got {preset!r}"
        spec = PRESETS[preset]
        required = tuple(c.lower() for c in spec["contains"] or ())
        if spec["level"]:
            levels = set(spec["level"]) if levels is None else levels & set(spec["level"])
            if not levels:
                return None, f"Error: 'level' {level!r} excludes every level of preset {preset!r} ({spec['level']})"

    since = arguments.get("since")
    since_time = None
    if since is not None:
        if not isinstance(since, str) or not since.strip():
            return None, "Error: 'since' must be a non-empty string"
        try:
            since_time = parse_since(since, now)
        except ValueError:
            return None, (f"Error: 'since' must be an ISO time (2026-11-04T17:08:00) or relative "
                          f"(30s, 10m, 2h, 1d), got {since!r}")

    file = arguments.get("file", "latest")
    if not isinstance(file, str) or not file:
        return None, "Error: 'file' must be \"latest\", \"list\" or a file name from file=\"list\""
    if file not in ("latest", "list") and os.path.basename(file) != file:
        return None, "Error: 'file' must be a bare file name from file=\"list\", not a path"

    return {
        "lines": lines,
        "needles": tuple(c.lower() for c in contains),
        "required": required,
        "levels": levels,
        "since": since_time,
        "file": file,
    }, None


def _filters_summary(options, arguments):
    parts = []
    if arguments.get("preset"):
        parts.append(f"preset={arguments['preset']}")
    if options["levels"]:
        parts.append("level=" + "".join(c for c in LEVELS if c in options["levels"]))
    if options["needles"]:
        parts.append("contains=" + json.dumps(list(arguments["contains"])))
    if options["since"]:
        parts.append(f"since={options['since'].isoformat(timespec='seconds')}")
    return ", ".join(parts) or "none"


def tail_log(arguments, roots=None, now=None, max_scan_bytes=MAX_SCAN_BYTES):
    """Return the newest matching entries of Fusion's app log, oldest first."""
    arguments = arguments or {}
    now = now or datetime.now()
    options, failure = _validate(arguments, now)
    if failure:
        return error_result(failure)

    roots = roots if roots is not None else default_log_roots()
    files = find_log_files(roots)
    if not files:
        return error_result("Error: no Fusion app log found (looked for "
                      f"{LOG_GLOB} under: {', '.join(roots)})")
    if options["file"] == "list":
        return ok_result(describe_files(files))
    if options["file"] == "latest":
        mtime, size, path = files[0]
    else:
        chosen = [f for f in files if os.path.basename(f[2]) == options["file"]]
        if not chosen:
            return error_result(f"Error: no log file named {options['file']!r}; use file=\"list\"")
        mtime, size, path = chosen[0]

    stats = {}
    try:
        with open(path, "rb") as handle:
            entries = iter_entries_reverse(
                iter_lines_reverse(handle, size, max_bytes=max_scan_bytes, stats=stats))
            matched, since_reached = collect(
                entries, options["lines"], options["levels"], options["needles"],
                options["required"], options["since"])
    except OSError as exc:
        return error_result(f"Error: could not read {path}: {exc}")

    if len(matched) >= options["lines"]:
        reach = f"stopped at the {options['lines']}-entry limit"
    elif since_reached:
        reach = "reached `since`"
    elif stats.get("exhausted"):
        reach = "read the whole file"
    else:
        reach = f"stopped at the {_human_size(max_scan_bytes)} scan cap; older entries not searched"
    header = (
        f"{os.path.basename(path)} ({_human_size(size)}, modified "
        f"{datetime.fromtimestamp(mtime).isoformat(timespec='seconds')}); filters: "
        f"{_filters_summary(options, arguments)}; {len(matched)} entr"
        f"{'y' if len(matched) == 1 else 'ies'}, oldest first; scanned "
        f"{_human_size(stats.get('scanned', 0))}, {reach}"
    )
    body = [header, ""]
    for entry in reversed(matched):
        body.extend(format_entry(entry))
    if not matched:
        body.append("(no matching entries)")
    return ok_result("\n".join(body))
