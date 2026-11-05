"""Switch cloud files between read-only and editable (Personal Use slots).

The API has no setter for ``DataFile.isReadOnly``. Fusion's own UI commands do
it, but only for the ACTIVE document, and ``CommandDefinition.execute()`` only
queues: the command runs once control returns to Fusion's event loop. Run back
to back while an assembly was loading they did nothing for a long while, left
Fusion unresponsive, and one finished much later. So this tool works one file
at a time: wait for Fusion to go quiet, bring the file to the front, wait
again, run the command, verify through a fresh ``findFileById``, and stop at
the first failure. Every file has a time limit and the batch stays inside the
bridge's 120 s main-thread limit; files it did not reach come back as
``not_attempted`` so the caller can call again.

Rules:

- A document with unsaved changes is never touched and never discarded.
- Only documents this call opened are closed, and only when unmodified and
  when closing cannot take an edited linked document with it. ``close()`` on
  an instance held by an open parent silently does nothing, so closes are
  re-checked and reported as ``still_open``.
- A command that was sent but not seen to finish may still run later, on
  whatever is active by then. The target is therefore left active: no close,
  no re-activation of the starting document.

Planning and state logic are plain functions over dicts, so they test with
fakes; the glue reads ``time`` through this module.
"""

import time
import traceback
from collections import Counter

from .common.api import safe, strict_items
from .common.args import bool_arg, is_int, reject_unknown, text_arg
from .common.cloud import ID_PREFIX, path_segments, resolve_path, resolve_project
from .common.documents import instances_of, open_documents, pump
from .common.results import tool_result
from .dispatch import get_app, log

COMMAND_IDS = {
    "read_only": "ActiveDocumentDeactivateCommand",
    "editable": "ActiveDocumentActivateCommand",
}
MAX_FILES = 10
_ARGS = {"files", "project", "state", "dry_run"}

PER_FILE_TIMEOUT_S = 25.0
# The bridge abandons a main-thread call after settings.MCP_MAIN_THREAD_TIMEOUT
# (120 s). A file only starts when a full PER_FILE_TIMEOUT_S fits in this
# budget; the margin left over covers the close, the restore and the reply.
TOTAL_BUDGET_S = 100.0
# Settling may not eat the time the command needs to be verified.
COMMAND_RESERVE_S = 10.0
SETTLE_QUIET_S = 1.0
CLOSE_SETTLE_QUIET_S = 0.5
CLOSE_SETTLE_TIMEOUT_S = 3.0
CLOSE_VERIFY_TIMEOUT_S = 2.0
RESTORE_TIMEOUT_S = 3.0
# A doEvents call that takes longer than this was doing real work.
MAX_QUIET_PUMP_S = 0.25
_POLL_S = 0.1
_STATE_POLL_S = 0.5
_IDLE_COMMANDS = ("SelectCommand", None, "")


def _row_file_id(info):
    return info.get("file_id")


# -- Validation ---------------------------------------------------------------


def validate_arguments(arguments):
    """Return (files, project, state, dry_run), or raise ValueError."""
    reject_unknown(arguments, _ARGS)
    state = arguments.get("state")
    if state not in COMMAND_IDS:
        raise ValueError(f"'state' must be one of {sorted(COMMAND_IDS)}, got {state!r}")
    dry_run = bool_arg(arguments, "dry_run", True)
    project = text_arg(arguments, "project")

    files = arguments.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError(f"'files' must be a list of 1 to {MAX_FILES} DataFile ids or paths")
    if not all(isinstance(spec, str) and spec.strip() for spec in files):
        raise ValueError("every 'files' entry must be a non-empty string")
    files = [spec.strip() for spec in files]
    repeated = sorted(spec for spec, n in Counter(files).items() if n > 1)
    if repeated:
        raise ValueError(f"'files' lists the same entry twice: {repeated}")
    if not project and not all(spec.startswith(ID_PREFIX) for spec in files):
        raise ValueError(f"'project' is required for entries that are paths, not ids ('{ID_PREFIX}...')")
    return files, project.strip() if project else None, state, dry_run


