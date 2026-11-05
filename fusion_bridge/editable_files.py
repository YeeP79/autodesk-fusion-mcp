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

import time
import traceback

from .common.api import items, present
from .common.results import error_result, ok_result
from .dispatch import get_app, log

DEFAULT_EDITABLE_LIMIT = 10
DEFAULT_MAX_FILES = 500
MAX_MAX_FILES = 20000
DEFAULT_MAX_DEPTH = 8
MAX_MAX_DEPTH = 64
DEFAULT_STALE_DAYS = 14
# The walk runs on Fusion's main thread, which the bridge gives 120 s per call;
# stop early enough to leave time for the hints and the reply.
DEFAULT_TIME_BUDGET_S = 90.0
MAX_TIME_BUDGET_S = 110.0
IDS_SAMPLE_SIZE = 3
DESIGN_EXTENSIONS = ("f3d",)
MAX_REFERENCE_NAMES = 10
_SECONDS_PER_DAY = 86400.0

HINTS_NOTE = (
    "Hints are heuristics, not guarantees. Fusion's API cannot change a file's editable "
    "state: right-click the file in the Data Panel and choose Make Read-Only / Make Editable."
)


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
    budget = arguments.get("time_budget_s", DEFAULT_TIME_BUDGET_S)
    if not _is_number(budget) or not 1 <= budget <= MAX_TIME_BUDGET_S:
        return None, f"Error: 'time_budget_s' must be a number from 1 to {MAX_TIME_BUDGET_S:.0f}"
    return {
        "project": project.strip() if project else None,
        "editable_limit": limit,
        "max_files": max_files,
        "max_depth": max_depth,
        "stale_days": stale_days,
        "include_read_only": include_read_only,
        "time_budget_s": budget,
    }, None


# -- Safe reads -------------------------------------------------------------


def is_design(data_file):
    return str(present(data_file, "fileExtension", "")).lower() in DESIGN_EXTENSIONS


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
    exact = [p for p in projects if (present(p, "name", "") or "").lower() == project_filter.lower()]
    if exact:
        return exact
    return [p for p in projects if project_matches(present(p, "name", ""), project_filter)]


# -- Ids --------------------------------------------------------------------


def lineage_key(raw_id):
    """Reduce a DataFile id to its lineage token so different urn forms compare equal.

    ``urn:adsk.wipprod:dm.lineage:TOKEN`` (DataFile.id) and
    ``urn:adsk.wipprod:fs.file:vf.TOKEN?version=17`` (a version urn) both give
    ``TOKEN``. Anything else is returned without its ``?query``. None for no id.
    """
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    text = raw_id.strip().split("?", 1)[0]
    for marker in ("dm.lineage:", "vf."):
        index = text.rfind(marker)
        if index != -1 and text[index + len(marker):]:
            return text[index + len(marker):]
    return text


def _name_key(project_name, file_name):
    if not file_name:
        return None
    return ((project_name or "").lower(), file_name.lower())


# -- Walk -------------------------------------------------------------------


def file_entry(data_file, path_parts, details=True):
    """Plain-dict description of one DataFile (keeps the object for hints).

    With *details* False only name, id and the read-only flag are read; every
    property is a cloud read, so the walk defers version and dates until
    ``ensure_details`` (only editable files need them).
    """
    name = present(data_file, "name", "")
    entry = {
        "path": "/".join([*path_parts, name]),
        "name": name,
        "id": present(data_file, "id"),
        "is_read_only": bool(present(data_file, "isReadOnly", True)),
        "_project": path_parts[0] if path_parts else "",
        "_file": data_file,
    }
    if details:
        ensure_details(entry)
    return entry


def ensure_details(entry):
    """Read version and dates for *entry* once."""
    if "version" not in entry:
        data_file = entry.get("_file")
        entry["version"] = present(data_file, "versionNumber")
        entry["date_created"] = present(data_file, "dateCreated")
        entry["date_modified"] = present(data_file, "dateModified")
    return entry


