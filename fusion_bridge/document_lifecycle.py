"""Close a document and update its links, reporting what really happened.

``Document.close(False)`` on an instance held by an open parent returns without
closing anything, and closing a parent silently closes its children. So
``close_document`` makes exactly one attempt per instance, then compares
``app.documents`` before and after: the return value is reported, never trusted.
What stays open comes back with the open parents holding it; a retry cannot help.

``DocumentReference.isOutOfDate`` is equally unreliable: it stayed False while
the parent was open after the child was saved, and only turned True once the
parent was reopened. ``update_references`` therefore compares the loaded version
with the cloud's latest itself, and judges ``getLatestVersion()`` by whether the
reference's version number moved. A successful update leaves the parent modified,
so nothing is finished until the parent is saved.

Decision logic is Fusion-free and tested with fakes; the glue reads ``time`` through
this module.
"""

import time
import traceback
from collections import Counter

from .common.api import safe, strict_items
from .common.args import bool_arg, int_or_none, reject_unknown
from .common.documents import open_documents, pump
from .common.results import tool_result
from .dispatch import get_app, log

_POLL_S = 0.1
_MAX_FOLDER_DEPTH = 64

# A held instance never goes, so this is how long a refused close takes to report.
CLOSE_VERIFY_TIMEOUT_S = 3.0
# Children go after their parent: keep watching until the list stops changing.
CLOSE_QUIET_S = 0.5
# Cloud lookups and folder walks are network calls on the main thread.
LOOKUP_BUDGET_S = 30.0
# The bridge abandons a main-thread call after 120 s; stay well inside it.
APPLY_BUDGET_S = 60.0
UPDATE_VERIFY_TIMEOUT_S = 5.0

CURRENT = "current"
OUT_OF_DATE = "out_of_date"
STALE_NOT_FLAGGED = "stale_not_flagged"
UNKNOWN = "unknown"

UPDATED = "updated"
NO_CHANGE = "no_change"
UNVERIFIED = "unverified"
FAILED = "failed"
NOT_ATTEMPTED = "not_attempted"

UNSAVED_FIX = (
    "Nothing was closed. Save first (save_document), or pass discard_unsaved: true to lose these "
    "changes. Closing a document also closes the documents it links."
)
HELD_FIX = (
    "Fusion ignores close() on an instance held by an open parent, and retrying changes nothing. "
    "Close {parents} first (close_document); that closes this instance with it."
)
UNHELD_NOTE = (
    "No open document links this file, so nothing here explains it. Check for a running command "
    "or an open dialog, then close the tab by hand."
)
STALE_FIX = (
    "The cloud has newer versions of {names} than this session loaded, but Fusion does not flag "
    "them: isOutOfDate stays False until the parent is reopened. Save or discard the parent's "
    "edits, close and reopen it, then run update_references again."
)
NO_CHANGE_FIX = (
    "getLatestVersion() changed nothing for {names}. Close and reopen the parent, then run "
    "update_references again."
)
SAVE_FIX = "{document} is modified: save it (save_document) or the update is lost when it closes."
READ_ONLY_NOTE = "{document} is read-only: a save will not stick."
NON_ACTIVE_NOTE = (
    "{document} is not the active document, and reads from a non-active document can be partial. "
    "Activate it for a reliable answer."
)
INDIRECT_NOTE = (
    "{count} more reference(s) sit inside sub-assemblies and are not listed. Run this on those "
    "documents to check them."
)
LOOKUP_BUDGET_NOTE = (
    "The cloud lookup budget ran out after {seconds:g} s: {count} file(s) use the possibly stale "
    "latest version from the open document, with no path."
)


def _selector(arguments):
    """Return (field, value) for the selector given, or None when there is none."""
    given = {key: arguments.get(key) for key in ("name", "data_file_id") if arguments.get(key) is not None}
    if len(given) > 1:
        raise ValueError("provide only one of 'name' or 'data_file_id'")
    if not given:
        return None
    field, value = next(iter(given.items()))
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string")
    return field, value


def validate_close(arguments):
    """Return (field, value, all_instances, discard_unsaved); raise ValueError if invalid."""
    reject_unknown(arguments, ("name", "data_file_id", "all_instances", "discard_unsaved"))
    selector = _selector(arguments)
    if selector is None:
        raise ValueError("provide one of 'name' or 'data_file_id'")
    return (*selector, bool_arg(arguments, "all_instances"), bool_arg(arguments, "discard_unsaved"))


