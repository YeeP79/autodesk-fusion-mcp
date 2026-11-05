"""List open documents and switch the active one.

Reads from a non-active document return partial data, so tools that inspect a
design need the right document active first. ``document_status`` shows what is
open (and which documents will not save); ``activate_document`` switches.
"""

import traceback

from . import document_guard
from .common.api import safe
from .common.documents import open_documents
from .common.results import tool_result
from .dispatch import get_app, log

_MAX_FOLDER_DEPTH = 64

READ_ONLY_WARNING = "active document is read-only: saves will not stick"
UNSAVED_WARNING = "active document has unsaved changes"
NO_ACTIVE_WARNING = "no document is active"
DUPLICATE_WARNING = (
    "{count} open instances of {name!r} (DataFile {file_id}, loaded versions {versions}): "
    "saving a non-primary instance can silently do nothing (save returns True, no upload; "
    "its window title has a '(1)' suffix). Close the extra instance(s) without saving and "
    "reopen the file fresh."
)
LINKED_PARENT_NOTE = (
    " Open documents that link it ({parents}) load their own instances, so closing the "
    "duplicate may need those parents closed too."
)


def folder_path(data_file):
    """Build "/Folder/Sub" from ``parentFolder`` links; "/" for the project root.

    The root folder's own name is left out (the project name is reported
    separately). Returns None when the parent folder cannot be read.
    """
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


def _product_types(document):
    products = safe(document, "products")
    if products is None:
        return []
    types = []
    try:
        for i in range(products.count):
            product_type = safe(products.item(i), "productType")
            if product_type:
                types.append(product_type)
    except Exception:
        pass
    return types


def referenced_ids(document):
    """DataFile ids this document directly references (empty if unreadable)."""
    ids = set()
    refs = safe(document, "documentReferences")
    if refs is None:
        return ids
    try:
        for i in range(refs.count):
            ref_file = safe(refs.item(i), "dataFile")
            ref_id = safe(ref_file, "id") if ref_file is not None else None
            if ref_id:
                ids.add(ref_id)
    except Exception:
        pass
    return ids


def describe_document(document):
    """Summarise one open document as a JSON-safe dict."""
    info = {
        "name": safe(document, "name"),
        "is_active": safe(document, "isActive") is True,
        "document_type": safe(document, "objectType"),
        "product_types": _product_types(document),
        "is_modified": safe(document, "isModified"),
        "is_saved": safe(document, "isSaved"),
        "data_file": None,
    }
    data_file = safe(document, "dataFile")
    if data_file is not None:
        project = safe(data_file, "parentProject")
        read_only = document_guard.read_only_state(document)
        info["data_file"] = {
            "id": safe(data_file, "id"),
            "name": safe(data_file, "name"),
            "version_number": safe(data_file, "versionNumber"),
            "is_read_only": "unknown" if read_only is None else read_only,
            "project": safe(project, "name") if project is not None else None,
            "folder_path": folder_path(data_file),
        }
    return info


def attach_references(infos, referenced_id_sets):
    """Add ``referenced_by`` (names of open docs that link each one) in place."""
    for info in infos:
        file_id = (info.get("data_file") or {}).get("id")
        info["referenced_by"] = [
            other["name"]
            for other, ids in zip(infos, referenced_id_sets)
            if other is not info and file_id and file_id in ids
        ]


def flag_duplicates(infos):
    """Mark documents sharing a DataFile id and return one warning per shared file.

    Fusion can hold several in-memory instances of one file (opening it twice, or
    opening a parent that links it). Each instance gets ``duplicate_instances`` and
    ``loaded_versions`` (every instance's version, in document order).
    """
    groups = {}
    for info in infos:
        file_id = (info.get("data_file") or {}).get("id")
        if file_id:
            groups.setdefault(file_id, []).append(info)
    warnings = []
    for file_id, group in groups.items():
        if len(group) < 2:
            continue
        versions = [info["data_file"].get("version_number") for info in group]
        for info in group:
            info["duplicate_instances"] = len(group)
            info["loaded_versions"] = versions
        names = {info["name"] for info in group}
        parents = sorted({parent for info in group for parent in info.get("referenced_by", ())} - names)
        warning = DUPLICATE_WARNING.format(
            count=len(group), name=group[0]["data_file"].get("name") or group[0]["name"],
            file_id=file_id, versions=versions)
        if parents:
            warning += LINKED_PARENT_NOTE.format(parents=", ".join(parents))
        warnings.append(warning)
    return warnings


