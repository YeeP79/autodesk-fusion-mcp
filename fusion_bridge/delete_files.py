"""Permanently delete Fusion cloud files, parents first.

``dry_run`` defaults to true; a real run also needs ``confirm``. Planning, ordering
and log parsing are Fusion-free (duck-typed over ``DataProject`` / ``DataFolder`` /
``DataFile``) so they can be tested with fakes.

What this module encodes:

- ``DataFile.deleteMe()`` works on read-only files but fails while another file
  links the target, so a parent goes before the files it links. Links are read
  again just before each delete, and a file is only attempted once no undeleted
  member of the set still links it.
- ``deleteMe()`` RAISES (``RuntimeError: 2 : InternalValidationError : res``) rather
  than returning False, and the exception says nothing useful. The server's reason
  is only in Fusion's app log, in the ``POST 400 .../lineages/delete`` entry. The
  log offset is taken before each delete, so an older failure of the same file is
  never reported as this one.
- ``hasParentReferences`` can be False while the server still refuses with
  ``GEN_RELATED_OBJECTS``: the relationship is one the API does not expose.
- The whole call is refused, deleting nothing, for a file open in Fusion, a link
  from outside the set, or a path that is missing or ambiguous. If links cannot be
  read, they are treated as unknown and the call is refused too.
- Nothing is ever opened or closed (closing a document also closes what it links).
- ``DataProject`` has no delete, so projects cannot be removed through the API.
"""

import json
import re
import time
import traceback
from functools import partial

from . import project_tree
from .common.app_log import (
    LOG_POLLS, ServerLog, default_log_roots, failure_reason, newest_log, parse_requests, poll_reason
)
from .common.args import bool_arg, reject_unknown, require_object
from .common.cloud import (
    AmbiguousFolder, PathResolver, Refusal, find_project, items, open_file_ids, problem,
    project_arg, resolve_file, resolve_folder, split_path
)
from .common.results import error_result, tool_result
from .dispatch import get_app, log

# Plans list every file; a one-space indent keeps them readable and short.
_result = partial(tool_result, indent=1)

MAX_FILES_PER_CALL = 200

_KNOWN_ARGS = {"project", "files", "folder", "include_empty_folder", "dry_run", "confirm"}
_DELETE_ENTRY = re.compile(r"^POST (\d{3}) (\S*/lineages/delete)\b(.*)$")

RELATED_OBJECTS = "GEN_RELATED_OBJECTS"
RELATED_OBJECTS_HINT = (
    "The server reports a relationship the API may not expose (hasParentReferences can be false here). "
    "Try deleting this file from the Data Panel, which may name what holds it. "
    "If it was related to a file deleted in this call, running the delete again may also work."
)
TRASH_NOTE = (
    "Autodesk's API documentation for DataFile.deleteMe does not say whether deleted files can be "
    "recovered from a trash; treat this delete as permanent."
)
NOTES = [
    "Deleting a file removes the whole file with all its versions.",
    "'folder' covers the files directly in that folder, not its subfolders.",
    "Projects cannot be deleted: DataProject has no delete in the Fusion API.",
]


# -- Collections and lookup (duck-typed) -------------------------------------


def _safe_path(data_file, resolver):
    """A linked file can sit anywhere, even somewhere unreadable; its id still identifies it."""
    try:
        return resolver.file_path(data_file)
    except Exception:
        return None


def _describe(data_file, resolver):
    return {"path": _safe_path(data_file, resolver), "id": data_file.id}


def _candidate(data_file, resolver):
    return dict(_describe(data_file, resolver), version=data_file.versionNumber)


def parents_of(data_file):
    """The files linking *data_file*, one per id."""
    unique = {}
    for ref in items(data_file.parentReferences):
        unique.setdefault(ref.id, ref)
    return list(unique.values())


# -- Ordering ----------------------------------------------------------------


def order_rounds(ids, parents):
    """Group *ids* into delete rounds, parents before the files they link.

    *parents* maps id -> linking ids; ids outside *ids* are ignored. Returns
    ``(rounds, stuck)``: *stuck* holds ids that can never become free (a link cycle,
    or a file below one).
    """
    members = set(ids)
    remaining = list(ids)
    done = set()
    rounds = []
    while remaining:
        ready = [i for i in remaining
                 if not (set(parents.get(i, ())) & members) - done - {i}]
        if not ready:
            break
        rounds.append(ready)
        done.update(ready)
        remaining = [i for i in remaining if i not in done]
    return rounds, remaining


