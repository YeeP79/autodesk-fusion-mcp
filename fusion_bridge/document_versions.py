"""Version history and restore for the active document's cloud DataFile.

``DataFile.versions`` lists the other versions of a file and
``DataFile.promote()`` copies an older version forward as a new latest
version. The open document keeps showing what it loaded, so restore closes
it without saving and reopens the file. Closing a document also closes the
documents it links, which invalidates held ``Document`` objects; only
strings are kept across the close and everything is re-queried after it.

Decision logic (argument validation, refusal messages, list formatting) is
kept in plain functions over dicts so it is testable without Fusion.
"""

import traceback

from .common.api import attempt
from .common.args import UNKNOWN_CAPITALIZED, is_int, reject_unknown, require_object
from .common.cloud import iso_utc
from .common.results import error_result, ok_result
from .dispatch import get_app, log

DEFAULT_LIMIT = 20
MAX_LIMIT = 500


# -- Pure logic ---------------------------------------------------------------


def validate_list_args(arguments):
    """Return the version limit, or raise ValueError."""
    require_object(arguments)
    reject_unknown(arguments, {"limit", "description"}, UNKNOWN_CAPITALIZED)
    limit = arguments.get("limit", DEFAULT_LIMIT)
    if not is_int(limit) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"'limit' must be an integer between 1 and {MAX_LIMIT}")
    return limit


def validate_restore_args(arguments):
    """Return (version_number, confirm, discard_unsaved), or raise ValueError."""
    require_object(arguments)
    reject_unknown(arguments, {"version_number", "confirm", "discard_unsaved", "description"}, UNKNOWN_CAPITALIZED)
    if "version_number" not in arguments:
        raise ValueError("'version_number' is required")
    number = arguments["version_number"]
    if not is_int(number) or number < 1:
        raise ValueError("'version_number' must be a positive integer")
    confirm = arguments.get("confirm", False)
    if type(confirm) is not bool:
        raise ValueError("'confirm' must be boolean")
    discard = arguments.get("discard_unsaved", False)
    if type(discard) is not bool:
        raise ValueError("'discard_unsaved' must be boolean")
    return number, confirm, discard


def format_version_list(document_name, records, loaded_number, latest_number, limit):
    """Build the document_versions payload from plain version records.

    ``records`` are dicts with version_number, date_created, description,
    milestone and created_by. Duplicates (by version_number) are dropped and
    the list is sorted newest first, then cut to ``limit``.
    """
    unique = {}
    for record in records:
        number = record.get("version_number")
        if is_int(number) and number not in unique:
            unique[number] = record
    ordered = sorted(unique.values(), key=lambda r: r["version_number"], reverse=True)
    versions = []
    for record in ordered[:limit]:
        entry = {
            "version_number": record["version_number"],
            "date_created": record.get("date_created"),
            "is_current": record["version_number"] == loaded_number,
            "is_latest": record["version_number"] == latest_number,
        }
        for key in ("description", "milestone", "created_by"):
            if record.get(key):
                entry[key] = record[key]
        versions.append(entry)
    payload = {
        "document": document_name,
        "loaded_version": loaded_number,
        "latest_version": latest_number,
        "total_versions": len(ordered),
        "versions": versions,
    }
    if len(ordered) > limit:
        payload["truncated"] = True
        payload["note"] = f"Showing the newest {limit} of {len(ordered)} versions; raise 'limit' to see more."
    if loaded_number is not None and latest_number is not None and loaded_number != latest_number:
        payload.setdefault("warnings", []).append(
            f"The open document is version {loaded_number}, not the latest ({latest_number})."
        )
    return payload