def match_named(objects, name, label):
    """The one object called *name* (or name.ext), ignoring case only if no exact hit.

    Names are not unique in Fusion, so several hits raise LookupError rather
    than picking one.
    """
    rows = []
    for obj in objects:
        own = safe(obj, "name") or ""
        ext = safe(obj, "fileExtension")
        rows.append((obj, {own, f"{own}.{ext}"} if ext else {own}))
    hits = ([obj for obj, names in rows if name in names]
            or [obj for obj, names in rows if name.casefold() in {n.casefold() for n in names}])
    if not hits:
        found = sorted(safe(obj, "name") or "?" for obj, _ in rows)[:50]
        raise LookupError(f"No {label} named {name!r}; found: {found}")
    if len(hits) > 1:
        listed = "; ".join(f"{safe(o, 'name')} (id {safe(o, 'id')})" for o in hits)
        raise LookupError(f"{len(hits)} {label}s named {name!r}: {listed}; use the id")
    return hits[0]


# -- Pure planning ------------------------------------------------------------


def state_name(is_read_only):
    """``DataFile.isReadOnly`` as a state; None when it could not be read."""
    if is_read_only is True:
        return "read_only"
    return "editable" if is_read_only is False else None


def pick_instance(infos, file_id):
    """Index of the instance to reuse: active, then visible, then newest; or None."""
    hits = instances_of(infos, file_id, key=_row_file_id)
    if not hits:
        return None
    return max(hits, key=lambda i: (infos[i].get("is_active") is True,
                                    infos[i].get("is_visible") is not False,
                                    infos[i].get("version") or 0))


def plan_file(row, infos, target):
    """What to do with one file: {"action": "already"|"skip"|"change", ...}.

    A change also says how the file reaches the front: ``open_action`` is
    "none" (already active), "activate" or "open". An unreadable isModified
    counts as modified: never gamble with unsaved work.
    """
    before = row.get("before")
    if before is None:
        return {"action": "skip", "reason": "state_unknown",
                "message": "Could not read DataFile.isReadOnly for this file."}
    if before == target:
        return {"action": "already"}

    hits = instances_of(infos, row["id"], key=_row_file_id)
    modified = [infos[i].get("name") for i in hits if infos[i].get("is_modified") is not False]
    if modified:
        return {"action": "skip", "reason": "unsaved_changes", "documents": modified,
                "message": "The open document has unsaved changes; save or discard them first."}
    index = pick_instance(infos, row["id"])
    if index is None:
        return {"action": "change", "open_action": "open", "warnings": []}

    info = infos[index]
    warnings = []
    if len(hits) > 1:
        warnings.append(f"{len(hits)} instances of this file are open; using {info.get('name')!r}")
    latest = row.get("latest_version")
    if is_int(latest) and is_int(info.get("version")) and info["version"] < latest:
        warnings.append(f"the open instance has v{info['version']} loaded; the latest is v{latest}")
    if info.get("is_visible") is False:
        warnings.append("the only open instance is hidden (loaded by a parent); Fusion may refuse to activate it")
    return {"action": "change", "open_action": "none" if info.get("is_active") is True else "activate",
            "index": index, "warnings": warnings}


def free_slots(limits):
    """Editable slots left, or None when there is no limit to go by (paid licence)."""
    if not limits or not is_int(limits.get("count")) or not is_int(limits.get("max")):
        return None
    return max(0, limits["max"] - limits["count"])


def plan_batch(rows, infos, target, limits):
    """Plan every file; files beyond the free editable slots are refused up front."""
    plans = [plan_file(row, infos, target) for row in rows]
    free = free_slots(limits) if target == "editable" else None
    if free is None:
        return plans
    for i, plan in enumerate(plans):
        if plan["action"] != "change":
            continue
        if free > 0:
            free -= 1
            continue
        plans[i] = {"action": "skip", "reason": "no_editable_slots",
                    "message": (f"No editable slot for this file: {limits['count']} of {limits['max']} "
                                "are in use. Make another file read-only first.")}
    return plans


class SettleTracker:
    """Decide from successive polls whether Fusion has gone quiet.

    Quiet means the target is active, no command is running, the pump returned
    quickly and the document count stopped moving (a loading assembly keeps
    adding documents for its links), all held for *quiet_s* without a break.
    """

    def __init__(self, quiet_s):
        self.quiet_s = quiet_s
        self.quiet_since = None
        self.last_count = None

    def observe(self, now, target_active, active_command, document_count, pump_s):
        """Record a poll. Returns True once the quiet period is complete."""
        quiet = (target_active and active_command in _IDLE_COMMANDS
                 and pump_s <= MAX_QUIET_PUMP_S and document_count == self.last_count)
        self.last_count = document_count
        if not quiet:
            self.quiet_since = None
            return False
        if self.quiet_since is None:
            self.quiet_since = now
        return now - self.quiet_since >= self.quiet_s


