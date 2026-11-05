"""Cloud file housekeeping: move files, rename folders and files, remove empty folders.

Every tool plans first and changes nothing unless ``dry_run`` is false. Planning,
validation and the link check are Fusion-free (duck-typed over ``DataProject`` /
``DataFolder`` / ``DataFile`` like ``common.cloud``) so they can be tested with fakes.

Rules this module enforces:

- Files are addressed by path ("folder/sub/name") or by id ("urn:..."). A path
  that matches several files (same name, or names differing only by case) is
  refused with every candidate's path, version and id; pass the id then.
- Folder names are case-insensitive: renaming to a sibling's name in another
  case fails in Fusion (CB_NAE), so it is refused up front. A case-only rename
  of the same folder goes through a temporary name in two explicit steps.
- The server locks a folder briefly after a rename, so renaming it again at once
  is refused (423 LOCK_RESOURCE_IS_LOCKED, seen by Fusion as an opaque
  ``InternalValidationError``). The rename waits for the lock with events pumped,
  on a fresh folder object, and gives up after a bounded number of tries.
- Cloud writes raise that same opaque error for every server refusal, so each
  failure carries the server's reason from the app log (``common.app_log``).
- A file open in Fusion is never moved or renamed, and nothing is ever closed
  (closing a document also closes the documents it links). Such files are
  refused and listed.
- Links are by id, so moves and renames should keep them. The link check
  records, before and after the change, the resolved path of every changed
  file and every file linking to or from it, and reports paths that changed
  and links that were lost or became unresolvable.
"""

import time
import traceback
from functools import partial

import adsk

from . import project_tree
from .common import cloud
from .common.app_log import INTERNAL_ERROR, LOCKED_CODE, LOCKED_STATUS, ServerLog, current_log
from .common.args import bool_arg, reject_unknown, require_object, text_arg
from .common.cloud import Refusal, describe_file, problem, project_arg, resolve_file
from .common.results import error_result, tool_result
from .dispatch import get_app, log

# Plans list every file; a one-space indent keeps them readable and short.
_result = partial(tool_result, indent=1)

MAX_FILES_PER_CALL = 50
LINK_CHECK_MAX_FILES = 100
_TEMP_SUFFIX = "__case_rename_tmp"
# Live, a rename sent right after a rename was locked; one sent ~0.4 s after that worked.
_STEP_SETTLE_S = 1.0
_LOCK_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 3.0)
_PUMP_INTERVAL_S = 0.05

NOTES = [
    "Files are matched by path (case-insensitive) or by id ('urn:...'); ambiguous paths are refused.",
    "dry_run defaults to true: nothing changes until you pass dry_run=false.",
]


# -- Small helpers -----------------------------------------------------------


def _wait(seconds):
    """Wait on the main thread with Fusion's events pumped, so its data updates keep landing."""
    deadline = time.monotonic() + seconds
    while True:
        adsk.doEvents()
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, _PUMP_INTERVAL_S))


def _failure(exc, resource_id, server_log, offset):
    """Describe a failed cloud write, with the server's reason for Fusion's opaque error."""
    text = f"{type(exc).__name__}: {exc}"
    failure = {"error": text}
    if INTERNAL_ERROR not in text:
        return failure
    reason = server_log.reason(offset, resource_id)
    failure["server"] = reason
    if reason is None:
        failure["note"] = "No matching failed request found in Fusion's app log."
    else:
        failure["reason"] = reason["message"] or f"HTTP {reason['status']}"
    return failure


def _is_lock(failure):
    """True for the server's rename lock. With no log entry to go on, assume it may be."""
    server = failure.get("server")
    if server is None:
        return INTERNAL_ERROR in failure["error"]
    return server["status"] == LOCKED_STATUS or server["error_code"] == LOCKED_CODE


def _file_key(name, extension):
    """Fusion treats names case-insensitively; a design and a drawing may share a name."""
    return (name.casefold(), (extension or "").casefold())