def walk_projects(projects, max_depth, max_files, deadline=None, clock=time.monotonic):
    """Depth-first walk of each project's folders collecting Fusion designs.

    *max_depth* counts folder levels below the project root (0 = root only).
    Stops after *max_files* design files, or once *clock()* passes *deadline*
    (``truncated_reason`` "max_files" / "time_budget"). Returns (entries, info).
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

    def out_of_time():
        if deadline is not None and clock() >= deadline:
            info["truncated"] = True
            info["truncated_reason"] = "time_budget"
            return True
        return False

    def visit(folder, parts, depth):
        if info["truncated"] or out_of_time():
            return
        info["folders_visited"] += 1
        files = present(folder, "dataFiles")
        if files is None:
            info["errors"].append(f"{'/'.join(parts)}: could not read files")
        for data_file in items(files):
            if out_of_time():
                return
            if not is_design(data_file):
                info["non_design_files"] += 1
                continue
            if len(entries) >= max_files:
                info["truncated"] = True
                info["truncated_reason"] = "max_files"
                return
            entries.append(file_entry(data_file, parts, details=False))
        for sub in items(present(folder, "dataFolders")):
            if info["truncated"]:
                return
            if depth >= max_depth:
                info["folders_skipped_depth"] += 1
                continue
            visit(sub, [*parts, present(sub, "name", "?")], depth + 1)

    for project in projects:
        if info["truncated"]:
            break
        name = present(project, "name", "?")
        info["projects_walked"].append(name)
        root = present(project, "rootFolder")
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
    return [present(f, "name", "?") for f in items(present(data_file, attribute))]


def release_hints(entry, open_files, now, stale_days):
    """Heuristic hints on whether *entry* (an editable file) can be made read-only.

    *open_files* maps lineage key -> {"modified": bool, "active": bool}.
    Link names are read here, so only for editable files, never in the walk.
    """
    hints = []
    blockers = []
    ensure_details(entry)
    doc = open_files.get(lineage_key(entry.get("id")))
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
    parents = reference_names(data_file, "parentReferences") if present(data_file, "hasParentReferences", False) else []
    children = reference_names(data_file, "childReferences") if present(data_file, "hasChildReferences", False) else []
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
    files = list(items(present(limits, "editableFiles", [])))
    return {
        "max_editable": present(limits, "maxEditableFileCount"),
        "editable_count": present(limits, "editableFileCount"),
        "files": files,
    }


def match_personal_use_files(entries, api_files):
    """Mark walk entries listed in personalUseLimits.editableFiles.

    Ids are compared by lineage key (``id`` and ``versionId`` of each listed
    file). A listed file whose ids match nothing falls back to project + name,
    but only when exactly one walked design has that name in that project
    (duplicate names, e.g. archive copies, are never guessed). Sets
    ``_in_list`` / ``_matched_by`` on matched entries and returns
    (unmatched api files, raw id sample).
    """
    by_key = {}
    by_name = {}
    for entry in entries:
        key = lineage_key(entry.get("id"))
        if key:
            by_key.setdefault(key, entry)
        name_key = _name_key(entry.get("_project"), entry.get("name"))
        if name_key:
            by_name.setdefault(name_key, []).append(entry)

    unmatched = []
    sample = []
    for data_file in api_files:
        raw_ids = [present(data_file, "id"), present(data_file, "versionId")]
        if len(sample) < IDS_SAMPLE_SIZE:
            sample.append(raw_ids[0] if raw_ids[0] is not None else raw_ids[1])
        entry = next((by_key[k] for k in map(lineage_key, raw_ids) if k in by_key), None)
        matched_by = "id"
        if entry is None:
            project = present(present(data_file, "parentProject"), "name", "")
            candidates = by_name.get(_name_key(project, present(data_file, "name", "")), [])
            entry = candidates[0] if len(candidates) == 1 else None
            matched_by = "project_and_name"
        if entry is None:
            unmatched.append(data_file)
        else:
            entry["_in_list"] = True
            entry["_matched_by"] = matched_by
    return unmatched, sample


def summarize(entries, walk_info, personal_use, open_files, options, now):
    """Build the report dict from walk entries and the licence's own counts."""
    listed = []
    disagreement = False
    sample = []
    if personal_use:
        unmatched, sample = match_personal_use_files(entries, personal_use["files"])
        for data_file in unmatched:
            # Editable but outside the walk (other hub, filter, or cap):
            # still report it; Fusion gives no folder path from here.
            project = present(present(data_file, "parentProject"), "name", "?")
            entry = file_entry(data_file, [project, "..."])
            entry["is_read_only"] = False
            entry["outside_walk"] = True
            entry["_in_list"] = True
            listed.append(entry)
        disagreement = (
            any(e.get("_matched_by") != "id" for e in entries if e.get("_in_list"))
            or any(not e["is_read_only"] and not e.get("_in_list") for e in entries)
            or (bool(unmatched) and not walk_info.get("truncated") and options["project"] is None)
        )

    editable = [e for e in entries if not e["is_read_only"] or e.get("_in_list")] + listed
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
        ensure_details(entry)
        row = {k: v for k, v in entry.items() if not k.startswith("_") and k not in ("date_created", "date_modified")}
        row["in_personal_use_list"] = bool(entry.get("_in_list")) if personal_use else None
        if entry.get("_matched_by") == "project_and_name":
            row["matched_by"] = "project_and_name"
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
    if disagreement:
        # The licence list and the walk disagree; show the raw id form so the
        # mismatch can be diagnosed from one live run.
        report["personal_use_list_ids_sample"] = sample
        report["walk_ids_sample"] = [e.get("id") for e in entries if not e["is_read_only"]][:IDS_SAMPLE_SIZE]
    reason = walk_info.get("truncated_reason")
    if reason == "max_files":
        report["walk"]["truncated_note"] = f"stopped after max_files={options['max_files']} design files"
    elif reason == "time_budget":
        report["walk"]["truncated_note"] = (
            f"stopped after the {options['time_budget_s']:g} s time budget (Fusion's main thread); "
            "narrow with 'project' or raise time_budget_s"
        )
    if not personal_use:
        report["note"] = (
            "Data.personalUseLimits is null: this is not a Personal Use licence (or Fusion did not report it). "
            "Every design writable to you shows as editable."
        )
    if options["include_read_only"]:
        report["read_only_files"] = [
            {"path": e["path"], "version": ensure_details(e)["version"]}
            for e in entries if e["is_read_only"] and not e.get("_in_list")
        ]
    return report