def close_block(is_modified, linked_ids, edited_ids):
    """Why a document this call opened must stay open, or None when it may close.

    Closing a document also closes documents it links, so one that links an
    edited document stays open. *linked_ids* is None when the links could not
    be read; with edited documents around, that counts as linked.
    """
    if is_modified is not False:
        return "unsaved_changes"
    if edited_ids and (linked_ids is None or set(linked_ids) & set(edited_ids)):
        return "links_edited_document"
    return None


def lost_documents(before, after):
    """Names of the documents in *before* that are missing from *after*.

    Documents are matched by DataFile id, as a name can change with its
    version suffix; unsaved ones have no id and fall back to the name.
    """
    def key(info):
        return info.get("file_id") or info.get("name")

    missing = Counter(key(info) for info in before) - Counter(key(info) for info in after)
    lost = []
    for info in before:
        if missing[key(info)] > 0:
            missing[key(info)] -= 1
            lost.append(info.get("name"))
    return lost


def timeout_message(target, seconds):
    """Explain a command that did not change the state in time."""
    message = (f"State did not change within {seconds:.0f} s. The command may still run later, so the "
               "document was left active and open; check with a dry run before retrying.")
    if target == "editable":
        message += (" Make Editable may have shown a dialog (not tested): check fusion_dialogs. "
                    "It can also be refused at the editable limit.")
    return message


def summarize(entries):
    """Count entries by status."""
    return dict(Counter(entry["status"] for entry in entries))


# -- Fusion reads -------------------------------------------------------------


def _read_state(app, file_id):
    """Current state from a fresh cloud lookup, or None."""
    try:
        data_file = app.data.findFileById(file_id)
    except Exception:
        return None
    return state_name(safe(data_file, "isReadOnly")) if data_file is not None else None


def _editable_limits(app):
    """Personal Use counters; None on paid licences (personalUseLimits is null)."""
    limits = safe(app.data, "personalUseLimits")
    if limits is None:
        return None
    return {"count": safe(limits, "editableFileCount"), "max": safe(limits, "maxEditableFileCount")}


def _describe(document):
    data_file = safe(document, "dataFile")
    return {
        "name": safe(document, "name"),
        "is_active": safe(document, "isActive"),
        "is_visible": safe(document, "isVisible"),
        "is_modified": safe(document, "isModified"),
        "file_id": safe(data_file, "id"),
        "version": safe(data_file, "versionNumber"),
    }


def _snapshot(app):
    """(documents, infos) for everything open, hidden instances included."""
    documents = open_documents(app)
    return documents, [_describe(document) for document in documents]


def _linked_file_ids(document):
    """File ids *document* links, or None when they could not be read."""
    try:
        return {safe(safe(ref, "dataFile"), "id") for ref in strict_items(document.documentReferences)}
    except Exception:
        return None


def _resolve_project(app, project):
    return resolve_project(strict_items(app.data.dataProjects), project, match_named)


def _resolve_path(project_obj, path):
    segments = path_segments(path)
    if not segments:
        raise LookupError(f"{path!r} does not name a file")
    return resolve_path(project_obj.rootFolder, segments, match_named)


def _resolve_one(app, spec, project_obj):
    if spec.startswith(ID_PREFIX):
        data_file = app.data.findFileById(spec)
        if data_file is None:
            raise LookupError(f"No file with id {spec!r}")
        return data_file
    return _resolve_path(project_obj, spec)