def plan_restore(state, version_number, confirm, discard_unsaved):
    """Decide whether restore may proceed.

    ``state`` is a dict: document_name, version_numbers (iterable of ints),
    loaded_version, latest_version, is_modified, is_read_only and
    other_modified (names of other open documents with unsaved changes).

    Returns ``(refusal_text_or_None, plan_dict)``. ``plan_dict`` describes
    what restore will do and is included in the refusal for confirm=false.
    """
    name = state.get("document_name") or "the active document"
    numbers = sorted(set(state.get("version_numbers") or ()), reverse=True)
    latest = state.get("latest_version")
    loaded = state.get("loaded_version")
    other_modified = list(state.get("other_modified") or ())

    if version_number not in numbers:
        shown = ", ".join(str(n) for n in numbers[:20]) or "none"
        return f"Version {version_number} does not exist for '{name}'. Available versions: {shown}.", None
    if version_number == latest:
        return (
            f"Version {version_number} is already the latest version of '{name}'; there is nothing to restore."
            + (f" The open document is version {loaded}; reopen the file to load the latest." if loaded != latest else ""),
            None,
        )
    if state.get("is_read_only"):
        return (
            f"'{name}' is read-only for this account (for example, a Personal Use file not designated editable, "
            "or someone else is editing it). Fusion cannot create a new version, so restore was not attempted.",
            None,
        )

    plan = {
        "document": name,
        "restore_version": version_number,
        "current_latest_version": latest,
        "expected_new_version": latest + 1 if is_int(latest) else None,
        "steps": [
            f"Promote version {version_number} to a new latest version (v{latest + 1 if is_int(latest) else '?'}); "
            "existing versions are kept.",
            f"Close '{name}' without saving"
            + (" (DISCARDING its unsaved changes)" if state.get("is_modified") else "")
            + "; Fusion also closes documents it links.",
            "Reopen the file at the new latest version and make it the active document.",
        ],
    }

    blockers = []
    if state.get("is_modified"):
        blockers.append(f"'{name}' has unsaved changes")
    if other_modified:
        blockers.append(
            "other open documents have unsaved changes and may be closed if linked: " + ", ".join(other_modified)
        )
    if blockers and not discard_unsaved:
        return (
            "Not restored: " + "; ".join(blockers) + ". Save them first, or pass discard_unsaved: true to "
            "discard those changes.",
            plan,
        )
    if not confirm:
        return (
            "Not restored: 'confirm' must be true. Restore would:\n"
            + "\n".join(f"{i}. {step}" for i, step in enumerate(plan["steps"], 1)),
            plan,
        )
    return None, plan


def closed_documents(before_names, after_names):
    """Names open before restore that are no longer open (multiset difference)."""
    remaining = list(after_names)
    closed = []
    for name in before_names:
        if name in remaining:
            remaining.remove(name)
        else:
            closed.append(name)
    return closed


# -- Fusion access ------------------------------------------------------------


def _user_name(user):
    if user is None:
        return None
    return attempt(lambda: user.displayName) or attempt(lambda: user.userName) or None


def _version_record(data_file):
    milestone = None
    if attempt(lambda: data_file.isMilestone, False):
        milestone = attempt(lambda: data_file.milestone.name)
    creator = _user_name(attempt(lambda: data_file.createdBy)) or _user_name(attempt(lambda: data_file.lastUpdatedBy))
    return {
        "version_number": attempt(lambda: data_file.versionNumber),
        "date_created": iso_utc(attempt(lambda: data_file.dateCreated)),
        "description": attempt(lambda: data_file.description) or None,
        "milestone": milestone,
        "created_by": creator,
    }


def _version_files(data_file):
    """All versions of ``data_file`` including itself, keyed by version number.

    ``DataFile.versions`` is documented as "the other versions"; whether it
    includes the file itself is not stated, so the file is added if missing.
    """
    files = {}
    for version in list(data_file.versions) + [data_file]:
        number = attempt(lambda v=version: v.versionNumber)
        if is_int(number) and number not in files:
            files[number] = version
    return files


def _active_data_file(app):
    doc = app.activeDocument
    if doc is None:
        raise ValueError("No active document")
    if not attempt(lambda: doc.isSaved, False):
        raise ValueError(f"'{doc.name}' has never been saved, so it has no version history")
    data_file = doc.dataFile
    if data_file is None:
        raise ValueError(f"'{doc.name}' has no cloud data file")
    return doc, data_file


def document_versions(arguments):
    """List versions of the active document's data file, newest first."""
    try:
        limit = validate_list_args(arguments)
    except ValueError as exc:
        return error_result(f"Error: {exc}")
    try:
        doc, data_file = _active_data_file(get_app())
        files = _version_files(data_file)
        records = [_version_record(f) for f in files.values()]
        payload = format_version_list(
            doc.name, records, data_file.versionNumber, data_file.latestVersionNumber, limit
        )
        return ok_result(payload)
    except ValueError as exc:
        return error_result(f"Error: {exc}")
    except Exception as exc:
        log(f"[MCP] document_versions failed: {exc}")
        return error_result(f"Error reading document versions: {exc}\n{traceback.format_exc()}")