def validate_new_name(value, what):
    """Return the stripped name, or raise ValueError."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{what}' must be a non-empty string")
    name = value.strip()
    if "/" in name or "\\" in name:
        raise ValueError(f"'{what}' is a name, not a path: it may not contain '/' or '\\'")
    if name in (".", ".."):
        raise ValueError(f"'{what}' may not be {name!r}")
    return name


def open_file_ids(documents):
    """Map dataFile id -> {document, visible} for every document open in Fusion."""
    return cloud.open_file_ids(documents)[0]


# -- Resolution --------------------------------------------------------------


def resolve_folder_prefix(root, segments):
    """Walk as far down *segments* as folders exist.

    Returns ``(deepest_folder, names_as_in_fusion, missing_segments)``. Raises
    LookupError when a segment matches several sibling folders.
    """
    folder, actual = root, []
    for i, seg in enumerate(segments):
        _, folded = cloud.match_name(cloud.items(folder.dataFolders), seg)
        if not folded:
            return folder, actual, list(segments[i:])
        folder = cloud.find_child_folder(folder, seg)
        actual.append(folder.name)
    return folder, actual, []


def collect_files(folder, cap):
    """Files in *folder* and its subfolders, depth-first, up to *cap*. Returns (files, capped)."""
    found = []
    stack = [folder]
    while stack:
        current = stack.pop()
        for data_file in cloud.items(current.dataFiles):
            if len(found) >= cap:
                return found, True
            found.append(data_file)
        stack.extend(reversed(cloud.items(current.dataFolders)))
    return found, False


# -- Link check --------------------------------------------------------------


def _link_entry(data_file, resolver):
    """Snapshot one file: name, resolved path and linked ids. Returns (entry, linked DataFiles)."""
    entry = {"name": data_file.name}
    linked = []
    try:
        entry["path"] = resolver.file_path(data_file)
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    try:
        uses = cloud.items(data_file.childReferences) if data_file.hasChildReferences else []
        used_by = cloud.items(data_file.parentReferences) if data_file.hasParentReferences else []
        entry["uses"] = sorted(r.id for r in uses)
        entry["used_by"] = sorted(r.id for r in used_by)
        linked = uses + used_by
    except Exception as exc:
        entry["links_error"] = f"{type(exc).__name__}: {exc}"
    return entry, linked


def link_snapshot(primary_files, resolver, cap=LINK_CHECK_MAX_FILES):
    """Snapshot *primary_files* and every file they link to or from (by id).

    Returns ``(snapshot, capped)`` where snapshot maps id -> entry.
    """
    snapshot = {}
    pending = []
    for data_file in primary_files:
        if data_file.id in snapshot:
            continue
        snapshot[data_file.id], linked = _link_entry(data_file, resolver)
        pending.extend(linked)
    capped = False
    for ref in pending:
        if ref.id in snapshot:
            continue
        if len(snapshot) >= cap:
            capped = True
            break
        snapshot[ref.id], _ = _link_entry(ref, resolver)
    return snapshot, capped


def link_snapshot_after(ids, lookup, resolver):
    """Re-fetch each id with *lookup* and snapshot it; unknown ids are marked unresolvable."""
    snapshot = {}
    for fid in ids:
        try:
            data_file = lookup(fid)
        except Exception as exc:
            snapshot[fid] = {"unresolvable": True, "error": f"{type(exc).__name__}: {exc}"}
            continue
        if data_file is None:
            snapshot[fid] = {"unresolvable": True}
            continue
        snapshot[fid], _ = _link_entry(data_file, resolver)
    return snapshot


def links_by_path(snapshot, ids):
    """Readable link listing for *ids*: each file's uses/used_by as paths (or ids when outside the snapshot)."""
    def path_of(fid):
        return snapshot.get(fid, {}).get("path", fid)

    listing = []
    for fid in ids:
        entry = snapshot.get(fid, {})
        row = {"id": fid, "path": entry.get("path")}
        for key in ("uses", "used_by"):
            if entry.get(key):
                row[key] = [path_of(r) for r in entry[key]]
        if "links_error" in entry:
            row["links_error"] = entry["links_error"]
        listing.append(row)
    return listing


