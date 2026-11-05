"""Save a document and prove it saved; open a cloud file without duplicating it.

``Document.save()`` can return True while nothing reaches the cloud: when a
second in-memory instance of the same DataFile is open (Fusion titles it
"... (1)"), the save workflow starts but no upload follows, ``isModified``
stays true and no new version appears. ``save_document`` never trusts the
return value; it waits until the document is clean AND the DataFile's latest
version number has gone up, and explains a failure with the open instances
and Fusion's own app log. ``open_document`` reuses an open instance instead
of loading another one, which is how the duplicate gets created.

Neither tool ever closes a document: closing one also closes the documents
it links.
"""

import re
import time
import traceback
from datetime import datetime, timedelta

import adsk.core

from .common import cloud
from .common.api import safe, strict_items
from .common.app_log import default_log_roots, entry_time, newest_log, read_tail_lines
from .common.args import UNKNOWN_CAPITALIZED, int_or_none, is_int, is_number, reject_unknown, text_arg
from .common.results import tool_result
from .dispatch import get_app, log

DEFAULT_SAVE_TIMEOUT_S = 60.0
# The bridge abandons a main-thread call after settings.MCP_MAIN_THREAD_TIMEOUT
# (120 s); stay well inside it so the result always reaches the client.
MAX_SAVE_TIMEOUT_S = 110.0
_POLL_SLEEP_S = 0.1
_CLOUD_POLL_INTERVAL_S = 1.0

LOG_KEYWORDS = ("Workflow start: save", "CFSUploader", "Upload failed", "Uploaded document")
_LOG_TAIL_BYTES = 4 * 1024 * 1024
_LOG_MAX_LINES = 20
_LOG_LINE_MAX_CHARS = 400
_LOG_TS = re.compile(r"^(\d{8}T\d{6})\s")
# Fusion titles a second in-memory copy "Name (1)"; the document name may also
# carry the " v12" version suffix after it.
_INSTANCE_SUFFIX = re.compile(r"\((\d+)\)(?:\s+v\d+)?\s*$")

STUCK_INSTANCE_FIX = (
    "Close the stuck document tab WITHOUT saving, reopen the file fresh (open_document), "
    "redo the edit, then save again. Closing also closes documents it links, so save or "
    "note those first. This tool never closes anything itself."
)


# -- Small helpers -----------------------------------------------------------


def instance_suffix(name):
    """The "(1)"-style duplicate counter at the end of a document name, or None."""
    match = _INSTANCE_SUFFIX.search(name or "")
    return int(match.group(1)) if match else None


# -- Document snapshots (the only place that touches Document objects) --------


def describe_document(document):
    """JSON-safe snapshot of an open document."""
    data_file = safe(document, "dataFile")
    info = {
        "name": safe(document, "name"),
        "is_active": safe(document, "isActive") is True,
        "is_visible": safe(document, "isVisible"),
        "is_modified": safe(document, "isModified"),
        "data_file": None,
    }
    if data_file is not None:
        read_only = safe(data_file, "isReadOnly")
        project = safe(data_file, "parentProject")
        folder = safe(data_file, "parentFolder")
        info["data_file"] = {
            "id": safe(data_file, "id"),
            "name": safe(data_file, "name"),
            "version_number": safe(data_file, "versionNumber"),
            "is_read_only": read_only if isinstance(read_only, bool) else "unknown",
            "project": safe(project, "name") if project is not None else None,
            "folder": safe(folder, "name") if folder is not None else None,
        }
    return info


def _open_documents(app):
    documents = app.documents
    return [documents.item(i) for i in range(documents.count)]


def _file_id(info):
    return (info.get("data_file") or {}).get("id")


def _loaded_version(info):
    return int_or_none((info.get("data_file") or {}).get("version_number"))


# -- Pure decision logic -------------------------------------------------------


def instances_of(infos, file_id):
    """Indexes of open documents whose DataFile id is *file_id*."""
    if not file_id:
        return []
    return [i for i, info in enumerate(infos) if _file_id(info) == file_id]