def validate_references(arguments):
    """Return (selector or None, apply, files or None); raise ValueError if invalid."""
    reject_unknown(arguments, ("name", "data_file_id", "apply", "files"))
    selector = _selector(arguments)
    files = arguments.get("files")
    if files is not None:
        if not isinstance(files, list) or not files:
            raise ValueError("'files' must be a non-empty array of file names or DataFile ids")
        if not all(isinstance(item, str) and item.strip() for item in files):
            raise ValueError("every entry in 'files' must be a non-empty string")
    return selector, bool_arg(arguments, "apply"), files


# -- close_document: decisions ------------------------------------------------


def _file_id(info):
    return info.get("data_file_id")


def _same_selector(info, field, value):
    if field == "data_file_id":
        return _file_id(info) == value
    return value in (info.get("name"), info.get("file_name"))


def _candidate(info):
    return {"name": info.get("name"), "data_file_id": _file_id(info), "version": info.get("version")}


def pick_targets(infos, field, value, all_instances=False):
    """Indexes to close, or (None, error payload).

    ``all_instances`` reaches every instance of ONE file. A name shared by two
    different files stays ambiguous, because closing the wrong one loses a tab
    nobody asked to lose.
    """
    matches = [index for index, info in enumerate(infos) if _same_selector(info, field, value)]
    if not matches:
        return None, {"error": f"No open document matches {field}={value!r}",
                      "open_documents": [_candidate(info) for info in infos]}
    if len(matches) == 1:
        return matches, None
    candidates = [_candidate(infos[index]) for index in matches]
    ids = [_file_id(infos[index]) for index in matches]
    if None in ids or len(set(ids)) > 1:
        return None, {"error": f"{len(matches)} different files match {field}={value!r}; use data_file_id",
                      "candidates": candidates}
    if not all_instances:
        return None, {"error": f"{len(matches)} open instances match {field}={value!r}; "
                               "pass all_instances: true to close them all",
                      "candidates": candidates}
    return matches, None


def unsaved_reason(info):
    """Why closing would lose work, or None. An unreadable state counts as unsaved."""
    if info.get("is_saved") is False:
        return "never_saved"
    if info.get("is_modified") is True:
        return "modified"
    if info.get("is_modified") is not False or info.get("is_saved") is not True:
        return "state_unreadable"
    return None


def close_preflight(infos, targets):
    """Unsaved work a close would lose: the targets, and open documents they link.

    A linked document is listed even when another parent might keep it open: which
    instance survives is not knowable in advance, and the cost of asking is one flag.
    """
    linked_ids = set().union(*(infos[index].get("references", ()) for index in targets))
    blockers = []
    for index, info in enumerate(infos):
        role = "target" if index in targets else "linked" if _file_id(info) in linked_ids else None
        reason = unsaved_reason(info) if role else None
        if reason:
            blockers.append({"name": info.get("name"), "role": role, "reason": reason})
    return blockers


def instance_key(info):
    """What identifies an instance across two reads (wrappers are not stable)."""
    if _file_id(info):
        return (_file_id(info), info.get("version"))
    return ("unsaved", info.get("name"))


def reconcile(before, after_keys, targets):
    """Split the earlier documents into (closed, still_open, also_closed) indexes.

    Identical instances cannot be told apart, so this counts: bystanders claim the
    survivors first, which charges a disappearance to the document that was asked
    to close rather than inventing a side effect.
    """
    remaining = Counter(after_keys)
    also_closed, closed, still_open = [], [], []
    target_set = set(targets)
    for index, info in enumerate(before):
        if index in target_set:
            continue
        key = instance_key(info)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            also_closed.append(index)
    for index in targets:
        key = instance_key(before[index])
        if remaining[key] > 0:
            remaining[key] -= 1
            still_open.append(index)
        else:
            closed.append(index)
    return closed, still_open, also_closed


class CloseWatch:
    """Decides when ``app.documents`` has settled after the close attempts.

    Only reads happen while it runs: the attempts are already over, so a held
    instance costs one timeout, never another close.
    """

    def __init__(self, before, targets, started, timeout_s=CLOSE_VERIFY_TIMEOUT_S, quiet_s=CLOSE_QUIET_S):
        self.before = before
        self.targets = targets
        self.deadline = started + timeout_s
        self.quiet_s = quiet_s
        self.last_keys = None
        self.last_change = started

    def observe(self, now, keys):
        """Return True once the targets are gone and the list is quiet, or time is up."""
        current = Counter(keys)
        if current != self.last_keys:
            self.last_keys = current
            self.last_change = now
        if now >= self.deadline:
            return True
        _, still_open, _ = reconcile(self.before, keys, self.targets)
        return not still_open and now - self.last_change >= self.quiet_s


