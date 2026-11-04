"""Report which cloud documents are editable under Fusion for Personal Use.

Personal Use allows a small number of editable documents; everything else
opens read-only and saves silently do nothing. ``DataFile.isReadOnly`` and
``Data.personalUseLimits`` are readable, but the API has no setter: the user
switches a file in the Data Panel (right-click > Make Editable / Make
Read-Only). This tool only reads. It walks the active hub's projects, lists
editable designs and adds heuristic "safe to release" hints for each one.

The walk, summary and hints are plain functions over duck-typed objects
(``count``/``item(i)`` collections, attribute reads) so they test with fakes.
"""

import json
import time
import traceback

from .dispatch import get_app, log

DEFAULT_EDITABLE_LIMIT = 10
DEFAULT_MAX_FILES = 500
MAX_MAX_FILES = 20000
DEFAULT_MAX_DEPTH = 8
MAX_MAX_DEPTH = 64
DEFAULT_STALE_DAYS = 14
DESIGN_EXTENSIONS = ("f3d",)
MAX_REFERENCE_NAMES = 10
_SECONDS_PER_DAY = 86400.0

HINTS_NOTE = (
    "Hints are heuristics, not guarantees. Fusion's API cannot change a file's editable "
    "state: right-click the file in the Data Panel and choose Make Read-Only / Make Editable."
)


def _error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_arguments(arguments):
    """Return (options, None) or (None, error message)."""
    project = arguments.get("project")
    if project is not None and (not isinstance(project, str) or not project.strip()):
        return None, "Error: 'project' must be a non-empty string"
    limit = arguments.get("editable_limit")
    if limit is not None and (not _is_int(limit) or limit < 1):
        return None, "Error: 'editable_limit' must be a positive integer"
    max_files = arguments.get("max_files", DEFAULT_MAX_FILES)
    if not _is_int(max_files) or not 1 <= max_files <= MAX_MAX_FILES:
        return None, f"Error: 'max_files' must be an integer from 1 to {MAX_MAX_FILES}"
    max_depth = arguments.get("max_depth", DEFAULT_MAX_DEPTH)
    if not _is_int(max_depth) or not 0 <= max_depth <= MAX_MAX_DEPTH:
        return None, f"Error: 'max_depth' must be an integer from 0 to {MAX_MAX_DEPTH}"
    stale_days = arguments.get("stale_days", DEFAULT_STALE_DAYS)
    if not _is_number(stale_days) or stale_days < 0:
        return None, "Error: 'stale_days' must be a non-negative number"
    include_read_only = arguments.get("include_read_only", False)
    if not isinstance(include_read_only, bool):
        return None, "Error: 'include_read_only' must be a boolean"
    return {
        "project": project.strip() if project else None,
        "editable_limit": limit,
        "max_files": max_files,
        "max_depth": max_depth,
        "stale_days": stale_days,
        "include_read_only": include_read_only,
    }, None


# -- Safe reads -------------------------------------------------------------


def _safe(obj, name, default=None):
    """Read an attribute; cloud-backed properties can raise or be missing."""
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def _items(collection):
    """Yield items of a Fusion collection (count/item) or a plain list."""
    if collection is None:
        return
    if isinstance(collection, (list, tuple)):
        yield from collection
        return
    count = _safe(collection, "count", 0)
    for i in range(count):
        try:
            item = collection.item(i)
        except Exception:
            continue
        if item is not None:
            yield item


def is_design(data_file):
    return str(_safe(data_file, "fileExtension", "")).lower() in DESIGN_EXTENSIONS


def project_matches(project_name, project_filter):
    """Case-insensitive exact match wins; otherwise substring."""
    if project_filter is None:
        return True
    return project_filter.lower() in (project_name or "").lower()


def select_projects(projects, project_filter):
    """Return projects matching *project_filter*; an exact name match beats substrings."""
    projects = list(projects)
    if project_filter is None:
        return projects
    exact = [p for p in projects if (_safe(p, "name", "") or "").lower() == project_filter.lower()]
    if exact:
        return exact
    return [p for p in projects if project_matches(_safe(p, "name", ""), project_filter)]


# -- Walk -------------------------------------------------------------------