def _resolve_rows(app, specs, project):
    """Return (rows, problems); one row per file, nothing touched."""
    problems = []
    project_obj = None
    if project:
        try:
            project_obj = _resolve_project(app, project)
        except LookupError as exc:
            return [], [str(exc)]

    rows = []
    for spec in specs:
        try:
            data_file = _resolve_one(app, spec, project_obj)
        except LookupError as exc:
            problems.append(str(exc))
            continue
        except Exception as exc:
            problems.append(f"{spec!r}: {type(exc).__name__}: {exc}")
            continue
        rows.append({
            "file": spec,
            "id": safe(data_file, "id"),
            "name": safe(data_file, "name"),
            "before": state_name(safe(data_file, "isReadOnly")),
            "latest_version": safe(data_file, "latestVersionNumber"),
            "data_file": data_file,
        })
    repeated = sorted(file_id for file_id, n in Counter(row["id"] for row in rows).items() if n > 1)
    if repeated:
        problems.append(f"several 'files' entries are the same file: {repeated}")
    return rows, problems


# -- Waiting ------------------------------------------------------------------


def _target_is_active(app, file_id):
    active = safe(app, "activeDocument")
    if active is None or safe(safe(active, "dataFile"), "id") != file_id:
        return False
    return safe(app, "activeProduct") is not None


def _wait_settled(app, file_id, deadline, quiet_s=SETTLE_QUIET_S):
    """Pump until Fusion is quiet (with *file_id* in front, unless None)."""
    tracker = SettleTracker(quiet_s)
    started = time.monotonic()
    ui = app.userInterface
    while True:
        pump_s = pump()
        now = time.monotonic()
        command = safe(ui, "activeCommand")
        target_active = file_id is None or _target_is_active(app, file_id)
        settled = tracker.observe(now, target_active, command, safe(app.documents, "count"), pump_s)
        if settled or now >= deadline:
            return {"settled": settled, "waited_seconds": round(now - started, 2),
                    "active_command": command, "target_active": target_active}
        time.sleep(_POLL_S)


def _wait_for_state(app, file_id, target, deadline):
    """Pump until the cloud reports *target*; returns the last state read."""
    state = None
    next_read = 0.0
    while True:
        pump()
        now = time.monotonic()
        if now >= next_read or now >= deadline:
            state = _read_state(app, file_id)
            next_read = now + _STATE_POLL_S
            if state == target or now >= deadline:
                return state
        time.sleep(_POLL_S)


def _still_open(app, file_id, deadline):
    """Pump until no instance of the file is open; returns those that remain."""
    while True:
        pump()
        _, infos = _snapshot(app)
        hits = [infos[i].get("name") for i in instances_of(infos, file_id, key=_row_file_id)]
        if not hits or time.monotonic() >= deadline:
            return hits
        time.sleep(_POLL_S)


# -- One file -----------------------------------------------------------------


def _fail(entry, reason, message):
    entry.update(status="failed", reason=reason, message=message)


def _not_settled(entry, settle, phase):
    _fail(entry, "not_settled", (
        f"Fusion did not go quiet {phase} (waited {settle['waited_seconds']} s, active command "
        f"{settle['active_command']!r}); the command was not run."))
    entry["settle"] = settle


def _close_opened(app, document, entry, file_id, edited_ids):
    """Close a document this call opened, if that is safe, and verify it went."""
    settle = _wait_settled(app, file_id, time.monotonic() + CLOSE_SETTLE_TIMEOUT_S, CLOSE_SETTLE_QUIET_S)
    if not settle["settled"]:
        entry["close_skipped"] = "not_settled"
        return
    block = close_block(safe(document, "isModified"), _linked_file_ids(document), edited_ids)
    if block:
        entry["close_skipped"] = block
        return
    try:
        document.close(False)
    except Exception as exc:
        entry["close_note"] = f"close() raised {type(exc).__name__}: {exc}"
    remaining = _still_open(app, file_id, time.monotonic() + CLOSE_VERIFY_TIMEOUT_S)
    entry["still_open"] = bool(remaining)
    entry["closed"] = not remaining
    if remaining and "close_note" not in entry:
        entry["close_note"] = (f"close() left {remaining} open: Fusion ignores it for an instance held by "
                               "an open parent document.")


def _bring_to_front(app, row, plan, documents, entry):
    """Open or activate the file. Returns its document, or None after a failure."""
    if plan["open_action"] == "open":
        document = app.documents.open(row["data_file"], True)
        if document is None:
            _fail(entry, "open_failed", "Fusion did not open the document.")
            return None
        entry["opened"] = entry["still_open"] = True
        if safe(document, "isActive") is not True:
            document.activate()
        return document

    document = documents[plan["index"]]
    if plan["open_action"] == "activate" and not document.activate():
        _fail(entry, "activate_failed", (
            f"Fusion refused to activate {safe(document, 'name')!r}. A hidden instance held by a parent "
            "cannot always be brought forward; close the parent, or open the file in its own tab."))
        return None
    return document