def holders_of(infos, file_id):
    """Open documents linking *file_id*, leaving out instances of the file itself."""
    if not file_id:
        return []
    return [index for index, info in enumerate(infos)
            if _file_id(info) != file_id and file_id in info.get("references", ())]


def top_parents(infos, holders):
    """The holders' own top-level parents: a held parent cannot be closed either."""
    seen, pending, tops = set(), list(holders), []
    while pending:
        index = pending.pop()
        if index in seen:
            continue
        seen.add(index)
        above = holders_of(infos, _file_id(infos[index]))
        if above:
            pending.extend(above)
        else:
            tops.append(index)
    return sorted(tops or holders)


def describe_hold(infos, file_id):
    """Who holds a file open and how to free it, from a read taken after the close."""
    holders = holders_of(infos, file_id)
    if not holders:
        return {"held_by": [], "how_to_free": UNHELD_NOTE}
    names = [infos[index].get("name") for index in top_parents(infos, holders)]
    return {
        "held_by": [{"name": infos[index].get("name"), "data_file_id": _file_id(infos[index]),
                     "is_modified": infos[index].get("is_modified")} for index in holders],
        "how_to_free": HELD_FIX.format(parents=", ".join(repr(name) for name in names)),
    }


def active_warning(before, gone, active_name):
    """Warn when the active document went, naming what is active now."""
    name = next((before[index].get("name") for index in gone if before[index].get("is_active")), None)
    if name is None:
        return None
    now = f"{active_name!r} is active now" if active_name else "no document is active now"
    return f"{name!r} was the active document; {now}."


def close_result(before, after, targets, attempts, active_name):
    """Build the close_document report from the reads taken either side of it."""
    closed, still_open, also_closed = reconcile(before, [instance_key(info) for info in after], targets)

    def entry(index):
        return _candidate(before[index])

    gone = closed + also_closed
    if not still_open:
        status = "closed"
    else:
        status = "partial" if closed else "not_closed"
    warning = active_warning(before, gone, active_name)
    return {
        "status": status,
        "closed": [entry(index) for index in closed],
        "still_open": [{**entry(index), **describe_hold(after, _file_id(before[index]))} for index in still_open],
        "also_closed": [entry(index) for index in also_closed],
        "discarded_changes": [before[index].get("name") for index in gone if unsaved_reason(before[index])],
        "attempts": attempts,
        "active_document": active_name,
        "warnings": [warning] if warning else [],
    }


# -- update_references: decisions ---------------------------------------------


def classify_reference(is_out_of_date, loaded, latest):
    """Compare what Fusion says with what the version numbers say."""
    if is_out_of_date is True:
        return OUT_OF_DATE
    if loaded is None or latest is None:
        return UNKNOWN
    return STALE_NOT_FLAGGED if latest > loaded else CURRENT


def resolve_files(references, files):
    """Reference indexes chosen by *files* (all when None), or (None, error payload)."""
    if files is None:
        return list(range(len(references))), None
    selected = set()
    for wanted in files:
        hits = [index for index, ref in enumerate(references) if ref.get("data_file_id") == wanted]
        if not hits:
            hits = [index for index, ref in enumerate(references) if ref.get("name") == wanted]
        known = [{"name": ref.get("name"), "data_file_id": ref.get("data_file_id")} for ref in references]
        if not hits:
            return None, {"error": f"No reference matches {wanted!r}; nothing was changed", "references": known}
        if len({references[index].get("data_file_id") for index in hits}) > 1:
            return None, {"error": f"{wanted!r} names several different files; use its data_file_id. "
                                   "Nothing was changed", "references": [known[index] for index in hits]}
        selected.update(hits)
    return sorted(selected), None


def plan_updates(references, selected):
    """Split chosen references into (to update, skipped).

    The call marks the parent modified, so a reference already at the latest
    version is left alone. A stale unflagged one still gets its one attempt: the
    version check afterwards says whether it did anything.
    """
    to_update, skipped = [], []
    for index in selected:
        if references[index].get("status") == CURRENT:
            skipped.append({"name": references[index].get("name"), "reason": "already_latest"})
        else:
            to_update.append(index)
    return to_update, skipped