def diff_links(before, after, expected_ids):
    """Compare snapshots taken before and after a change.

    *expected_ids* are files whose path is meant to change. Returns a report
    with ``ok`` false when a file became unresolvable, a link was lost, or a
    path changed that should not have.
    """
    path_changes, unresolvable, lost, gained = [], [], [], []
    for fid, b in before.items():
        a = after.get(fid, {"unresolvable": True})
        if a.get("unresolvable") or "path" not in a:
            unresolvable.append({"id": fid, "name": b.get("name"), "path_before": b.get("path"),
                                 **({"error": a["error"]} if "error" in a else {})})
            continue
        if b.get("path") != a["path"]:
            path_changes.append({"id": fid, "before": b.get("path"), "after": a["path"],
                                 "expected": fid in expected_ids})
        if "links_error" in a and "links_error" not in b:
            lost.append({"id": fid, "path": a["path"], "error": a["links_error"]})
            continue
        for key in ("uses", "used_by"):
            was, now = set(b.get(key, ())), set(a.get(key, ()))
            for ref in sorted(was - now):
                lost.append({"id": fid, "path": a["path"], "link": key, "ref_id": ref,
                             "ref_path": before.get(ref, {}).get("path")})
            for ref in sorted(now - was):
                gained.append({"id": fid, "path": a["path"], "link": key, "ref_id": ref})
    unexpected = [c for c in path_changes if not c["expected"]]
    report = {
        "ok": not unresolvable and not lost and not unexpected,
        "files_checked": len(before),
        "path_changes": path_changes,
        "unresolvable": unresolvable,
        "links_lost": lost,
    }
    if gained:
        report["links_gained"] = gained
    return report


# -- move_files planning -----------------------------------------------------


def plan_move(project, specs, target_project, to_segments, *, create_missing, find_by_id, open_ids, resolver):
    """Plan moving *specs* into *to_segments* of *target_project*.

    Returns ``(plan, problems, work)``; *work* is ``(files, target_folder,
    missing_segments)`` for ``execute_move`` and is only usable when
    *problems* is empty.
    """
    problems = []
    files = []
    seen = {}
    for spec in specs:
        try:
            data_file = resolve_file(project, spec, find_by_id, resolver)
        except Refusal as refusal:
            problems.append(refusal.problem)
            continue
        if data_file.id in seen:
            problems.append(problem("listed_twice", f"{spec!r} and {seen[data_file.id]!r} are the same file",
                                     file=spec, id=data_file.id))
            continue
        seen[data_file.id] = spec
        files.append(data_file)

    target, actual, missing = None, [], []
    try:
        target, actual, missing = resolve_folder_prefix(target_project.rootFolder, to_segments)
    except LookupError as exc:
        problems.append(problem("target_ambiguous", str(exc)))
    target_path = cloud.join_path(target_project.name, *actual, *missing)
    if missing and not create_missing:
        problems.append(problem(
            "target_missing",
            f"Folder {'/'.join(missing)!r} does not exist under "
            f"{cloud.join_path(target_project.name, *actual)!r}; pass create_missing=true to create it"))

    existing = cloud.items(target.dataFiles) if target is not None and not missing else []
    existing_by_key = {}
    for data_file in existing:
        existing_by_key.setdefault(_file_key(data_file.name, data_file.fileExtension), []).append(data_file)

    entries = []
    incoming = {}
    for data_file in files:
        source = resolver.file_path(data_file)
        entries.append({"id": data_file.id, "name": data_file.name, "version": data_file.versionNumber,
                        "from": source, "to": cloud.join_path(target_path, data_file.name)})
        if data_file.id in open_ids:
            problems.append(problem("open_in_fusion",
                                     f"{source!r} is open in Fusion; close it yourself and retry",
                                     id=data_file.id, **open_ids[data_file.id]))
        if target is not None and not missing and data_file.parentFolder.id == target.id:
            problems.append(problem("already_there", f"{source!r} is already in {target_path!r}", id=data_file.id))
            continue
        key = _file_key(data_file.name, data_file.fileExtension)
        clashes = [f for f in existing_by_key.get(key, []) if f.id != data_file.id]
        if clashes:
            problems.append(problem(
                "name_collision",
                f"{target_path!r} already has a file named {data_file.name!r} (names compare ignoring case)",
                id=data_file.id, existing=[describe_file(f, resolver) for f in clashes]))
        incoming.setdefault(key, []).append(data_file)
    for group in incoming.values():
        if len(group) > 1:
            problems.append(problem(
                "name_collision",
                f"{len(group)} files named {group[0].name!r} would land in {target_path!r}",
                files=[describe_file(f, resolver) for f in group]))

    plan = {
        "target_folder": target_path,
        "folders_to_create": [cloud.join_path(target_project.name, *actual, *missing[:i + 1])
                              for i in range(len(missing))],
        "moves": entries,
    }
    return plan, problems, (files, target, missing)


