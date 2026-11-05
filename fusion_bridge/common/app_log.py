"""Locate and read Fusion's application log, without touching the Fusion API.

Fusion writes ``AppLogFile<session start>.log`` under
``~/Library/Application Support/Autodesk/Autodesk Fusion 360/<user id>/logs``
(``%LOCALAPPDATA%\\Autodesk\\Autodesk Fusion 360\\<user id>\\logs`` on Windows).
One file spans the whole session, often days and tens of MB, so the newest by
mtime is the live one.

Entry format: ``20261104T170849 I 24564113 [CFSUploader] Starting upload ...``
(timestamp in local time, level I/W/E/D, thread id, message). Lines that do not
start with a timestamp (HTTP request/response dumps) continue the entry above.

Cloud writes fail with ``RuntimeError: 2 : InternalValidationError : res``, which
says nothing. The real reason is only in the log, as the failed request with its
response body under it::

    20261105T113355 E .24563490 POST 423 https://.../entities/urn%3A...%3Aco.1HAh.../rename?newName=Sub - ...
    Request Body: [Empty body]
    Response Body: {"errorCode":"LOCK_RESOURCE_IS_LOCKED","message":"Resource is locked by other operation. ..."}

A caller marks the log before the call. Only entries after the mark that name the
resource count, so an older failure or another file's is never reported as this
one.
"""

import glob
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import unquote

INTERNAL_ERROR = "InternalValidationError"
LOCKED_CODE = "LOCK_RESOURCE_IS_LOCKED"
LOCKED_STATUS = 423

LOG_GLOB = os.path.join("*", "logs", "AppLogFile*.log")
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"
LOG_TAIL_BYTES = 256 * 1024
# The failure entry can land in the log just after the call raises.
LOG_POLLS = 6
_LOG_POLL_INTERVAL_S = 0.1
_MAX_LOG_READ_BYTES = 4 * 1024 * 1024
_MAX_BODY_CHARS = 500

_HEADER = re.compile(r"^(\d{8}T\d{6}) ([A-Z]) (\S+) ?(.*)$")
# GETs are left out: Fusion's own background reads 404 around any folder change.
_WRITE_REQUEST = re.compile(r"^(?:POST|PUT|PATCH|DELETE) (\d{3}) (\S+)(.*)$")
_REQUEST_ID = re.compile(r"requestId:(\S+)")
_REQUEST_BODY = "Request Body:"
_RESPONSE_BODY = "Response Body:"


# -- Locating log files --------------------------------------------------------


def default_log_roots():
    """Directories that hold ``<user id>/logs/AppLogFile*.log``."""
    roots = [os.path.expanduser(
        os.path.join("~", "Library", "Application Support", "Autodesk", "Autodesk Fusion 360"))]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(os.path.join(local_app_data, "Autodesk", "Autodesk Fusion 360"))
    return roots


def find_log_files(roots):
    """``(mtime, size, path)`` of every app log under *roots*, newest first.

    Equal mtimes fall back to the name, which ends in the session's start time.
    """
    found = []
    for root in roots:
        for path in glob.glob(os.path.join(glob.escape(root), LOG_GLOB)):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            found.append((stat.st_mtime, stat.st_size, path))
    found.sort(key=lambda item: (item[0], item[2]), reverse=True)
    return found


def newest_log(roots):
    """Path of the live app log (newest mtime under *roots*), or None."""
    files = find_log_files(roots)
    return files[0][2] if files else None


def read_tail_lines(path, max_bytes=LOG_TAIL_BYTES):
    """Complete lines from the last *max_bytes* of *path*; the log runs to tens of MB."""
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read(max_bytes)
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start else lines


# -- Parsing -------------------------------------------------------------------


def parse_header(line):
    """``(timestamp, level, thread, message)`` for an entry's first line, else None."""
    match = _HEADER.match(line)
    return match.groups() if match else None


def entry_time(timestamp):
    """The local time of a log timestamp, or None when it is not a real time."""
    try:
        return datetime.strptime(timestamp, TIMESTAMP_FORMAT)
    except ValueError:
        return None


def parse_requests(text, request=_WRITE_REQUEST):
    """Request entries in app log *text*, oldest first.

    *request* matches an entry's message with three groups: status, url and the
    rest of the line. An entry is its timestamped line plus the untimestamped
    lines under it. Returns dicts with ``status``, ``url``, ``request_id``,
    ``request_body`` and ``response_body``.
    """
    entries = []
    current = None
    for line in text.splitlines():
        header = parse_header(line)
        if header:
            current = None
            found = request.match(header[3])
            if found:
                request_id = _REQUEST_ID.search(found.group(3))
                current = {"status": int(found.group(1)), "url": found.group(2),
                           "request_id": request_id.group(1) if request_id else None,
                           "request_body": None, "response_body": None}
                entries.append(current)
        elif current is not None:
            stripped = line.strip()
            if stripped.startswith(_REQUEST_BODY):
                current["request_body"] = stripped[len(_REQUEST_BODY):].strip()
            elif stripped.startswith(_RESPONSE_BODY):
                current["response_body"] = stripped[len(_RESPONSE_BODY):].strip()
    return entries


def parse_failures(text):
    """Failed write requests in app log *text*, oldest first; see ``parse_requests``."""
    return [entry for entry in parse_requests(text) if entry["status"] >= 400]


def failure_reason(entry):
    """What the server said in a failed request entry."""
    reason = {"status": entry["status"], "error_code": None, "message": None, "request_id": entry["request_id"]}
    body = entry["response_body"]
    try:
        parsed = json.loads(body)
        reason["error_code"] = parsed.get("errorCode")
        reason["message"] = parsed.get("message")
    except (TypeError, ValueError, AttributeError):
        if body:
            reason["message"] = body[:_MAX_BODY_CHARS]
    return reason


def resource_token(resource_id):
    """The part of a urn shared by its forms: a file's lineage and ``vf.`` version ids differ otherwise."""
    return resource_id.rsplit(":", 1)[-1].split("?", 1)[0].removeprefix("vf.")


def server_reason(text, resource_id):
    """The server's reason for the last failed write naming *resource_id* in *text*, or None."""
    token = resource_token(resource_id)
    failed = [e for e in parse_failures(text)
              if token in unquote(e["url"]) or token in (e["request_body"] or "")]
    return failure_reason(failed[-1]) if failed else None


# -- Reading after a failure ---------------------------------------------------


def poll_reason(read_reason, polls=LOG_POLLS, sleep=time.sleep):
    """Call *read_reason* until it finds something, with a short sleep between tries."""
    for attempt in range(polls):
        if attempt:
            sleep(_LOG_POLL_INTERVAL_S)
        reason = read_reason()
        if reason is not None:
            return reason
    return None


class ServerLog:
    """Read what Fusion appended to its app log since a mark. A None path reads nothing."""

    def __init__(self, path=None):
        self.path = path

    def mark(self):
        try:
            return os.path.getsize(self.path) if self.path else None
        except OSError:
            return None

    def since(self, offset):
        if offset is None:
            return ""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                return fh.read(_MAX_LOG_READ_BYTES).decode("utf-8", "replace")
        except OSError:
            return ""

    def reason(self, offset, resource_id, *, polls=LOG_POLLS, sleep=time.sleep):
        """Poll after a failed call; return the server's reason or None."""
        if offset is None:
            return None
        return poll_reason(lambda: server_reason(self.since(offset), resource_id), polls, sleep)


def current_log():
    return ServerLog(newest_log(default_log_roots()))