def judge_update(before, versions_after, returned, error=None):
    """Decide what a getLatestVersion() call did. The version number outranks ``returned``."""
    if before is not None and versions_after and before not in versions_after:
        return UPDATED
    if error is not None or returned is False:
        return FAILED
    if before is None or not versions_after:
        return UNVERIFIED
    return NO_CHANGE


def names_with_status(references, status):
    return [ref.get("name") for ref in references if ref.get("status") == status]


def reference_advice(document, references, updates, document_modified):
    """What to do next, in order. Empty when there is nothing to do."""
    advice = []
    if updates and document_modified:
        advice.append(SAVE_FIX.format(document=repr(document)))
    stale = names_with_status(references, STALE_NOT_FLAGGED)
    stuck = [item["name"] for item in updates
             if item["status"] in (NO_CHANGE, FAILED) and item["name"] not in stale]
    if stale:
        advice.append(STALE_FIX.format(names=", ".join(repr(name) for name in stale)))
    if stuck:
        advice.append(NO_CHANGE_FIX.format(names=", ".join(repr(name) for name in stuck)))
    return advice


# -- Fusion reads -------------------------------------------------------------


def _reference_ids(document):
    """DataFile ids this document links, directly or through sub-assemblies."""
    ids = set()
    for attr in ("documentReferences", "allDocumentReferences"):
        try:
            refs = strict_items(safe(document, attr))
        except Exception:
            continue
        for ref in refs:
            file_id = safe(safe(ref, "dataFile"), "id")
            if file_id:
                ids.add(file_id)
    return ids


def _describe(document):
    data_file = safe(document, "dataFile")
    return {
        "name": safe(document, "name"),
        "data_file_id": safe(data_file, "id"),
        "file_name": safe(data_file, "name"),
        "version": safe(data_file, "versionNumber"),
        "is_active": safe(document, "isActive") is True,
        "is_modified": safe(document, "isModified"),
        "is_saved": safe(document, "isSaved"),
    }


def _read_documents(app):
    """Return (documents, infos) from a fresh read of ``app.documents``."""
    documents = open_documents(app)
    return documents, [_describe(document) for document in documents]


def _read_with_references(app):
    documents, infos = _read_documents(app)
    for document, info in zip(documents, infos):
        info["references"] = _reference_ids(document)
    return documents, infos


def _attempt_close(document, info):
    """The one close attempt for this instance. Always False: True prompts the user."""
    attempt = {"name": info.get("name")}
    if safe(document, "isValid") is False:
        attempt["skipped"] = "already_gone"
        return attempt
    try:
        attempt["returned"] = document.close(False)
    except Exception as exc:
        attempt["error"] = f"{type(exc).__name__}: {exc}"
    log(f"[MCP] close_document {info.get('name')!r}: {attempt}")
    return attempt


def _wait_for_close(app, before, targets):
    started = time.monotonic()
    watch = CloseWatch(before, targets, started)
    while True:
        pump()
        _, infos = _read_documents(app)
        if watch.observe(time.monotonic(), [instance_key(info) for info in infos]):
            return
        time.sleep(_POLL_S)