def _change_file(app, cmd_def, row, target, batch_deadline, entry):
    """Take one file to *target*, filling in *entry*."""
    started = time.monotonic()
    deadline = min(started + PER_FILE_TIMEOUT_S, batch_deadline)
    file_id = row["id"]

    # Plan again: the previous file may have opened or closed documents.
    documents, infos = _snapshot(app)
    plan = plan_file(dict(row, before=_read_state(app, file_id)), infos, target)
    entry["warnings"] = plan.get("warnings", [])
    if plan["action"] != "change":
        _apply_plan(entry, plan)
        if plan["action"] == "already":
            entry["after"] = target
        return
    entry["open_action"] = plan["open_action"]
    edited_ids = {info.get("file_id") for info in infos if info.get("is_modified") is not False} - {None}

    if plan["open_action"] != "none":
        settle = _wait_settled(app, None, deadline - COMMAND_RESERVE_S)
        if not settle["settled"]:
            _not_settled(entry, settle, "before switching documents")
            return
    document = _bring_to_front(app, row, plan, documents, entry)
    if document is None:
        return

    # open() blocks and cannot be polled, so its time does not count: the wait
    # for quiet would otherwise be starved exactly when the file is big.
    if plan["open_action"] == "open":
        deadline = min(time.monotonic() + PER_FILE_TIMEOUT_S, batch_deadline)
        if deadline - COMMAND_RESERVE_S - time.monotonic() < SETTLE_QUIET_S:
            _not_attempted(entry, "time_budget")
            _close_opened(app, document, entry, file_id, edited_ids)
            return
    settle = _wait_settled(app, file_id, deadline - COMMAND_RESERVE_S)
    if not settle["settled"]:
        _not_settled(entry, settle, "with this file in front")
        return

    _, infos = _snapshot(app)
    modified = [infos[i].get("name") for i in instances_of(infos, file_id, key=_row_file_id)
                if infos[i].get("is_modified") is not False]
    if modified:
        entry.update(status="skipped", reason="unsaved_changes", documents=modified,
                     message="The document has unsaved changes; nothing was run and it was left open.")
        return

    current = _read_state(app, file_id)
    if current == target:
        entry.update(status="already", after=current)
    else:
        entry["command_pending"] = True
        cmd_def.execute()
        current = _wait_for_state(app, file_id, target, deadline)
        entry["after"] = current
        if current != target:
            _fail(entry, "timeout", timeout_message(target, time.monotonic() - started))
            return
        entry["command_pending"] = False
        entry["status"] = "changed"

    if entry["opened"]:
        _close_opened(app, document, entry, file_id, edited_ids)


def _new_entry(row):
    return {"file": row["file"], "id": row["id"], "name": row["name"], "status": None,
            "before": row["before"], "after": row["before"], "elapsed_seconds": 0.0,
            "opened": False, "closed": False, "still_open": False, "command_pending": False}


def _apply_plan(entry, plan):
    """Fill in *entry* for a file that needs no change or was refused."""
    entry["status"] = "already" if plan["action"] == "already" else "skipped"
    for key in ("reason", "message", "documents"):
        if key in plan:
            entry[key] = plan[key]


# -- The batch ----------------------------------------------------------------


def _not_attempted(entry, reason):
    entry.update(status="not_attempted", reason=reason, message={
        "time_budget": "Out of time for this call; call again with the remaining files.",
        "stopped_after_failure": "Not attempted because an earlier file failed.",
    }[reason])


def _run_batch(app, cmd_def, rows, plans, target, batch_deadline):
    """Work through the planned files in order; returns their entries."""
    entries = []
    stop = None
    for row, plan in zip(rows, plans):
        entry = _new_entry(row)
        entries.append(entry)
        if plan["action"] != "change":
            _apply_plan(entry, plan)
            continue
        if stop is None and time.monotonic() + PER_FILE_TIMEOUT_S > batch_deadline:
            stop = "time_budget"
        if stop:
            _not_attempted(entry, stop)
            continue

        started = time.monotonic()
        try:
            _change_file(app, cmd_def, row, target, batch_deadline, entry)
        except Exception as exc:
            _fail(entry, "error", f"{type(exc).__name__}: {exc}")
            log(f"[MCP] set_editable_state {row['name']!r}: {traceback.format_exc()}")
        entry["elapsed_seconds"] = round(time.monotonic() - started, 2)
        log(f"[MCP] set_editable_state {row['name']!r}: {entry['status']} "
            f"({entry.get('reason', target)}) in {entry['elapsed_seconds']}s")
        if entry["status"] == "failed":
            stop = "stopped_after_failure"
    return entries