def file_entry(data_file, path_parts):
    """Plain-dict description of one DataFile (keeps the object for hints)."""
    name = _safe(data_file, "name", "")
    return {
        "path": "/".join([*path_parts, name]),
        "name": name,
        "id": _safe(data_file, "id"),
        "version": _safe(data_file, "versionNumber"),
        "is_read_only": bool(_safe(data_file, "isReadOnly", True)),
        "date_created": _safe(data_file, "dateCreated"),
        "date_modified": _safe(data_file, "dateModified"),
        "_file": data_file,
    }


def walk_projects(projects, max_depth, max_files):
    """Depth-first walk of each project's folders collecting Fusion designs.

    *max_depth* counts folder levels below the project root (0 = root only).
    Stops after *max_files* design files. Returns (entries, info).
    """
    entries = []
    info = {
        "projects_walked": [],
        "folders_visited": 0,
        "folders_skipped_depth": 0,
        "non_design_files": 0,
        "errors": [],
        "truncated": False,
    }

    def visit(folder, parts, depth):
        if info["truncated"]:
            return
        info["folders_visited"] += 1
        files = _safe(folder, "dataFiles")
        if files is None:
            info["errors"].append(f"{'/'.join(parts)}: could not read files")
        for data_file in _items(files):
            if not is_design(data_file):
                info["non_design_files"] += 1
                continue
            if len(entries) >= max_files:
                info["truncated"] = True
                return
            entries.append(file_entry(data_file, parts))
        for sub in _items(_safe(folder, "dataFolders")):
            if info["truncated"]:
                return
            if depth >= max_depth:
                info["folders_skipped_depth"] += 1
                continue
            visit(sub, [*parts, _safe(sub, "name", "?")], depth + 1)

    for project in projects:
        if info["truncated"]:
            break
        name = _safe(project, "name", "?")
        info["projects_walked"].append(name)
        root = _safe(project, "rootFolder")
        if root is None:
            info["errors"].append(f"{name}: no root folder")
            continue
        visit(root, [name], 0)
    return entries, info


# -- Hints ------------------------------------------------------------------


def last_changed(entry):
    """Latest of dateCreated/dateModified (a save creates a new version, so the
    latest version's creation date is the last save; dateModified covers renames)."""
    stamps = [s for s in (entry.get("date_created"), entry.get("date_modified")) if _is_number(s)]
    return max(stamps) if stamps else None


def reference_names(data_file, attribute):
    return [_safe(f, "name", "?") for f in _items(_safe(data_file, attribute))]


def release_hints(entry, open_files, now, stale_days):
    """Heuristic hints on whether *entry* (an editable file) can be made read-only.

    *open_files* maps DataFile id -> {"modified": bool, "active": bool}.
    """
    hints = []
    blockers = []
    doc = open_files.get(entry.get("id"))
    if doc is None:
        hints.append("not open in Fusion right now")
    elif doc.get("modified"):
        blockers.append("open with unsaved changes: save first (unsaved changes are lost once read-only)")
    else:
        blockers.append("open in Fusion right now" + (" (active document)" if doc.get("active") else ""))

    changed = last_changed(entry)
    age_days = None if changed is None else round((now - changed) / _SECONDS_PER_DAY, 1)
    if age_days is None:
        hints.append("last change date unknown")
    elif age_days >= stale_days:
        hints.append(f"not changed for {age_days} days (threshold {stale_days})")
    else:
        blockers.append(f"changed {age_days} days ago (within {stale_days} days)")

    data_file = entry.get("_file")
    parents = reference_names(data_file, "parentReferences") if _safe(data_file, "hasParentReferences", False) else []
    children = reference_names(data_file, "childReferences") if _safe(data_file, "hasChildReferences", False) else []
    if parents:
        hints.append(
            f"linked into {len(parents)} other design(s): they still load it read-only, "
            "but Edit In Place on it from those assemblies needs it editable"
        )

    result = {
        "likely_safe": not blockers,
        "reasons": hints,
        "blockers": blockers,
        "days_since_change": age_days,
    }
    if parents:
        result["parent_references"] = parents[:MAX_REFERENCE_NAMES]
        if len(parents) > MAX_REFERENCE_NAMES:
            result["parent_references_truncated"] = len(parents)
    if children:
        result["child_reference_count"] = len(children)
    return result


# -- Summary ----------------------------------------------------------------


def personal_use_summary(limits):
    """Read PersonalUseLimits (None for paid licences) into a plain dict."""
    if limits is None:
        return None
    files = list(_items(_safe(limits, "editableFiles", [])))
    return {
        "max_editable": _safe(limits, "maxEditableFileCount"),
        "editable_count": _safe(limits, "editableFileCount"),
        "files": files,
    }