def describe_instances(infos, indexes):
    """The fields that tell duplicate instances apart."""
    rows = []
    for i in indexes:
        info = infos[i]
        rows.append({
            "name": info.get("name"),
            "is_active": info.get("is_active"),
            "is_visible": info.get("is_visible"),
            "loaded_version": _loaded_version(info),
            "is_modified": info.get("is_modified"),
            "title_suffix": instance_suffix(info.get("name")),
        })
    return rows


def pick_target(infos, field, value):
    """Choose the document to save; returns (index, error_payload).

    *field* is None for the active document. Several instances of one DataFile
    are not ambiguous here: the active one wins, else the highest loaded
    version, and the save preflight then judges whether it is safe.
    """
    if field is None:
        active = [i for i, info in enumerate(infos) if info.get("is_active")]
        if not active:
            return None, {"error": "No document is active"}
        return active[0], None

    if field == "data_file_id":
        matches = instances_of(infos, value)
    else:
        matches = [i for i, info in enumerate(infos)
                   if value in (info.get("name"), (info.get("data_file") or {}).get("name"))]
    if not matches:
        return None, {
            "error": f"No open document matches {field}={value!r}",
            "open_documents": [{"name": info.get("name"), "data_file_id": _file_id(info)} for info in infos],
        }
    ids = {_file_id(infos[i]) for i in matches}
    if len(ids) > 1:
        return None, {
            "error": f"{len(matches)} open documents named {value!r} are different files; use data_file_id",
            "candidates": [{"name": infos[i].get("name"), "data_file_id": _file_id(infos[i]),
                            "project": (infos[i].get("data_file") or {}).get("project"),
                            "folder": (infos[i].get("data_file") or {}).get("folder")} for i in matches],
        }
    active = [i for i in matches if infos[i].get("is_active")]
    if active:
        return active[0], None
    return max(matches, key=lambda i: (_loaded_version(infos[i]) or 0, -i)), None


def save_preflight(infos, target):
    """Decide whether a save may be attempted; returns (refusal, warnings).

    Refuses a never-saved document (needs Save As), a read-only one (Personal
    Use: the save silently does nothing), and, when the file is open more than
    once, any instance that is not the newest loaded version: saving an older
    copy either fails silently (the duplicate-instance stall) or would write
    stale geometry over a newer version.
    """
    info = infos[target]
    data_file = info.get("data_file")
    if not data_file or not data_file.get("id"):
        return {"reason": "never_saved",
                "error": "Document has never been saved to the cloud; use Save As in Fusion first"}, []
    if data_file.get("is_read_only") is True:
        return {"reason": "read_only",
                "error": "Document is read-only (DataFile.isReadOnly): Fusion would accept the save and "
                         "write nothing. Common on Personal Use licences; the API cannot change it."}, []

    warnings = []
    if data_file.get("is_read_only") == "unknown":
        warnings.append("could not read DataFile.isReadOnly; the save is attempted anyway")
    same = instances_of(infos, data_file["id"])
    if len(same) > 1:
        versions = [_loaded_version(infos[i]) or 0 for i in same]
        newest = max(versions)
        mine = _loaded_version(info) or 0
        details = describe_instances(infos, same)
        if mine < newest:
            return {
                "reason": "older_duplicate",
                "error": (f"{len(same)} instances of this file are open and the target has v{mine} loaded "
                          f"while another has v{newest}. Saving the older copy would either stall (no "
                          f"upload) or overwrite newer work with stale geometry. Save the v{newest} instance "
                          f"instead, or close this one without saving."),
                "instances": details,
            }, []
        tied = versions.count(newest)
        warnings.append(
            f"{len(same)} instances of this file are open" +
            (f" ({tied} at v{newest})" if tied > 1 else "") +
            "; duplicate instances are known to make saves stall without uploading")
    if info.get("is_active") is not True:
        warnings.append("target is not the active document")
    return None, warnings