def _restore_active(app, document, name, pending):
    """Put the starting document back in front, unless a command is pending."""
    result = {"name": name, "restored": False}
    if document is None:
        result["note"] = "No document was active at the start."
        return result
    if pending:
        result["note"] = ("Not restored: a command may still run on the active document, and it must not "
                          "land on this one.")
        return result
    try:
        if safe(document, "isActive") is not True:
            document.activate()
        deadline = time.monotonic() + RESTORE_TIMEOUT_S
        while safe(document, "isActive") is not True and time.monotonic() < deadline:
            pump()
            time.sleep(_POLL_S)
        result["restored"] = safe(document, "isActive") is True
    except Exception as exc:
        result["note"] = f"Could not re-activate it: {type(exc).__name__}: {exc}"
    if not result["restored"] and "note" not in result:
        result["note"] = "It did not become active again; it may have closed."
    return result


def _dry_run_entry(row, plan, target):
    entry = {"file": row["file"], "id": row["id"], "name": row["name"], "before": row["before"], "after": target}
    if plan["action"] == "change":
        entry.update(status="would_change", open_action=plan["open_action"], warnings=plan["warnings"])
    else:
        entry["after"] = row["before"]
        _apply_plan(entry, plan)
    return entry


def _limits_report(before, after=None):
    report = {"before": (before or {}).get("count"), "max": (before or {}).get("max"),
              "free_before": free_slots(before)}
    if after is not None:
        report["after"] = after.get("count")
    return report


def set_editable_state(arguments):
    """Make cloud files read-only or editable, one at a time and verified."""
    try:
        specs, project, target, dry_run = validate_arguments(arguments)
    except ValueError as exc:
        return tool_result(f"Error: {exc}", True)

    try:
        started = time.monotonic()
        app = get_app()
        rows, problems = _resolve_rows(app, specs, project)
        if problems:
            return tool_result({"error": "Nothing was changed: some files could not be resolved.",
                          "problems": problems}, True)

        _, infos = _snapshot(app)
        limits = _editable_limits(app)
        plans = plan_batch(rows, infos, target, limits)
        needs_command = any(plan["action"] == "change" for plan in plans)
        command_id = COMMAND_IDS[target]
        cmd_def = app.userInterface.commandDefinitions.itemById(command_id) if needs_command else None
        if needs_command and cmd_def is None:
            return tool_result(f"Error: Fusion command {command_id!r} is not available in this build", True)

        result = {"dry_run": dry_run, "state": target, "command": command_id}
        if dry_run:
            entries = [_dry_run_entry(row, plan, target) for row, plan in zip(rows, plans)]
            result.update(files=entries, summary=summarize(entries), editable_count=_limits_report(limits))
            return tool_result(result)

        active = safe(app, "activeDocument")
        active_name = safe(active, "name")
        entries = _run_batch(app, cmd_def, rows, plans, target, started + TOTAL_BUDGET_S)
        pending = any(entry["command_pending"] for entry in entries)
        if needs_command:
            restored = _restore_active(app, active, active_name, pending)
        else:
            restored = {"name": active_name, "restored": True}
        _, infos_after = _snapshot(app)
        limits_after = _editable_limits(app)

        result.update(
            files=entries,
            summary=summarize(entries),
            complete=all(entry["status"] in ("changed", "already") for entry in entries),
            remaining_files=[entry["id"] for entry in entries if entry["status"] == "not_attempted"],
            command_pending=pending,
            editable_count=_limits_report(limits, limits_after or {}),
            active_document=restored,
            documents_lost=lost_documents(infos, infos_after),
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        return tool_result(result, any(entry["status"] in ("failed", "skipped") for entry in entries))
    except Exception as exc:
        log(f"[MCP] set_editable_state failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)