def execute_move(files, target, missing, server_log=None):
    """Create missing folders, then move files one by one, stopping at the first failure."""
    server_log = server_log or ServerLog()
    created, moved = [], []
    for seg in missing:
        offset = server_log.mark()
        try:
            target = target.dataFolders.add(seg)
            created.append(seg)
        except Exception as exc:
            return {"folders_created": created, "moved": moved,
                    "failed": dict(_failure(exc, target.id, server_log, offset), step=f"create folder {seg!r}"),
                    "not_attempted": [f.id for f in files]}
    for i, data_file in enumerate(files):
        offset = server_log.mark()
        try:
            failure = None if data_file.move(target) else {"error": "DataFile.move returned false"}
        except Exception as exc:
            failure = _failure(exc, data_file.id, server_log, offset)
        if failure:
            return {"folders_created": created, "moved": moved,
                    "failed": dict(failure, id=data_file.id, name=data_file.name),
                    "not_attempted": [f.id for f in files[i + 1:]]}
        moved.append(data_file.id)
    return {"folders_created": created, "moved": moved}


# -- rename planning ---------------------------------------------------------


def plan_folder_rename(folder, new_name, resolver):
    """Return ``(plan, problems)`` for renaming *folder* to *new_name*.

    plan["steps"] lists the names to set in order: one step normally, two
    (through a temporary name) when only the case changes, none when the name
    is already exactly *new_name*.
    """
    problems = []
    path = resolver.folder_path(folder)
    plan = {"folder": path, "id": folder.id, "from": folder.name, "to": new_name, "steps": []}
    if folder.isRoot:
        return plan, [problem("root_folder", "The project root folder cannot be renamed")]
    if folder.name == new_name:
        plan["no_change"] = True
        return plan, problems
    parent = folder.parentFolder
    siblings = [f for f in cloud.items(parent.dataFolders) if f.id != folder.id]
    clashes = [f for f in siblings if f.name.casefold() == new_name.casefold()]
    if clashes:
        problems.append(problem(
            "name_collision",
            f"{resolver.folder_path(parent)!r} already has a folder named {clashes[0].name!r}; "
            "Fusion folder names are case-insensitive",
            existing=[{"path": resolver.folder_path(f), "id": f.id} for f in clashes]))
        return plan, problems
    if folder.name.casefold() == new_name.casefold():
        taken = {f.name.casefold() for f in siblings}
        temp = new_name + _TEMP_SUFFIX
        n = 2
        while temp.casefold() in taken:
            temp = f"{new_name}{_TEMP_SUFFIX}{n}"
            n += 1
        plan["case_only"] = True
        plan["steps"] = [temp, new_name]
    else:
        plan["steps"] = [new_name]
    return plan, problems


def _refetch_folder(folder):
    """A fresh DataFolder from its parent, or *folder* if that cannot be read."""
    try:
        for sibling in cloud.items(folder.parentFolder.dataFolders):
            if sibling.id == folder.id:
                return sibling
    except Exception as exc:
        log(f"[MCP] rename_folder: could not re-fetch folder: {exc}")
    return folder


def _set_folder_name(folder, name, server_log, wait):
    """Set *folder*'s name, waiting out the server's lock.

    Returns ``(folder, failure, retries)``; *folder* may be a fresher object and
    *failure* is None on success. Read-back is what counts as success.
    """
    retries = 0
    failure = None
    for delay in (0.0,) + _LOCK_RETRY_DELAYS_S:
        if delay:
            wait(delay)
            folder = _refetch_folder(folder)
            retries += 1
        offset = server_log.mark()
        try:
            folder.name = name
            if folder.name == name:
                return folder, None, retries
            failure = {"error": f"name reads back as {folder.name!r}"}
            break
        except Exception as exc:
            failure = _failure(exc, folder.id, server_log, offset)
            if not _is_lock(failure):
                break
    failure["attempts"] = retries + 1
    return folder, failure, retries