class SaveVerifier:
    """Decide from successive observations whether a save really landed.

    A save counts only when the document is clean (``isModified`` false) AND
    the cloud's latest version number is above the one recorded before the
    save. Feed ``observe`` until ``done``; ``timed_out`` ends it as a failure.
    """

    def __init__(self, version_before, timeout_s):
        self.version_before = version_before
        self.timeout_s = timeout_s
        self.is_modified = None
        self.latest_version = None
        self.saved = False
        self.done = False
        self.timed_out = False

    def observe(self, elapsed_s, is_modified=None, latest_version=None):
        """Record a poll; None leaves that reading unchanged. Returns ``done``."""
        if self.done:
            return True
        if is_modified is not None:
            self.is_modified = bool(is_modified)
        if is_int(latest_version):
            self.latest_version = latest_version
        if (self.is_modified is False and self.latest_version is not None
                and self.version_before is not None and self.latest_version > self.version_before):
            self.saved = True
            self.done = True
        elif elapsed_s >= self.timeout_s:
            self.timed_out = True
            self.done = True
        return self.done

    @property
    def stage(self):
        """Where the save got to, in words a failure report can use."""
        if self.saved:
            return "saved"
        bumped = (self.latest_version is not None and self.version_before is not None
                  and self.latest_version > self.version_before)
        if self.is_modified is not False and not bumped:
            return "nothing_happened"
        if self.is_modified is False and not bumped:
            return "clean_locally_but_no_new_version"
        return "new_version_but_still_modified"


def parse_log_time(line):
    """Timestamp of a Fusion app log line (local time, 1 s resolution), or None."""
    match = _LOG_TS.match(line)
    return entry_time(match.group(1)) if match else None


def filter_log_lines(lines, since, keywords=LOG_KEYWORDS, limit=_LOG_MAX_LINES):
    """Last *limit* lines at or after *since* that contain any keyword.

    Log stamps are truncated to whole seconds, so *since* is floored the same
    way. Lines without a stamp (continuations) inherit the previous stamp.
    """
    floor = since.replace(microsecond=0)
    current = None
    matched = []
    for raw in lines:
        line = raw.rstrip("\n")
        stamp = parse_log_time(line)
        if stamp is not None:
            current = stamp
        if current is None or current < floor:
            continue
        if any(key in line for key in keywords):
            matched.append(line[:_LOG_LINE_MAX_CHARS])
    return matched[-limit:] if limit else matched


def summarize_log(lines, file_name=None):
    """Classify the save workflow from filtered log lines."""
    save_started = any("Workflow start: save" in line for line in lines)
    upload_started = any("[CFSUploader] Starting upload" in line for line in lines)
    uploaded = [line for line in lines if "Uploaded document" in line]
    uploaded_this = bool(file_name) and any(f"Uploaded document {file_name}," in line for line in uploaded)
    if not lines:
        verdict = "no save activity logged since the save started"
    elif save_started and not upload_started:
        verdict = ("save workflow started but no upload began: the duplicate-instance stall "
                   "(a second in-memory copy of the file swallows the save)")
    elif upload_started and not uploaded:
        verdict = "upload started but has not finished"
    elif uploaded and file_name and not uploaded_this:
        verdict = "uploads finished, but none for this file"
    else:
        verdict = "upload reported finished"
    return {
        "save_workflow_started": save_started,
        "upload_started": upload_started,
        "uploaded_this_file": uploaded_this,
        "verdict": verdict,
        "note": "'[CFS] Upload failed ... PIM data publish is not finished' also appears after good saves",
    }


def save_recommendation(stage, duplicates, read_only):
    """One plain-language next step for a failed save."""
    if read_only is True:
        return "The document is read-only; copy it into a project you own (Save As) and edit that copy."
    if len(duplicates) > 1 or stage == "nothing_happened":
        return STUCK_INSTANCE_FIX
    if stage == "clean_locally_but_no_new_version":
        return ("Fusion marked the document clean but no new version is in the cloud yet; the upload may be "
                "queued. Check the Data Panel / job status before retrying, and if nothing appears, " +
                STUCK_INSTANCE_FIX[0].lower() + STUCK_INSTANCE_FIX[1:])
    return ("A new version appeared but the document is still modified (edits after the save?). "
            "Run document checks before saving again.")