def summarize(entries, walk_info, personal_use, open_files, options, now):
    """Build the report dict from walk entries and the licence's own counts."""
    by_id = {e["id"]: e for e in entries if e.get("id")}
    api_ids = set()
    if personal_use:
        for data_file in personal_use["files"]:
            file_id = _safe(data_file, "id")
            api_ids.add(file_id)
            if file_id not in by_id:
                # Editable but outside the walk (other hub, filter, or cap):
                # still report it; Fusion gives no folder path from here.
                project = _safe(_safe(data_file, "parentProject"), "name", "?")
                entry = file_entry(data_file, [project, "..."])
                entry["is_read_only"] = False
                entry["outside_walk"] = True
                by_id[file_id or entry["path"]] = entry

    editable = [e for e in by_id.values() if not e["is_read_only"] or e.get("id") in api_ids]
    editable.sort(key=lambda e: e["path"].lower())

    if options["editable_limit"] is not None:
        limit, limit_source = options["editable_limit"], "argument"
    elif personal_use and _is_int(personal_use.get("max_editable")):
        limit, limit_source = personal_use["max_editable"], "personalUseLimits.maxEditableFileCount"
    else:
        limit, limit_source = DEFAULT_EDITABLE_LIMIT, "default"

    count = personal_use["editable_count"] if personal_use and _is_int(personal_use.get("editable_count")) else len(editable)
    rows = []
    for entry in editable:
        row = {k: v for k, v in entry.items() if not k.startswith("_") and k not in ("date_created", "date_modified")}
        row["in_personal_use_list"] = entry.get("id") in api_ids if personal_use else None
        row["safe_to_release_hints"] = release_hints(entry, open_files, now, options["stale_days"])
        rows.append(row)

    report = {
        "license": "personal_use" if personal_use else "not_personal_use_or_unknown",
        "editable_count": count,
        "editable_limit": limit,
        "limit_source": limit_source,
        "slots_free": limit - count,
        "editable_files": rows,
        "design_files_scanned": len(entries),
        "read_only_files_scanned": sum(1 for e in entries if e["is_read_only"]),
        "walk": {k: v for k, v in walk_info.items() if k != "errors" or v},
        "hints_note": HINTS_NOTE,
    }
    if walk_info.get("truncated"):
        report["walk"]["truncated_reason"] = f"stopped after max_files={options['max_files']} design files"
    if not personal_use:
        report["note"] = (
            "Data.personalUseLimits is null: this is not a Personal Use licence (or Fusion did not report it). "
            "Every design writable to you shows as editable."
        )
    if options["include_read_only"]:
        report["read_only_files"] = [
            {"path": e["path"], "version": e["version"]} for e in entries if e["is_read_only"] and e.get("id") not in api_ids
        ]
    return report


def open_file_states(documents):
    """Map DataFile id -> state for open documents that are saved to the cloud."""
    states = {}
    for doc in _items(documents):
        data_file = _safe(doc, "dataFile")
        file_id = _safe(data_file, "id")
        if file_id:
            states[file_id] = {"modified": bool(_safe(doc, "isModified", False)),
                               "active": bool(_safe(doc, "isActive", False))}
    return states


def editable_files(arguments):
    """Report editable designs against the Personal Use limit, with release hints."""
    options, err = validate_arguments(arguments)
    if err:
        return _error(err)
    try:
        app = get_app()
        data = app.data
        started = time.monotonic()
        all_projects = list(_items(_safe(data, "dataProjects")))
        projects = select_projects(all_projects, options["project"])
        if options["project"] and not projects:
            names = sorted(_safe(p, "name", "?") for p in all_projects)
            return _error(f"Error: no project matches {options['project']!r}. Projects: {names}")

        entries, walk_info = walk_projects(projects, options["max_depth"], options["max_files"])
        personal_use = personal_use_summary(_safe(data, "personalUseLimits"))
        report = summarize(entries, walk_info, personal_use, open_file_states(app.documents), options, time.time())
        report["hub"] = _safe(_safe(data, "activeHub"), "name")
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        log(f"[MCP] editable_files: {report['editable_count']}/{report['editable_limit']} editable, "
            f"{len(entries)} designs scanned in {report['elapsed_seconds']}s")
        return {"content": [{"type": "text", "text": json.dumps(report, indent=2)}], "isError": False}
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] editable_files failed: {exc}")
        return _error(f"Error: {exc}\n{tb}")