def _rename_failure_message(name, failure, restored, name_now):
    why = failure.get("reason") or failure["error"]
    message = f"Could not rename the folder to {name!r} after {failure['attempts']} attempt(s): {why}."
    if restored is None:
        return message
    if restored:
        return f"{message} The original name {name_now!r} was restored."
    return f"{message} Restoring failed too: the folder is now named {name_now!r}."


def execute_folder_rename(folder, steps, server_log=None, wait=None):
    """Set each name in *steps*, verifying by read-back; on a failed second step try to restore."""
    server_log = server_log or ServerLog()
    wait = wait or _wait
    original = folder.name
    done = []
    lock_retries = 0
    for name in steps:
        if done:
            wait(_STEP_SETTLE_S)
            folder = _refetch_folder(folder)
        folder, failure, retries = _set_folder_name(folder, name, server_log, wait)
        lock_retries += retries
        if failure:
            result = {"steps_done": done, "failed": dict(failure, to=name), "name_now": folder.name}
            restored = None
            if done:
                folder, restore_failure, _ = _set_folder_name(folder, original, server_log, wait)
                restored = restore_failure is None
                result["restored"] = restored
                if restore_failure:
                    result["restore_error"] = restore_failure
                result["name_now"] = folder.name
            result["message"] = _rename_failure_message(name, failure, restored, result["name_now"])
            return result
        done.append(name)
    result = {"steps_done": done, "name_now": folder.name}
    if lock_retries:
        result["lock_retries"] = lock_retries
    return result


def plan_file_rename(data_file, new_name, open_ids, resolver):
    """Return ``(plan, problems)`` for renaming *data_file* to *new_name*."""
    problems = []
    path = resolver.file_path(data_file)
    plan = {"file": path, "id": data_file.id, "version": data_file.versionNumber,
            "from": data_file.name, "to": new_name}
    ext = data_file.fileExtension or ""
    if ext and new_name.casefold().endswith("." + ext.casefold()):
        problems.append(problem("extension_in_name",
                                 f"Give the name without the .{ext} extension (Fusion keeps it separately)"))
    if data_file.id in open_ids:
        problems.append(problem("open_in_fusion", f"{path!r} is open in Fusion; close it yourself and retry",
                                 id=data_file.id, **open_ids[data_file.id]))
    if data_file.name == new_name:
        plan["no_change"] = True
        return plan, problems
    key = _file_key(new_name, ext)
    clashes = [f for f in cloud.items(data_file.parentFolder.dataFiles)
               if f.id != data_file.id and _file_key(f.name, f.fileExtension) == key]
    if clashes:
        problems.append(problem(
            "name_collision",
            f"{resolver.folder_path(data_file.parentFolder)!r} already has a file named {clashes[0].name!r} "
            "(names compare ignoring case)",
            existing=[describe_file(f, resolver) for f in clashes]))
    return plan, problems


# -- remove_empty_folders planning -------------------------------------------


def find_empty_folders(start, start_path, *, budget_s=None, clock=time.monotonic):
    """Find truly empty folders below *start* (never *start* itself).

    A folder is empty when it has no files and every subfolder is empty.
    Returns ``(empty, complete)``: *empty* is ``[(folder, path)]`` in delete
    order (children before parents); *complete* is false when the time budget
    cut the walk short (unvisited subtrees are never counted as empty).
    """
    deadline = clock() + budget_s if budget_s else None
    empty = []
    state = {"complete": True}

    def visit(folder, path, level):
        if deadline is not None and clock() >= deadline:
            state["complete"] = False
            return False
        if level > cloud.MAX_DEPTH:
            state["complete"] = False
            return False
        has_files = bool(cloud.items(folder.dataFiles))
        all_empty = True
        for sub in sorted(cloud.items(folder.dataFolders), key=lambda f: (f.name.casefold(), f.id)):
            if not visit(sub, cloud.join_path(path, sub.name), level + 1):
                all_empty = False
        is_empty = not has_files and all_empty
        if is_empty and level > 0:
            empty.append((folder, path))
        return is_empty

    visit(start, start_path, 0)
    return empty, state["complete"]