# -- App log -----------------------------------------------------------------


def parse_delete_entries(text):
    """The ``lineages/delete`` entries in app log *text*, oldest first; see ``parse_requests``."""
    return parse_requests(text, _DELETE_ENTRY)


def _names_lineage(request_body, urn):
    if not request_body:
        return False
    try:
        lineages = json.loads(request_body).get("lineages")
        return any(isinstance(item, dict) and item.get("urn") == urn for item in lineages)
    except (ValueError, AttributeError, TypeError):
        return f'"{urn}"' in request_body  # a body the log cut short is not valid JSON


def server_reason(text, urn):
    """The server's reason for the last failed delete of *urn* in *text*, or None.

    Only an entry naming *urn* counts: a reason is never borrowed from another file.
    """
    failed = [e for e in parse_delete_entries(text) if e["status"] >= 400 and _names_lineage(e["request_body"], urn)]
    return failure_reason(failed[-1]) if failed else None


def find_reason(reader, offset, urn, *, polls=LOG_POLLS, sleep=time.sleep):
    """Poll the log after a failed delete; return the server's reason or None."""
    return poll_reason(lambda: server_reason(reader.since(offset), urn), polls, sleep)


# -- Planning ----------------------------------------------------------------


def plan_delete(project, specs, folder, *, find_by_id, open_ids, resolver, out_of_time=lambda: False):
    """Build the delete plan. Reads only.

    Returns ``(plan, problems, work)``; *work* maps id -> DataFile in delete order
    and may only be used when *problems* is empty.
    """
    problems = []
    targets = {}
    from_files, from_folder = set(), set()
    for spec in specs:
        try:
            data_file = resolve_file(project, spec, find_by_id, resolver, _candidate)
        except Refusal as refusal:
            problems.append(refusal.problem)
            continue
        targets.setdefault(data_file.id, data_file)
        from_files.add(data_file.id)

    subfolders = []
    if folder is not None:
        for data_file in items(folder.dataFiles):
            targets.setdefault(data_file.id, data_file)
            from_folder.add(data_file.id)
        subfolders = [f"{resolver.folder_path(folder)}/{s.name}" for s in items(folder.dataFolders)]

    parents = {}
    outside = []
    for fid, data_file in targets.items():
        if out_of_time():
            problems.append(problem(
                "time_budget", "Ran out of time reading links, so the plan is incomplete. "
                "Name fewer files per call."))
            break
        try:
            refs = parents_of(data_file)
        except Exception as exc:
            problems.append(problem(
                "links_unreadable", f"Could not read what links this file: {type(exc).__name__}: {exc}",
                **_describe(data_file, resolver)))
            continue
        parents[fid] = [r.id for r in refs]
        linkers = [_describe(r, resolver) for r in refs if r.id not in targets]
        if linkers:
            outside.append(dict(_describe(data_file, resolver), linked_from=linkers))
    for row in outside:
        problems.append(problem(
            "linked_from_outside", "Files outside this set link this file; add them to the set or remove the links.",
            **row))

    for fid, data_file in targets.items():
        if fid in open_ids:
            problems.append(problem(
                "open_in_fusion", "This file is open in Fusion (hidden documents count); close it first.",
                **_describe(data_file, resolver), **open_ids[fid]))

    rounds, stuck = order_rounds(list(targets), parents)
    order = []
    for number, ids in enumerate(rounds, 1):
        for fid in ids:
            data_file = targets[fid]
            row = dict(_describe(data_file, resolver), round=number, version=data_file.versionNumber)
            linked_by = [p for p in parents.get(fid, ()) if p in targets and p != fid]
            if linked_by:
                row["linked_from_in_set"] = [_describe(targets[p], resolver) for p in linked_by]
            order.append(row)
    plan = {
        "summary": {
            "files": len(targets),
            "named_in_files": len(from_files),
            "in_folder": len(from_folder),
            "rounds": len(rounds),
            "files_per_round": [len(r) for r in rounds],
            "files_linked_from_outside": len(outside),
        },
        "order": order,
        "outside_links": outside,
    }
    if stuck:
        plan["unorderable"] = [dict(_describe(targets[i], resolver), reason="link cycle") for i in stuck]
    if folder is not None:
        plan["folder"] = {"path": resolver.folder_path(folder), "id": folder.id,
                          "files": len(from_folder), "subfolders_not_included": subfolders}
    work = {i: targets[i] for ids in rounds for i in ids}
    work.update((i, targets[i]) for i in stuck)
    return plan, problems, work


