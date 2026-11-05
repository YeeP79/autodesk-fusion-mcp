"""The server's reason for a failed cloud call, read from Fusion's app log.

Cloud writes fail with ``RuntimeError: 2 : InternalValidationError : res``, which
says nothing. The real reason is only in Fusion's app log, as the failed request
with its response body under it::

    20261105T113355 E .24563490 POST 423 https://.../entities/urn%3A...%3Aco.1HAh.../rename?newName=Sub - ...
    Request Body: [Empty body]
    Response Body: {"errorCode":"LOCK_RESOURCE_IS_LOCKED","message":"Resource is locked by other operation. ..."}

A caller marks the log before the call. Only entries after the mark that name the
resource count, so an older failure or another file's is never reported as this
one. Fusion-free, so it can be tested with plain files.
"""

import glob
import json
import os
import re
import time
from urllib.parse import unquote

INTERNAL_ERROR = "InternalValidationError"
LOCKED_CODE = "LOCK_RESOURCE_IS_LOCKED"
LOCKED_STATUS = 423

# The failure entry can land in the log just after the call raises.
_LOG_POLLS = 6
_LOG_POLL_INTERVAL_S = 0.1
_MAX_LOG_READ_BYTES = 4 * 1024 * 1024
_MAX_BODY_CHARS = 500

_LOG_GLOB = os.path.join("*", "logs", "AppLogFile*.log")
_LOG_HEADER = re.compile(r"^\d{8}T\d{6} [A-Z] \S+ ?(.*)$")
# GETs are left out: Fusion's own background reads 404 around any folder change.
_REQUEST = re.compile(r"^(?:POST|PUT|PATCH|DELETE) (\d{3}) (\S+)(.*)$")
_REQUEST_ID = re.compile(r"requestId:(\S+)")
_REQUEST_BODY = "Request Body:"
_RESPONSE_BODY = "Response Body:"


def parse_failures(text):
    """Failed write requests in app log *text*, oldest first.

    An entry is its timestamped line plus the untimestamped lines under it. Returns
    dicts with ``status``, ``url``, ``request_id``, ``request_body`` and ``response_body``.
    """
    entries = []
    current = None
    for line in text.splitlines():
        header = _LOG_HEADER.match(line)
        if header:
            current = None
            found = _REQUEST.match(header.group(1))
            if found and int(found.group(1)) >= 400:
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


def resource_token(resource_id):
    """The part of a urn shared by its forms: a file's lineage and ``vf.`` version ids differ otherwise."""
    return resource_id.rsplit(":", 1)[-1].split("?", 1)[0].removeprefix("vf.")


def server_reason(text, resource_id):
    """The server's reason for the last failed write naming *resource_id* in *text*, or None."""
    token = resource_token(resource_id)
    failed = [e for e in parse_failures(text)
              if token in unquote(e["url"]) or token in (e["request_body"] or "")]
    if not failed:
        return None
    entry = failed[-1]
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


def default_log_roots():
    roots = [os.path.expanduser(os.path.join("~", "Library", "Application Support", "Autodesk",
                                             "Autodesk Fusion 360"))]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(os.path.join(local_app_data, "Autodesk", "Autodesk Fusion 360"))
    return roots


def newest_log(roots):
    """The live app log (newest mtime under *roots*), or None."""
    found = []
    for root in roots:
        for path in glob.glob(os.path.join(glob.escape(root), _LOG_GLOB)):
            try:
                found.append((os.path.getmtime(path), path))
            except OSError:
                continue
    return max(found)[1] if found else None


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

    def reason(self, offset, resource_id, *, polls=_LOG_POLLS, sleep=time.sleep):
        """Poll after a failed call; return the server's reason or None."""
        if offset is None:
            return None
        for attempt in range(polls):
            if attempt:
                sleep(_LOG_POLL_INTERVAL_S)
            reason = server_reason(self.since(offset), resource_id)
            if reason is not None:
                return reason
        return None


def current_log():
    return ServerLog(newest_log(default_log_roots()))