def top_level_paths(empty):
    """The outermost empty folders (deleting them removes everything listed)."""
    ids = {f.id for f, _ in empty}
    return [path for f, path in empty if f.parentFolder is None or f.parentFolder.id not in ids]


def execute_delete_folders(empty, server_log=None):
    """Delete *empty* in order, re-checking each is still empty; stop at the first failure."""
    server_log = server_log or ServerLog()
    deleted_ids, deleted = set(), []
    for i, (folder, path) in enumerate(empty):
        offset = server_log.mark()
        try:
            files = cloud.items(folder.dataFiles)
            remaining = [s for s in cloud.items(folder.dataFolders) if s.id not in deleted_ids]
            if files or remaining:
                failure = {"error": (f"no longer empty ({len(files)} files, {len(remaining)} subfolders); "
                                     "something changed since planning")}
            else:
                failure = None if folder.deleteMe() else {"error": "DataFolder.deleteMe returned false"}
        except Exception as exc:
            failure = _failure(exc, folder.id, server_log, offset)
        if failure:
            return {"deleted": deleted, "failed": dict(failure, path=path),
                    "not_attempted": [p for _, p in empty[i + 1:]]}
        deleted_ids.add(folder.id)
        deleted.append(path)
    return {"deleted": deleted}


# -- Argument validation -----------------------------------------------------

_COMMON_ARGS = {"project", "dry_run"}
MOVE_ARGS = _COMMON_ARGS | {"files", "to_folder", "to_project", "create_missing"}
RENAME_FOLDER_ARGS = _COMMON_ARGS | {"folder", "new_name"}
RENAME_FILE_ARGS = _COMMON_ARGS | {"file", "new_name"}
REMOVE_EMPTY_ARGS = _COMMON_ARGS | {"folder"}


def _check_common(arguments, known):
    require_object(arguments)
    reject_unknown(arguments, known)
    return {"project": project_arg(arguments), "dry_run": bool_arg(arguments, "dry_run", True)}


def _folder_segments(value, what, required):
    try:
        segments = cloud.split_path(value)
    except ValueError as exc:
        raise ValueError(f"'{what}': {exc}")
    if required and not segments:
        raise ValueError(f"'{what}' must name a folder inside the project")
    return segments


def validate_move(arguments):
    opts = _check_common(arguments, MOVE_ARGS)
    files = arguments.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("'files' must be a non-empty list of file paths or ids")
    if len(files) > MAX_FILES_PER_CALL:
        raise ValueError(f"'files' may list at most {MAX_FILES_PER_CALL} files per call")
    if not all(isinstance(f, str) and f.strip() for f in files):
        raise ValueError("every entry in 'files' must be a non-empty string")
    if "to_folder" not in arguments or not isinstance(arguments["to_folder"], str):
        raise ValueError("'to_folder' is required: a folder path ('' for the project root)")
    to_project = text_arg(arguments, "to_project", suffix=" (project name or id)")
    create_missing = bool_arg(arguments, "create_missing")
    opts.update(files=[f.strip() for f in files],
                to_segments=_folder_segments(arguments["to_folder"], "to_folder", False),
                to_project=to_project.strip() if to_project else None,
                create_missing=create_missing)
    return opts


def validate_rename_folder(arguments):
    opts = _check_common(arguments, RENAME_FOLDER_ARGS)
    opts["segments"] = _folder_segments(arguments.get("folder"), "folder", True)
    opts["new_name"] = validate_new_name(arguments.get("new_name"), "new_name")
    return opts


def validate_rename_file(arguments):
    opts = _check_common(arguments, RENAME_FILE_ARGS)
    spec = arguments.get("file")
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("'file' must be a non-empty string (file path or id)")
    opts["file"] = spec.strip()
    opts["new_name"] = validate_new_name(arguments.get("new_name"), "new_name")
    return opts


def validate_remove_empty(arguments):
    opts = _check_common(arguments, REMOVE_EMPTY_ARGS)
    opts["segments"] = _folder_segments(arguments.get("folder"), "folder", False)
    return opts