def close_document(arguments):
    """Close a document or all instances of one file and verify what went."""
    try:
        field, value, all_instances, discard_unsaved = validate_close(arguments)
    except ValueError as exc:
        return tool_result(f"Error: {exc}", True)

    try:
        app = get_app()
        documents, before = _read_with_references(app)
        targets, error = pick_targets(before, field, value, all_instances)
        if error:
            return tool_result(error, True)
        blockers = close_preflight(before, targets)
        if blockers and not discard_unsaved:
            return tool_result({"status": "refused", "attempted": False, "reason": "unsaved_changes",
                          "unsaved": blockers, "recommendation": UNSAVED_FIX}, True)

        attempts = []
        for index in targets:
            attempts.append(_attempt_close(documents[index], before[index]))
            pump()
        _wait_for_close(app, before, targets)

        _, after = _read_with_references(app)
        active_name = safe(safe(app, "activeDocument"), "name")
        result = close_result(before, after, targets, attempts, active_name)
        return tool_result(result, bool(result["still_open"]))
    except Exception as exc:
        log(f"[MCP] close_document failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)


def _folder_path(data_file):
    """Folder path from ``parentFolder`` links: "/" for the project root, None if unreadable."""
    folder = safe(data_file, "parentFolder")
    if folder is None:
        return None
    names = []
    for _ in range(_MAX_FOLDER_DEPTH):
        if folder is None or safe(folder, "isRoot") is True:
            break
        names.append(safe(folder, "name") or "?")
        folder = safe(folder, "parentFolder")
    return "/" + "/".join(reversed(names))


class CloudLookups:
    """Fresh latest version and path per file, once each, within a time budget.

    ``dataFile.latestVersionNumber`` on a loaded reference may be as old as the
    load, so ask ``findFileById``; save_document does the same.
    """

    def __init__(self, app, budget_s=LOOKUP_BUDGET_S):
        self.app = app
        self.deadline = time.monotonic() + budget_s
        self.cache = {}
        self.skipped = set()

    def lookup(self, file_id):
        """Return (latest version, path), both None once the budget is spent."""
        if file_id in self.cache:
            return self.cache[file_id]
        if time.monotonic() >= self.deadline:
            self.skipped.add(file_id)
            return None, None
        try:
            data_file = self.app.data.findFileById(file_id)
        except Exception:
            data_file = None
        folder = _folder_path(data_file) if data_file is not None else None
        path = None if folder is None else f"{folder.rstrip('/')}/{safe(data_file, 'name')}"
        self.cache[file_id] = (int_or_none(safe(data_file, "latestVersionNumber")), path)
        return self.cache[file_id]


def read_reference(ref, cloud):
    data_file = safe(ref, "dataFile")
    file_id = safe(data_file, "id")
    file_version = int_or_none(safe(data_file, "versionNumber"))
    ref_version = int_or_none(safe(ref, "version"))
    loaded = file_version if ref_version is None else ref_version
    cloud_latest, path = cloud.lookup(file_id) if file_id else (None, None)
    known = [value for value in (cloud_latest, int_or_none(safe(data_file, "latestVersionNumber"))) if value is not None]
    latest = max(known) if known else None
    is_out_of_date = safe(ref, "isOutOfDate")
    entry = {
        "name": safe(data_file, "name"),
        "data_file_id": file_id,
        "path": path,
        "loaded_version": loaded,
        "latest_version": latest,
        "is_out_of_date": is_out_of_date if isinstance(is_out_of_date, bool) else None,
        "status": classify_reference(is_out_of_date, loaded, latest),
    }
    if file_version is not None and file_version != loaded:
        entry["data_file_version"] = file_version
    return entry


def direct_references(document):
    return strict_items(safe(document, "documentReferences"))


def _pick_document(app, selector):
    """Return (document, None), or (None, error payload) for a bad selection."""
    if selector is None:
        document = safe(app, "activeDocument")
        return (document, None) if document is not None else (None, {"error": "No document is active"})
    documents, infos = _read_documents(app)
    matches = [index for index, info in enumerate(infos) if _same_selector(info, *selector)]
    if len(matches) == 1:
        return documents[matches[0]], None
    field, value = selector
    if not matches:
        return None, {"error": f"No open document matches {field}={value!r}",
                      "open_documents": [_candidate(info) for info in infos]}
    return None, {"error": f"{len(matches)} open documents match {field}={value!r}; "
                           "activate the one you mean and call this with no selector",
                  "candidates": [_candidate(infos[index]) for index in matches]}


def _loaded_versions(document):
    """Loaded versions per DataFile id, from a fresh read of the references."""
    versions = {}
    for ref in direct_references(document):
        file_id = safe(safe(ref, "dataFile"), "id")
        version = int_or_none(safe(ref, "version"))
        if version is None:
            version = int_or_none(safe(safe(ref, "dataFile"), "versionNumber"))
        if file_id and version is not None:
            versions.setdefault(file_id, []).append(version)
    return versions


def _fresh_reference(document, entry):
    """Re-find a reference: an earlier update may have invalidated what was held."""
    for ref in direct_references(document):
        data_file = safe(ref, "dataFile")
        if safe(data_file, "id") != entry.get("data_file_id"):
            continue
        version = int_or_none(safe(ref, "version"))
        if version is None or version == entry.get("loaded_version"):
            return ref
    return None


def _attempt_update(ref, entry):
    """The one getLatestVersion() call for this reference."""
    attempt = {"returned": None, "error": None}
    try:
        attempt["returned"] = ref.getLatestVersion()
    except Exception as exc:
        attempt["error"] = f"{type(exc).__name__}: {exc}"
    log(f"[MCP] update_references {entry.get('name')!r}: {attempt}")
    return attempt


def _moved(entry, versions):
    after = versions.get(entry.get("data_file_id"))
    return bool(after) and entry.get("loaded_version") not in after


def _wait_for_versions(document, entries):
    """Pump until every reference's version moved, or time is up; return the last read."""
    deadline = time.monotonic() + UPDATE_VERIFY_TIMEOUT_S
    while True:
        pump()
        versions = _loaded_versions(document)
        if all(_moved(entry, versions) for entry in entries) or time.monotonic() >= deadline:
            return versions
        time.sleep(_POLL_S)


def _apply_updates(document, entries, held):
    """One getLatestVersion() per reference, then judge each by its version number."""
    started = time.monotonic()
    attempts = []
    for entry, ref in zip(entries, held):
        if time.monotonic() - started >= APPLY_BUDGET_S:
            attempts.append(None)
            continue
        attempts.append(_attempt_update(_fresh_reference(document, entry) or ref, entry))
        pump()

    tried = [entry for entry, attempt in zip(entries, attempts) if attempt is not None]
    versions = _wait_for_versions(document, tried) if tried else {}
    updates = []
    for entry, attempt in zip(entries, attempts):
        item = {"name": entry.get("name"), "data_file_id": entry.get("data_file_id"),
                "version_before": entry.get("loaded_version")}
        if attempt is None:
            updates.append({**item, "status": NOT_ATTEMPTED, "reason": "time_budget"})
            continue
        after = versions.get(entry.get("data_file_id"), [])
        item["version_after"] = after[0] if len(after) == 1 else after or None
        item["returned"] = attempt["returned"]
        if attempt["error"]:
            item["error"] = attempt["error"]
        item["status"] = judge_update(entry.get("loaded_version"), after, attempt["returned"], attempt["error"])
        updates.append(item)
    return updates


def indirect_count(document, direct_count):
    try:
        return max(0, len(strict_items(safe(document, "allDocumentReferences"))) - direct_count)
    except Exception:
        return 0


def _reference_warnings(document, name, direct_count, cloud):
    warnings = []
    if safe(document, "isActive") is False:
        warnings.append(NON_ACTIVE_NOTE.format(document=repr(name)))
    if safe(safe(document, "dataFile"), "isReadOnly") is True:
        warnings.append(READ_ONLY_NOTE.format(document=repr(name)))
    indirect = indirect_count(document, direct_count)
    if indirect:
        warnings.append(INDIRECT_NOTE.format(count=indirect))
    if cloud.skipped:
        warnings.append(LOOKUP_BUDGET_NOTE.format(seconds=LOOKUP_BUDGET_S, count=len(cloud.skipped)))
    return warnings


def update_references(arguments):
    """List a document's references; with apply, get the latest versions and verify."""
    try:
        selector, apply_updates, files = validate_references(arguments)
    except ValueError as exc:
        return tool_result(f"Error: {exc}", True)

    try:
        app = get_app()
        document, error = _pick_document(app, selector)
        if error:
            return tool_result(error, True)

        name = safe(document, "name")
        cloud = CloudLookups(app)
        held = direct_references(document)
        references = [read_reference(ref, cloud) for ref in held]
        selected, error = resolve_files(references, files)
        if error:
            return tool_result(error, True)
        to_update, skipped = plan_updates(references, selected)

        updates = []
        if apply_updates:
            updates = _apply_updates(document, [references[i] for i in to_update], [held[i] for i in to_update])
            references = [read_reference(ref, cloud) for ref in direct_references(document)]

        document_modified = safe(document, "isModified")
        result = {
            "document": name,
            "data_file_id": safe(safe(document, "dataFile"), "id"),
            "document_modified": document_modified,
            "is_up_to_date": safe(document, "isUpToDate"),
            "applied": apply_updates,
            "references": references,
            "out_of_date": names_with_status(references, OUT_OF_DATE),
            "stale_not_flagged": names_with_status(references, STALE_NOT_FLAGGED),
        }
        if apply_updates:
            result["updates"] = updates
            result["skipped"] = skipped
        else:
            result["would_update"] = [references[i].get("name") for i in to_update]
        result["warnings"] = _reference_warnings(document, name, len(references), cloud)
        result["recommendation"] = reference_advice(name, references, updates, document_modified is True)
        return tool_result(result, any(item["status"] in (NO_CHANGE, FAILED, NOT_ATTEMPTED) for item in updates))
    except Exception as exc:
        log(f"[MCP] update_references failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)