def plan_open(infos, file_id, target_version):
    """Decide how to open a file; returns a dict with ``action`` reuse|open.

    Reuse an open instance already at *target_version* (the active one first).
    Otherwise open it, and report any other loaded versions that will now
    coexist with it as duplicates.
    """
    same = instances_of(infos, file_id)
    exact = [i for i in same if _loaded_version(infos[i]) == target_version]
    if exact:
        active = [i for i in exact if infos[i].get("is_active")]
        chosen = (active or exact)[0]
        return {"action": "reuse", "index": chosen, "others": [i for i in same if i != chosen]}
    return {"action": "open", "others": same}


def path_segments(path):
    """Split "Folder/Sub/File" into non-empty segments; the last is the file."""
    if not isinstance(path, str):
        raise ValueError("'path' must be a string like 'Folder/Sub/File name'")
    segments = cloud.path_segments(path)
    if not segments:
        raise ValueError("'path' must name a file, e.g. 'Folder/Sub/File name'")
    return segments


def match_named(items, name, label):
    """The one item whose name (or name.ext) equals *name*; ValueError otherwise."""
    hits = [item for item in items
            if name in (item["name"], f"{item['name']}.{item.get('ext')}" if item.get("ext") else None)]
    if not hits:
        raise ValueError(f"No {label} named {name!r}; found: {sorted(i['name'] for i in items)[:50]}")
    if len(hits) > 1:
        ids = ", ".join(f"{h['name']} ({h.get('ext') or '?'}, id {h.get('id')})" for h in hits)
        raise ValueError(f"{len(hits)} {label}s named {name!r}: {ids}; use data_file_id")
    return hits[0]


# -- Validation ---------------------------------------------------------------

_SAVE_ARGS = {"description", "name", "data_file_id", "timeout_seconds"}
_OPEN_ARGS = {"data_file_id", "project", "path", "version"}


def validate_save(arguments):
    """Return (description, selector_field, selector_value, timeout_s)."""
    reject_unknown(arguments, _SAVE_ARGS, UNKNOWN_CAPITALIZED)
    description = arguments.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("'description' (the version comment) must be a non-empty string")
    name = text_arg(arguments, "name", strip=True)
    file_id = text_arg(arguments, "data_file_id", strip=True)
    if name and file_id:
        raise ValueError("give at most one of 'name' or 'data_file_id'")
    timeout_s = arguments.get("timeout_seconds", DEFAULT_SAVE_TIMEOUT_S)
    if not is_number(timeout_s) or timeout_s <= 0 or timeout_s > MAX_SAVE_TIMEOUT_S:
        raise ValueError(f"'timeout_seconds' must be a number in (0, {MAX_SAVE_TIMEOUT_S:.0f}]")
    field = "name" if name else ("data_file_id" if file_id else None)
    return description.strip(), field, name or file_id, float(timeout_s)


def validate_open(arguments):
    """Return (data_file_id, project, segments, version)."""
    reject_unknown(arguments, _OPEN_ARGS, UNKNOWN_CAPITALIZED)
    file_id = text_arg(arguments, "data_file_id", strip=True)
    project = text_arg(arguments, "project", strip=True)
    path = arguments.get("path")
    if file_id and (project or path is not None):
        raise ValueError("give either 'data_file_id' or 'project' + 'path', not both")
    if not file_id and not (project and path is not None):
        raise ValueError("give 'data_file_id', or both 'project' and 'path'")
    segments = path_segments(path) if not file_id else None
    version = arguments.get("version")
    if version is not None and (not is_int(version) or version < 1):
        raise ValueError("'version' must be a positive integer")
    return file_id, project, segments, version


# -- Fusion glue: save_document ----------------------------------------------


def _cloud_latest_version(app, file_id):
    """``latestVersionNumber`` straight from the cloud lookup, or None."""
    try:
        data_file = app.data.findFileById(file_id)
    except Exception:
        return None
    return int_or_none(safe(data_file, "latestVersionNumber")) if data_file is not None else None