def open_file_states(documents):
    """Map lineage key -> state for open documents that are saved to the cloud."""
    states = {}
    for doc in items(documents):
        data_file = present(doc, "dataFile")
        file_id = lineage_key(present(data_file, "id"))
        if file_id:
            states[file_id] = {"modified": bool(present(doc, "isModified", False)),
                               "active": bool(present(doc, "isActive", False))}
    return states


def editable_files(arguments):
    """Report editable designs against the Personal Use limit, with release hints."""
    options, err = validate_arguments(arguments)
    if err:
        return error_result(err)
    try:
        app = get_app()
        data = app.data
        started = time.monotonic()
        all_projects = list(items(present(data, "dataProjects")))
        projects = select_projects(all_projects, options["project"])
        if options["project"] and not projects:
            names = sorted(present(p, "name", "?") for p in all_projects)
            return error_result(f"Error: no project matches {options['project']!r}. Projects: {names}")

        entries, walk_info = walk_projects(projects, options["max_depth"], options["max_files"],
                                           deadline=started + options["time_budget_s"])
        personal_use = personal_use_summary(present(data, "personalUseLimits"))
        report = summarize(entries, walk_info, personal_use, open_file_states(app.documents), options, time.time())
        report["hub"] = present(present(data, "activeHub"), "name")
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        log(f"[MCP] editable_files: {report['editable_count']}/{report['editable_limit']} editable, "
            f"{len(entries)} designs scanned in {report['elapsed_seconds']}s")
        return ok_result(report)
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] editable_files failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")