# -- Handlers ----------------------------------------------------------------


def _refused(tool, plan, problems):
    return _result({"tool": tool, "refused": True, "changed": False, "problems": problems, "plan": plan}, True)


def _finder(data, held):
    """Look files up by id: ``Data.findFileById`` when available, else the held objects."""
    find = getattr(data, "findFileById", None)

    def lookup(fid):
        if callable(find):
            return find(fid)
        return held.get(fid)

    return lookup


def _guarded(tool, validator, body):
    def handler(arguments):
        try:
            options = validator(arguments)
        except ValueError as exc:
            return error_result(f"Error: {exc}")
        started = time.monotonic()
        try:
            app = get_app()
            projects = cloud.items(app.data.dataProjects)
            try:
                project = cloud.find_project(projects, options["project"])
            except LookupError as exc:
                return error_result(f"Error: {exc}")
            result = body(app, projects, project, options)
            log(f"[MCP] {tool} in {project.name!r} (dry_run={options['dry_run']}) "
                f"took {time.monotonic() - started:.2f}s")
            return result
        except Exception as exc:
            tb = traceback.format_exc()
            log(f"[MCP] {tool} failed: {exc}")
            return error_result(f"Error: {exc}\n{tb}")

    handler.__name__ = tool
    return handler


def _apply_with_link_check(primary, expected_ids, data, change, resolver):
    """Snapshot links, run *change*, snapshot again; return (outcome, link_report)."""
    before, capped = link_snapshot(primary, resolver)
    held = {f.id: f for f in primary}
    outcome = change()
    after = link_snapshot_after(list(before), _finder(data, held), cloud.PathResolver())
    report = diff_links(before, after, expected_ids)
    if capped:
        report["capped_at"] = LINK_CHECK_MAX_FILES
    return outcome, report


def _dry_run_links(primary, resolver):
    before, capped = link_snapshot(primary, resolver)
    listing = {"files_to_check": len(before), "links": links_by_path(before, [f.id for f in primary])}
    if capped:
        listing["capped_at"] = LINK_CHECK_MAX_FILES
    return listing


def _move_body(app, projects, project, options):
    data = app.data
    target_project = project
    if options["to_project"]:
        try:
            target_project = cloud.find_project(projects, options["to_project"])
        except LookupError as exc:
            return error_result(f"Error: 'to_project': {exc}")
    resolver = cloud.PathResolver()
    find = getattr(data, "findFileById", None)
    find_by_id = find if callable(find) else (lambda fid: None)
    plan, problems, (files, target, missing) = plan_move(
        project, options["files"], target_project, options["to_segments"],
        create_missing=options["create_missing"], find_by_id=find_by_id,
        open_ids=open_file_ids(app.documents), resolver=resolver)
    if target_project.id != project.id:
        plan["cross_project"] = {"from": project.name, "to": target_project.name}
    if problems:
        return _refused("move_files", plan, problems)
    if options["dry_run"]:
        return _result({"tool": "move_files", "dry_run": True, "changed": False, "plan": plan,
                        "link_check": _dry_run_links(files, resolver), "notes": NOTES,
                        "next": "Re-run with dry_run=false to apply."})
    outcome, report = _apply_with_link_check(
        files, {f.id for f in files}, data, lambda: execute_move(files, target, missing, current_log()), resolver)
    failed = "failed" in outcome
    return _result({"tool": "move_files", "dry_run": False, "changed": bool(outcome["moved"] or
                                                                            outcome["folders_created"]),
                    "plan": plan, "result": outcome, "link_check": report}, failed or not report["ok"])