# -- Execution ---------------------------------------------------------------


def _failure(row, exc_text, reason):
    entry = dict(row, error=exc_text, server=reason)
    if reason is None:
        entry["reason"] = exc_text
        entry["note"] = "No matching lineages/delete failure found in Fusion's app log."
        return entry
    entry["reason"] = reason["message"] or f"HTTP {reason['status']}"
    if reason["error_code"] == RELATED_OBJECTS:
        entry["hint"] = RELATED_OBJECTS_HINT
    return entry


def execute_delete(work, *, lookup, open_ids, resolver, reader, out_of_time=lambda: False, sleep=time.sleep):
    """Delete the files in *work* (id -> DataFile, parents first) in repeated rounds.

    *open_ids* is called for a fresh reading before each delete. One file's failure
    never stops the rest; a file still linked by a member that was not deleted is
    left alone. Returns ``{deleted, failed, not_attempted}``.
    """
    members = set(work)
    deleted_ids = set()
    deleted, failed, not_attempted = [], [], []
    settled = set()
    rows = {fid: _describe(f, resolver) for fid, f in work.items()}
    blockers = {}

    def skip(fid, reason, **extra):
        settled.add(fid)
        not_attempted.append(dict(rows[fid], reason=reason, **extra))

    progress = True
    while progress:
        progress = False
        for fid in work:
            if fid in settled:
                continue
            if out_of_time():
                skip(fid, "time_budget: stopped before the dispatcher timeout; run the call again for the rest")
                continue
            try:
                fresh = lookup(fid)
                if fresh is None:
                    skip(fid, "no longer found; it may already be deleted")
                    continue
                refs = parents_of(fresh)
                opened = open_ids()
            except Exception as exc:
                skip(fid, f"re-check failed, so it was left alone: {type(exc).__name__}: {exc}")
                continue
            if fid in opened:
                skip(fid, "opened in Fusion after planning", **opened[fid])
                continue
            # A deleted parent can linger in a cached reference list.
            live = [r for r in refs if r.id not in deleted_ids and r.id != fid]
            outsiders = [r for r in live if r.id not in members]
            if outsiders:
                skip(fid, "linked from outside the set since planning",
                     linked_from=[_describe(r, resolver) for r in outsiders])
                continue
            if live:
                blockers[fid] = [r.id for r in live]
                continue

            offset = reader.mark()
            try:
                ok = fresh.deleteMe()
            except Exception as exc:
                reason = find_reason(reader, offset, fid, sleep=sleep)
                failed.append(_failure(rows[fid], f"{type(exc).__name__}: {exc}", reason))
                settled.add(fid)
                progress = True
                continue
            settled.add(fid)
            progress = True
            if ok:
                deleted_ids.add(fid)
                deleted.append(rows[fid])
            else:
                failed.append(_failure(rows[fid], "DataFile.deleteMe returned false",
                                       find_reason(reader, offset, fid, sleep=sleep)))

    for fid in work:
        if fid not in settled:
            holders = [rows[b] for b in blockers.get(fid, ()) if b in rows]
            skip(fid, "still linked from files in the set that were not deleted", linked_from=holders)
    return {"deleted": deleted, "failed": failed, "not_attempted": not_attempted}


def remove_folder(folder, path, deleted_ids):
    """Delete *folder* only if nothing is left in it; never raises."""
    result = {"path": path, "id": folder.id, "removed": False}
    try:
        # A listing can still show files deleted a moment ago.
        files = [f for f in items(folder.dataFiles) if f.id not in deleted_ids]
        subfolders = items(folder.dataFolders)
        if files or subfolders:
            result["reason"] = f"not empty: {len(files)} files, {len(subfolders)} subfolders remain"
            return result
        if folder.deleteMe():
            result["removed"] = True
        else:
            result["reason"] = "DataFolder.deleteMe returned false"
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result


# -- Tool handler ------------------------------------------------------------