def status_warnings(infos):
    """Plain-language warnings about the active document."""
    active = next((info for info in infos if info.get("is_active")), None)
    if active is None:
        return [NO_ACTIVE_WARNING]
    warnings = []
    if (active.get("data_file") or {}).get("is_read_only") is True:
        warnings.append(READ_ONLY_WARNING)
    if active.get("is_modified") is True:
        warnings.append(UNSAVED_WARNING)
    return warnings


def document_status(arguments):
    """Report every open document, highlighting the active one."""
    del arguments
    try:
        docs = open_documents(get_app())
        infos = [describe_document(doc) for doc in docs]
        attach_references(infos, [referenced_ids(doc) for doc in docs])
        duplicate_warnings = flag_duplicates(infos)
        active = next((info for info in infos if info["is_active"]), None)
        return tool_result({
            "active_document": active["name"] if active else None,
            "count": len(infos),
            "documents": infos,
            "warnings": status_warnings(infos) + duplicate_warnings,
        })
    except Exception as exc:
        log(f"document_status failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)


def validate_selector(arguments):
    """Return (field, value) for the single selector given, or an error string."""
    given = {key: arguments.get(key) for key in ("name", "data_file_id") if arguments.get(key) is not None}
    if len(given) != 1:
        return None, "Error: provide exactly one of 'name' or 'data_file_id'"
    field, value = next(iter(given.items()))
    if not isinstance(value, str) or not value.strip():
        return None, f"Error: '{field}' must be a non-empty string"
    return (field, value), None


def match_documents(infos, field, value):
    """Indexes of documents matching the selector.

    A name matches either the document name (which carries the version suffix,
    e.g. "Frame v3") or its DataFile name ("Frame").
    """
    matches = []
    for index, info in enumerate(infos):
        data_file = info.get("data_file") or {}
        if field == "data_file_id":
            hit = data_file.get("id") == value
        else:
            hit = value in (info.get("name"), data_file.get("name"))
        if hit:
            matches.append(index)
    return matches


def _candidate(info):
    data_file = info.get("data_file") or {}
    return {
        "name": info.get("name"),
        "data_file_id": data_file.get("id"),
        "project": data_file.get("project"),
        "folder_path": data_file.get("folder_path"),
    }


def activate_document(arguments):
    """Activate one open document chosen by name or DataFile id."""
    selector, error = validate_selector(arguments)
    if error:
        return tool_result(error, True)
    field, value = selector
    try:
        app = get_app()
        docs = open_documents(app)
        infos = [describe_document(doc) for doc in docs]
        matches = match_documents(infos, field, value)
        if not matches:
            return tool_result({
                "error": f"No open document matches {field}={value!r}",
                "open_documents": [_candidate(info) for info in infos],
            }, True)
        if len(matches) > 1:
            hint = ("they are duplicate instances of one file (see document_status warnings)"
                    if field == "data_file_id" else "use data_file_id")
            return tool_result({
                "error": f"{len(matches)} open documents match {field}={value!r}; {hint}",
                "candidates": [_candidate(infos[i]) for i in matches],
            }, True)

        target = docs[matches[0]]
        was_active = infos[matches[0]]["is_active"]
        if not was_active and not target.activate():
            return tool_result(f"Error: Fusion refused to activate {infos[matches[0]]['name']!r}", True)

        active_doc = app.activeDocument
        active = describe_document(active_doc) if active_doc is not None else None
        if active is not None:
            active["is_active"] = True  # it is app.activeDocument, whatever isActive says
        result = {
            "already_active": was_active,
            "active_document": active,
            "warnings": status_warnings([active] if active else []),
        }
        if active is None or active.get("name") != infos[matches[0]]["name"]:
            return tool_result({"error": "Activation did not take effect", **result}, True)
        return tool_result(result)
    except Exception as exc:
        log(f"activate_document failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)