def _open_document_names(app, visible_only=True):
    names = []
    for document in app.documents:
        if visible_only and not attempt(lambda d=document: d.isVisible, True):
            continue
        names.append(attempt(lambda d=document: d.name, "?"))
    return names


def restore_version(arguments):
    """Promote an older version, then close and reopen the active document."""
    try:
        version_number, confirm, discard_unsaved = validate_restore_args(arguments)
    except ValueError as exc:
        return error_result(f"Error: {exc}")

    promoted = False
    name = None
    try:
        app = get_app()
        doc, data_file = _active_data_file(app)
        name = doc.name
        files = _version_files(data_file)
        other_modified = [
            attempt(lambda d=d: d.name, "?") for d in app.documents
            if not attempt(lambda d=d: d.isActive, False) and attempt(lambda d=d: d.isModified, False)
        ]
        state = {
            "document_name": name,
            "version_numbers": list(files),
            "loaded_version": data_file.versionNumber,
            "latest_version": data_file.latestVersionNumber,
            "is_modified": attempt(lambda: doc.isModified, False),
            "is_read_only": attempt(lambda: data_file.isReadOnly, False),
            "other_modified": other_modified,
        }
        refusal, plan = plan_restore(state, version_number, confirm, discard_unsaved)
        if refusal:
            return error_result(refusal)

        # Keep only strings across the close: Document objects (this one and
        # any it links) become invalid once Fusion closes them.
        # DataFile.id (dm.lineage) is the version-independent id findFileById
        # takes; the fs.file form from versionId fails with "Invalid Version URI".
        file_id = attempt(lambda: data_file.id)
        before = [n for n in _open_document_names(app) if n != name]

        if not files[version_number].promote():
            return error_result(f"Fusion refused to promote version {version_number} of '{name}'. Nothing was changed.")
        promoted = True
        log(f"[MCP] restore_version: promoted v{version_number} of '{name}'")

        if not doc.close(False):
            return error_result(
                f"Version {version_number} was promoted to a new latest version, but Fusion did not close '{name}'. "
                "The open document still shows the old content: close it without saving and reopen it."
            )
        doc = data_file = None
        files = {}

        fresh = app.data.findFileById(file_id) if file_id else None
        if fresh is None:
            return error_result(
                f"Version {version_number} was promoted and '{name}' was closed, but the file could not be found "
                "again to reopen it. Open it from the Data Panel; the latest version is the restored one."
            )
        new_doc = app.documents.open(fresh, True)
        if new_doc is None:
            return error_result(
                f"Version {version_number} was promoted and '{name}' was closed, but Fusion failed to reopen it. "
                "Open it from the Data Panel; the latest version is the restored one."
            )
        new_doc.activate()

        active = app.activeDocument
        active_file = active.dataFile if active is not None else None
        after = _open_document_names(app)
        result = {
            "document": attempt(lambda: active.name),
            "restored_from_version": version_number,
            "previous_latest_version": plan["current_latest_version"],
            "new_version": attempt(lambda: active_file.versionNumber),
            "latest_version": attempt(lambda: active_file.latestVersionNumber),
            "is_active": attempt(lambda: active.name) == attempt(lambda: new_doc.name),
        }
        also_closed = closed_documents(before, [n for n in after if n != result["document"]])
        if also_closed:
            result["also_closed"] = also_closed
        if result["new_version"] != plan["expected_new_version"]:
            result["warning"] = (
                f"Expected the reopened document to be version {plan['expected_new_version']}, "
                f"got {result['new_version']}; check the Data Panel."
            )
        log(f"[MCP] restore_version: '{name}' now at v{result['new_version']}")
        return ok_result(result)
    except ValueError as exc:
        return error_result(f"Error: {exc}")
    except Exception as exc:
        log(f"[MCP] restore_version failed: {exc}")
        prefix = (
            f"Version {version_number} of '{name}' WAS promoted before the failure; the latest version is the "
            "restored one. " if promoted else ""
        )
        return error_result(f"{prefix}Error restoring version: {exc}\n{traceback.format_exc()}")