def _current_file_id(document, fallback):
    """The document's DataFile id now: it is a local file path until the first upload lands."""
    data_file = safe(document, "dataFile")
    value = safe(data_file, "id") if data_file is not None else None
    return value if isinstance(value, str) and value else fallback


def _log_evidence(started_wall, file_name):
    path = newest_log(default_log_roots())
    if path is None:
        return {"log_file": None, "note": "no Fusion AppLogFile*.log found"}
    try:
        lines = filter_log_lines(read_tail_lines(path, _LOG_TAIL_BYTES), started_wall)
    except Exception as exc:
        return {"log_file": path, "note": f"could not read log: {exc}"}
    return {"log_file": path, "summary": summarize_log(lines, file_name), "lines": lines}


def save_document(arguments):
    """Save a document with a version comment and verify a new version landed."""
    try:
        description, field, value, timeout_s = validate_save(arguments)
    except ValueError as exc:
        return tool_result(f"Error: {exc}", True)

    try:
        app = get_app()
        docs = _open_documents(app)
        infos = [describe_document(doc) for doc in docs]
        target, error = pick_target(infos, field, value)
        if error:
            return tool_result(error, True)
        refusal, warnings = save_preflight(infos, target)
        if refusal:
            refusal = {"saved": False, "attempted": False, "document": infos[target].get("name"), **refusal}
            if refusal["reason"] == "older_duplicate":
                refusal["recommendation"] = STUCK_INSTANCE_FIX
            return tool_result(refusal, True)

        document = docs[target]
        info = infos[target]
        file_id = info["data_file"]["id"]
        file_name = info["data_file"].get("name")
        version_before = _cloud_latest_version(app, file_id)
        if version_before is None:
            version_before = _loaded_version(info)
        if info.get("is_modified") is False:
            return tool_result({
                "saved": False, "attempted": False, "document": info.get("name"),
                "reason": "not_modified",
                "note": "No unsaved changes, so Fusion would not create a new version.",
                "version": version_before,
            })

        started_wall = datetime.now() - timedelta(seconds=1)
        started = time.monotonic()
        returned = document.save(description)
        log(f"[MCP] save_document {info.get('name')!r}: save() returned {returned!r}; verifying")

        # The upload runs on Fusion's event loop, so pump events while polling.
        # The cloud lookup is a network call: ask it at most once a second.
        verifier = SaveVerifier(version_before, timeout_s)
        last_cloud = None
        while True:
            adsk.doEvents()
            now = time.monotonic()
            latest = None
            if last_cloud is None or now - last_cloud >= _CLOUD_POLL_INTERVAL_S:
                latest = _cloud_latest_version(app, _current_file_id(document, file_id))
                last_cloud = now
            if verifier.observe(now - started, safe(document, "isModified"), latest):
                break
            time.sleep(_POLL_SLEEP_S)
        elapsed = round(time.monotonic() - started, 2)

        result = {
            "saved": verifier.saved,
            "document": info.get("name"),
            "data_file_id": file_id,
            "description": description,
            "save_returned": returned if isinstance(returned, bool) else repr(returned),
            "version_before": version_before,
            "version_after": verifier.latest_version,
            "is_modified_after": verifier.is_modified,
            "elapsed_seconds": elapsed,
        }
        if warnings:
            result["warnings"] = warnings
        if verifier.saved:
            log(f"[MCP] save_document: v{version_before} -> v{verifier.latest_version} in {elapsed}s")
            return tool_result(result)

        # Failure: explain it from fresh readings.
        infos_after = [describe_document(doc) for doc in _open_documents(app)]
        same = instances_of(infos_after, file_id)
        read_only = safe(safe(document, "dataFile"), "isReadOnly")
        result["stage"] = verifier.stage
        result["diagnosis"] = {
            "instances_open": len(same),
            "instances": describe_instances(infos_after, same),
            "title_suffix": instance_suffix(info.get("name")),
            "is_read_only": read_only if isinstance(read_only, bool) else "unknown",
            "app_log": _log_evidence(started_wall, file_name),
        }
        result["recommendation"] = save_recommendation(verifier.stage, same, read_only)
        log(f"[MCP] save_document: NOT saved after {elapsed}s ({verifier.stage})")
        return tool_result(result, True)
    except Exception as exc:
        log(f"[MCP] save_document failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)


# -- Fusion glue: open_document ----------------------------------------------


def _match_object(objects, name, label):
    rows = [{"name": safe(o, "name"), "ext": safe(o, "fileExtension"), "id": safe(o, "id"), "obj": o} for o in objects]
    return match_named(rows, name, label)["obj"]


def _resolve_path(app, project, segments):
    found = cloud.resolve_project(strict_items(app.data.dataProjects), project, _match_object)
    return cloud.resolve_path(found.rootFolder, segments, _match_object)


def _version_file(data_file, version):
    """The DataFile for *version* of *data_file* (from ``DataFile.versions``)."""
    versions = data_file.versions
    for i in range(versions.count):
        candidate = versions.item(i)
        if safe(candidate, "versionNumber") == version:
            return candidate
    return None


def open_document(arguments):
    """Open a cloud file (or reuse an already-open instance) and activate it."""
    try:
        file_id, project, segments, version = validate_open(arguments)
    except ValueError as exc:
        return tool_result(f"Error: {exc}", True)

    try:
        app = get_app()
        if file_id:
            data_file = app.data.findFileById(file_id)
            if data_file is None:
                return tool_result(f"Error: no file with data_file_id {file_id!r}", True)
        else:
            try:
                data_file = _resolve_path(app, project, segments)
            except ValueError as exc:
                return tool_result(f"Error: {exc}", True)
            file_id = safe(data_file, "id")

        latest = safe(data_file, "latestVersionNumber")
        target_version = version if version is not None else latest
        if not isinstance(target_version, int):
            return tool_result("Error: could not read the file's latest version number; pass 'version'", True)
        if isinstance(latest, int) and target_version > latest:
            return tool_result(f"Error: 'version' {version} is newer than the latest version (v{latest})", True)

        docs = _open_documents(app)
        infos = [describe_document(doc) for doc in docs]
        plan = plan_open(infos, file_id, target_version)
        warnings = []
        if plan["action"] == "reuse":
            document = docs[plan["index"]]
            if not infos[plan["index"]]["is_active"] and not document.activate():
                return tool_result(f"Error: Fusion refused to activate {infos[plan['index']]['name']!r}", True)
        else:
            to_open = data_file
            if version is not None and version != latest:
                to_open = _version_file(data_file, version)
                if to_open is None:
                    return tool_result(f"Error: version {version} not found in DataFile.versions", True)
            if plan["others"]:
                loaded = sorted({_loaded_version(infos[i]) for i in plan["others"]} - {None})
                warnings.append(
                    f"this file was already open at {', '.join(f'v{v}' for v in loaded)}; opening "
                    f"v{target_version} creates another instance. Duplicate instances make saves stall: "
                    f"close the one you will not edit (without saving) before saving.")
            document = app.documents.open(to_open, True)
            if document is None:
                return tool_result("Error: Fusion did not open the document", True)
            if not safe(document, "isActive"):
                document.activate()

        infos_after = [describe_document(doc) for doc in _open_documents(app)]
        same = instances_of(infos_after, file_id)
        result_info = describe_document(document)
        result_info["latest_version_number"] = latest
        if result_info.get("data_file", {}) and result_info["data_file"].get("is_read_only") is True:
            warnings.append("document is read-only: saves will not stick")
        if len(same) > 1 and plan["action"] == "reuse":
            warnings.append(f"{len(same)} instances of this file are open; saves may stall until extras close")
        result = {
            "reused": plan["action"] == "reuse",
            "requested_version": version,
            "document": result_info,
            "instances_open": len(same),
            "instances": describe_instances(infos_after, same),
            "warnings": warnings,
        }
        log(f"[MCP] open_document {result_info.get('name')!r}: {'reused' if result['reused'] else 'opened'}")
        return tool_result(result)
    except Exception as exc:
        log(f"[MCP] open_document failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)