def _rename_folder_body(app, projects, project, options):
    resolver = cloud.PathResolver()
    try:
        folder, _ = cloud.resolve_folder(project.rootFolder, options["segments"])
    except LookupError as exc:
        return error_result(f"Error: {exc}")
    plan, problems = plan_folder_rename(folder, options["new_name"], resolver)
    if problems:
        return _refused("rename_folder", plan, problems)
    if plan.get("no_change"):
        return _result({"tool": "rename_folder", "dry_run": options["dry_run"], "changed": False, "plan": plan,
                        "message": "The folder already has exactly that name."})
    files, capped = collect_files(folder, LINK_CHECK_MAX_FILES)
    if capped:
        plan["link_check_note"] = (f"Folder holds more than {LINK_CHECK_MAX_FILES} files; "
                                   f"only the first {LINK_CHECK_MAX_FILES} are link-checked")
    if options["dry_run"]:
        return _result({"tool": "rename_folder", "dry_run": True, "changed": False, "plan": plan,
                        "link_check": _dry_run_links(files, resolver),
                        "next": "Re-run with dry_run=false to apply."})
    outcome, report = _apply_with_link_check(
        files, {f.id for f in files}, app.data,
        lambda: execute_folder_rename(folder, plan["steps"], current_log()), resolver)
    failed = "failed" in outcome
    return _result({"tool": "rename_folder", "dry_run": False, "changed": bool(outcome["steps_done"]),
                    "plan": plan, "result": outcome, "link_check": report}, failed or not report["ok"])


def _rename_file_body(app, projects, project, options):
    data = app.data
    resolver = cloud.PathResolver()
    find = getattr(data, "findFileById", None)
    try:
        data_file = resolve_file(project, options["file"], find if callable(find) else (lambda fid: None), resolver)
    except Refusal as refusal:
        return _refused("rename_file", {"file": options["file"]}, [refusal.problem])
    plan, problems = plan_file_rename(data_file, options["new_name"], open_file_ids(app.documents), resolver)
    if problems:
        return _refused("rename_file", plan, problems)
    if plan.get("no_change"):
        return _result({"tool": "rename_file", "dry_run": options["dry_run"], "changed": False, "plan": plan,
                        "message": "The file already has exactly that name."})
    if options["dry_run"]:
        return _result({"tool": "rename_file", "dry_run": True, "changed": False, "plan": plan,
                        "link_check": _dry_run_links([data_file], resolver),
                        "next": "Re-run with dry_run=false to apply."})

    def change():
        server_log = current_log()
        offset = server_log.mark()
        try:
            data_file.name = options["new_name"]
            if data_file.name != options["new_name"]:
                return {"renamed": False, "failed": {"error": f"name reads back as {data_file.name!r}"}}
        except Exception as exc:
            return {"renamed": False, "failed": _failure(exc, data_file.id, server_log, offset)}
        return {"renamed": True}

    outcome, report = _apply_with_link_check([data_file], {data_file.id}, data, change, resolver)
    return _result({"tool": "rename_file", "dry_run": False, "changed": outcome["renamed"],
                    "plan": plan, "result": outcome, "link_check": report},
                   "failed" in outcome or not report["ok"])


def _remove_empty_body(app, projects, project, options):
    try:
        folder, actual = cloud.resolve_folder(project.rootFolder, options["segments"])
    except LookupError as exc:
        return error_result(f"Error: {exc}")
    start_path = cloud.join_path(project.name, *actual)
    empty, complete = find_empty_folders(folder, start_path, budget_s=project_tree.time_budget())
    plan = {"root": start_path, "empty_folders": [p for _, p in empty], "top_level": top_level_paths(empty),
            "walk_complete": complete}
    if not complete:
        plan["note"] = "The walk stopped early (time budget or depth); only fully walked folders are listed."
    if not empty:
        return _result({"tool": "remove_empty_folders", "dry_run": options["dry_run"], "changed": False,
                        "plan": plan, "message": "No empty folders found."})
    if options["dry_run"]:
        return _result({"tool": "remove_empty_folders", "dry_run": True, "changed": False, "plan": plan,
                        "next": "Re-run with dry_run=false to delete them (children first)."})
    outcome = execute_delete_folders(empty, current_log())
    return _result({"tool": "remove_empty_folders", "dry_run": False, "changed": bool(outcome["deleted"]),
                    "plan": plan, "result": outcome}, "failed" in outcome)


move_files = _guarded("move_files", validate_move, _move_body)
rename_folder = _guarded("rename_folder", validate_rename_folder, _rename_folder_body)
rename_file = _guarded("rename_file", validate_rename_file, _rename_file_body)
remove_empty_folders = _guarded("remove_empty_folders", validate_remove_empty, _remove_empty_body)