def validate_arguments(arguments):
    """Return the options, or raise ValueError. Touches nothing in Fusion."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN_ARGS)
    project = project_arg(arguments)

    flags = {name: bool_arg(arguments, name, default)
             for name, default in (("dry_run", True), ("confirm", False), ("include_empty_folder", False))}

    files = arguments.get("files", [])
    if not isinstance(files, list):
        raise ValueError("'files' must be a list of file paths or ids")
    if not all(isinstance(f, str) and f.strip() for f in files):
        raise ValueError("every entry in 'files' must be a non-empty string")
    if len(files) > MAX_FILES_PER_CALL:
        raise ValueError(f"'files' may list at most {MAX_FILES_PER_CALL} files per call")

    folder = arguments.get("folder")
    segments = None
    if folder is not None:
        if not isinstance(folder, str):
            raise ValueError("'folder' must be a folder path")
        segments = split_path(folder, "path")
        if not segments:
            raise ValueError("'folder' must name a folder inside the project; the project root is not accepted "
                             "(projects cannot be deleted: DataProject has no delete in the Fusion API)")
    if not files and segments is None:
        raise ValueError("pass 'files', 'folder', or both")
    if flags["include_empty_folder"] and segments is None:
        raise ValueError("'include_empty_folder' needs 'folder'")
    if not flags["dry_run"] and not flags["confirm"]:
        raise ValueError("'confirm' must be true when 'dry_run' is false: this permanently deletes files")
    return dict(flags, project=project, files=[f.strip() for f in files], segments=segments)


def _run(app, options, out_of_time):
    data = app.data
    try:
        project = find_project(items(data.dataProjects), options["project"])
    except LookupError as exc:
        return error_result(f"Error: {exc}")

    resolver = PathResolver()
    base = {"tool": "delete_files", "dry_run": options["dry_run"], "project": project.name}
    folder = None
    if options["segments"] is not None:
        try:
            folder, _ = resolve_folder(project.rootFolder, options["segments"])
        except AmbiguousFolder as exc:
            refused = problem("ambiguous", str(exc), folder="/".join(options["segments"]))
            return _result(dict(base, refused=True, changed=False, problems=[refused]), True)
        except LookupError as exc:
            refused = problem("not_found", str(exc), folder="/".join(options["segments"]))
            return _result(dict(base, refused=True, changed=False, problems=[refused]), True)

    opened, unreadable = open_file_ids(app.documents)
    plan, problems, work = plan_delete(project, options["files"], folder, find_by_id=data.findFileById,
                                       open_ids=opened, resolver=resolver, out_of_time=out_of_time)
    if unreadable:
        plan["open_documents_unreadable"] = unreadable
    base.update(notes=NOTES, trash=TRASH_NOTE)
    if problems:
        return _result(dict(base, refused=True, changed=False, problems=problems, plan=plan), True)
    if options["dry_run"]:
        return _result(dict(base, changed=False, plan=plan,
                            next="Re-run with dry_run=false and confirm=true to delete these files."))

    def read_open():
        return open_file_ids(app.documents)[0]

    outcome = execute_delete(work, lookup=data.findFileById, open_ids=read_open, resolver=resolver,
                             reader=ServerLog(newest_log(default_log_roots())), out_of_time=out_of_time)
    folder_result = None
    if options["include_empty_folder"]:
        deleted_ids = {row["id"] for row in outcome["deleted"]}
        folder_result = remove_folder(folder, resolver.folder_path(folder), deleted_ids)
    incomplete = bool(outcome["failed"] or outcome["not_attempted"]
                      or (folder_result is not None and not folder_result["removed"]))
    changed = bool(outcome["deleted"]) or bool(folder_result and folder_result["removed"])
    return _result(dict(base, changed=changed, complete=not incomplete, **outcome, folder=folder_result), incomplete)


def delete_files(arguments):
    """Permanently delete cloud files, parents first; a dry run unless told otherwise."""
    try:
        options = validate_arguments(arguments)
    except ValueError as exc:
        return error_result(f"Error: {exc}")
    started = time.monotonic()
    deadline = started + project_tree.time_budget()
    try:
        result = _run(get_app(), options, lambda: time.monotonic() >= deadline)
        log(f"[MCP] delete_files in {options['project']!r} (dry_run={options['dry_run']}) "
            f"took {time.monotonic() - started:.2f}s")
        return result
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] delete_files failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")
