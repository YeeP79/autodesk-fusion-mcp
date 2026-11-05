"""Compare two cloud designs by what they contain, not by name or date.

Copies of an assembly drift apart without any sign in the data panel, and
checking by hand means opening each one, reading the numbers and remembering to
close it. This tool reads occurrence count, body count, total volume, component
names and timeline length from each file and reports ``same`` or the
differences. Equal numbers are evidence, not proof: see NOT_PROOF_NOTE.

Rules, all from live behavior:

- One file at a time. Opening files back to back while an assembly is still
  loading did nothing and left Fusion busy, and open() returns before the links
  finish, so every read waits for Fusion to go quiet.
- Reads from a non-active document return partial data, so a file is brought to
  the front first, and the Design comes from ``doc.products`` (the active
  product can be CAM or a drawing). A file that cannot come to the front is a
  failure, never a partial read.
- An open instance is reused, because opening a second one creates the stuck
  "(1)" duplicate.
- Only documents this call opened are closed, and never one holding unsaved
  changes or linking an edited document. close() on an instance held by a parent
  silently does nothing, so closes are re-checked and reported.
- open() blocks and cannot be interrupted. The second file only starts when a
  repeat of the first still fits inside the bridge's 120 s main-thread limit.

Body counts come in two forms. ``body_count`` and ``total_volume`` count every
instance in the assembly; ``unique_*`` count each component once.
"""

import json
import time
import traceback
from collections import Counter

import adsk.core
import adsk.fusion

from .dispatch import get_app, log

ID_PREFIX = "urn:"
DESIGN_PRODUCT = "DesignProductType"
DESIGN_EXTENSIONS = ("f3d", "f3z")
CM3_PER_IN3 = 16.387064
# Copies compute to the same volume far inside this; it only absorbs float noise.
VOLUME_REL_TOLERANCE = 1e-6
MAX_LISTED_NAMES = 100

# The bridge abandons a main-thread call after settings.MCP_MAIN_THREAD_TIMEOUT
# (120 s); the margin covers the last close, the restore and the reply.
TOTAL_BUDGET_S = 100.0
SETTLE_TIMEOUT_S = 25.0
SETTLE_QUIET_S = 1.0
CLOSE_SETTLE_QUIET_S = 0.5
CLOSE_SETTLE_TIMEOUT_S = 3.0
CLOSE_VERIFY_TIMEOUT_S = 2.0
RESTORE_TIMEOUT_S = 3.0
# A doEvents call that takes longer than this was doing real work.
MAX_QUIET_PUMP_S = 0.25
_POLL_S = 0.1
_IDLE_COMMANDS = ("SelectCommand", None, "")
_ARGS = {"file_a", "file_b", "project", "project_b"}
_DESIGN_TYPES = {0: "direct", 1: "parametric"}  # adsk.fusion.DesignTypes

COMPARED_FIELDS = ("design_type", "occurrence_count", "component_count", "body_count",
                   "unique_body_count", "timeline_length")
VOLUME_FIELDS = ("total_volume_cm3", "unique_volume_cm3")

NOT_PROOF_NOTE = (
    "Matching numbers are not proof of identical geometry. They compare counts, total volume, "
    "component names and timeline length, not shapes, positions, features or parameters: two "
    "designs can match here and still differ, for example a hole moved along a face. The root "
    "component is left out of the name comparison because it carries the file's own name."
)


def _text(payload, is_error=False):
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def _safe(obj, attr, default=None):
    """Read an attribute; cloud-backed properties can raise or be missing."""
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _items(collection):
    """Members of a Fusion collection (count/item) or a vector (iteration only)."""
    if collection is None:
        return []
    if hasattr(collection, "item") and hasattr(collection, "count"):
        return [collection.item(i) for i in range(collection.count)]
    return list(collection)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# -- Validation ---------------------------------------------------------------


def _optional_name(arguments, key):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string")
    return value.strip()


def validate_arguments(arguments):
    """Return (file_a, file_b, project_a, project_b), or raise ValueError."""
    unknown = sorted(set(arguments) - _ARGS)
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(unknown)}")
    specs = []
    for key in ("file_a", "file_b"):
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{key}' must be a DataFile id or a path")
        specs.append(value.strip())
    project_a = _optional_name(arguments, "project")
    project_b = _optional_name(arguments, "project_b") or project_a
    for spec, project in zip(specs, (project_a, project_b)):
        if not project and not spec.startswith(ID_PREFIX):
            raise ValueError(f"'project' is required for {spec!r}, a path rather than an id ('{ID_PREFIX}...')")
    if specs[0] == specs[1] and project_a == project_b:
        raise ValueError("'file_a' and 'file_b' are the same file")
    return specs[0], specs[1], project_a, project_b


def path_segments(path):
    """Split "Folder/Sub/File" into non-empty segments; the last is the file."""
    return [part.strip() for part in path.replace("\\", "/").split("/") if part.strip()]


def match_named(objects, name, label):
    """The one object called *name* (or name.ext), ignoring case only if no exact hit.

    Names are not unique in Fusion, so several hits raise LookupError rather
    than picking one.
    """
    rows = []
    for obj in objects:
        own = _safe(obj, "name") or ""
        ext = _safe(obj, "fileExtension")
        rows.append((obj, {own, f"{own}.{ext}"} if ext else {own}))
    hits = ([obj for obj, names in rows if name in names]
            or [obj for obj, names in rows if name.casefold() in {n.casefold() for n in names}])
    if not hits:
        found = sorted(_safe(obj, "name") or "?" for obj, _ in rows)[:50]
        raise LookupError(f"No {label} named {name!r}; found: {found}")
    if len(hits) > 1:
        listed = "; ".join(f"{_safe(o, 'name')} (id {_safe(o, 'id')})" for o in hits)
        raise LookupError(f"{len(hits)} {label}s named {name!r}: {listed}; use the id")
    return hits[0]


# -- Pure logic ---------------------------------------------------------------


def _component_stats(component):
    """(body count, volume in cm^3, bodies whose volume could not be read)."""
    bodies = _items(_safe(component, "bRepBodies"))
    volume, unreadable = 0.0, 0
    for body in bodies:
        value = _safe(body, "volume")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            volume += value
        else:
            unreadable += 1
    return len(bodies), volume, unreadable


def read_design(design):
    """The numbers compared for one design. Duck-typed over adsk.fusion.Design."""
    root = design.rootComponent
    components = _items(design.allComponents)
    occurrences = _items(root.allOccurrences)

    # An assembly repeats components, and body reads are slow. The name is part
    # of the key because a copied file can keep its components' ids.
    stats = {}

    def key_of(component):
        return _safe(component, "id"), _safe(component, "name")

    def stats_of(component):
        key = key_of(component)
        if key not in stats:
            stats[key] = _component_stats(component)
        return stats[key]

    unique = [stats_of(component) for component in components]
    placed = [stats_of(root)] + [stats_of(_safe(occurrence, "component")) for occurrence in occurrences]
    root_key = key_of(root)
    design_type = _DESIGN_TYPES.get(_safe(design, "designType"), "unknown")
    timeline = _safe(design, "timeline") if design_type == "parametric" else None
    return {
        "design_type": design_type,
        "root_component": _safe(root, "name"),
        "occurrence_count": len(occurrences),
        "component_count": len(components),
        "component_names": sorted((_safe(c, "name") or "?") for c in components if key_of(c) != root_key),
        "body_count": sum(row[0] for row in placed),
        "unique_body_count": sum(row[0] for row in unique),
        "total_volume_cm3": sum(row[1] for row in placed),
        "unique_volume_cm3": sum(row[1] for row in unique),
        "unreadable_volumes": sum(row[2] for row in unique),
        "timeline_length": _safe(timeline, "count") if timeline is not None else None,
        "marker_position": _safe(timeline, "markerPosition") if timeline is not None else None,
    }


def _volumes_match(a, b):
    return abs(a - b) <= VOLUME_REL_TOLERANCE * max(abs(a), abs(b))


def compare_facts(a, b):
    """The differences between two read_design results; empty when they match."""
    differences = [{"field": field, "a": a[field], "b": b[field]}
                   for field in COMPARED_FIELDS if a[field] != b[field]]
    differences += [{"field": field, "a": a[field], "b": b[field]}
                    for field in VOLUME_FIELDS if not _volumes_match(a[field], b[field])]
    names_a, names_b = Counter(a["component_names"]), Counter(b["component_names"])
    if names_a != names_b:
        differences.append({"field": "component_names",
                            "only_in_a": sorted((names_a - names_b).elements()),
                            "only_in_b": sorted((names_b - names_a).elements())})
    return differences


def public_facts(facts):
    """*facts* as reported: rounded, with inches, and a bounded name list."""
    names = facts["component_names"]
    report = dict(facts, component_names=names[:MAX_LISTED_NAMES])
    if len(names) > MAX_LISTED_NAMES:
        report["component_names_not_listed"] = len(names) - MAX_LISTED_NAMES
    report["total_volume_in3"] = round(facts["total_volume_cm3"] / CM3_PER_IN3, 3)
    report["unique_volume_in3"] = round(facts["unique_volume_cm3"] / CM3_PER_IN3, 3)
    report["total_volume_cm3"] = round(facts["total_volume_cm3"], 3)
    report["unique_volume_cm3"] = round(facts["unique_volume_cm3"], 3)
    return report


def instances_of(infos, file_id):
    """Indexes of open documents whose DataFile id is *file_id*."""
    return [i for i, info in enumerate(infos) if file_id and info.get("file_id") == file_id]


def pick_instance(infos, file_id):
    """Index of the instance to reuse: active, then visible, then newest; or None."""
    hits = instances_of(infos, file_id)
    if not hits:
        return None
    return max(hits, key=lambda i: (infos[i].get("is_active") is True,
                                    infos[i].get("is_visible") is not False,
                                    infos[i].get("version") or 0))


def instance_warnings(row, infos, index):
    """Why numbers read from a reused instance may not be the saved file's."""
    info = infos[index]
    warnings = []
    if info.get("is_modified") is not False:
        warnings.append(f"{info.get('name')!r} has unsaved changes: this reads the open state, not the saved file.")
    latest = row.get("latest_version")
    if _is_int(latest) and _is_int(info.get("version")) and info["version"] < latest:
        warnings.append(f"The open instance has v{info['version']} loaded; the latest is v{latest}.")
    if len(instances_of(infos, row["id"])) > 1:
        warnings.append(f"Several instances of this file are open; using {info.get('name')!r}.")
    return warnings


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

    Matched by DataFile id, as a name can change with its version suffix;
    unsaved documents have no id and fall back to the name.
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


def fits_budget(elapsed_s, first_file_s, budget_s=TOTAL_BUDGET_S):
    """May the second file start? open() can't be cut off, so assume it costs the same."""
    return elapsed_s + first_file_s <= budget_s


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


# -- Fusion reads -------------------------------------------------------------


def _pump():
    """Run Fusion's event loop once; returns how long that took."""
    started = time.monotonic()
    adsk.doEvents()
    return time.monotonic() - started


def _describe(document):
    data_file = _safe(document, "dataFile")
    return {
        "name": _safe(document, "name"),
        "is_active": _safe(document, "isActive"),
        "is_visible": _safe(document, "isVisible"),
        "is_modified": _safe(document, "isModified"),
        "file_id": _safe(data_file, "id"),
        "version": _safe(data_file, "versionNumber"),
    }


def _snapshot(app):
    """(documents, infos) for everything open, hidden instances included."""
    documents = _items(app.documents)
    return documents, [_describe(document) for document in documents]


def _linked_file_ids(document):
    """File ids *document* links, or None when they could not be read."""
    try:
        return {_safe(_safe(ref, "dataFile"), "id") for ref in _items(document.documentReferences)}
    except Exception:
        return None


def _resolve_project(app, project):
    projects = _items(app.data.dataProjects)
    by_id = [p for p in projects if _safe(p, "id") == project]
    return by_id[0] if by_id else match_named(projects, project, "project")


def _resolve_path(project_obj, path):
    segments = path_segments(path)
    if not segments:
        raise LookupError(f"{path!r} does not name a file")
    folder = project_obj.rootFolder
    for segment in segments[:-1]:
        folder = match_named(_items(folder.dataFolders), segment, "folder")
    return match_named(_items(folder.dataFiles), segments[-1], "file")


def _resolve_row(app, spec, project):
    """One file's row; raises LookupError when it cannot be found."""
    if spec.startswith(ID_PREFIX):
        data_file = app.data.findFileById(spec)
        if data_file is None:
            raise LookupError(f"No file with id {spec!r}")
    else:
        data_file = _resolve_path(_resolve_project(app, project), spec)
    return {
        "file": spec,
        "id": _safe(data_file, "id"),
        "name": _safe(data_file, "name"),
        "extension": _safe(data_file, "fileExtension"),
        "latest_version": _safe(data_file, "latestVersionNumber"),
        "data_file": data_file,
    }


def _resolve_rows(app, specs):
    """Return (rows, problems); nothing is opened."""
    rows, problems = [], []
    for spec, project in specs:
        try:
            row = _resolve_row(app, spec, project)
        except LookupError as exc:
            problems.append(str(exc))
            continue
        except Exception as exc:
            problems.append(f"{spec!r}: {type(exc).__name__}: {exc}")
            continue
        extension = (row["extension"] or "").lower()
        if extension not in DESIGN_EXTENSIONS:
            problems.append(f"{row['name']!r} is a .{extension or '?'} file, not a design.")
        rows.append(row)
    if len(rows) == 2 and rows[0]["id"] == rows[1]["id"]:
        problems.append(f"'file_a' and 'file_b' are the same file ({rows[0]['id']}).")
    return rows, problems


# -- Waiting ------------------------------------------------------------------


def _wait_settled(app, document, deadline, quiet_s=None):
    """Pump until Fusion is quiet (with *document* in front, unless None)."""
    tracker = SettleTracker(SETTLE_QUIET_S if quiet_s is None else quiet_s)
    ui = app.userInterface
    while True:
        pump_s = _pump()
        now = time.monotonic()
        in_front = document is None or _safe(document, "isActive") is True
        settled = tracker.observe(now, in_front, _safe(ui, "activeCommand"), _safe(app.documents, "count"), pump_s)
        if settled or now >= deadline:
            return settled
        time.sleep(_POLL_S)


def _still_open(app, file_id, deadline):
    """Pump until no instance of the file is open; returns those that remain."""
    while True:
        _pump()
        _, infos = _snapshot(app)
        hits = [infos[i].get("name") for i in instances_of(infos, file_id)]
        if not hits or time.monotonic() >= deadline:
            return hits
        time.sleep(_POLL_S)


# -- One file -----------------------------------------------------------------


def _fail(entry, reason, message):
    entry.update(status="failed", reason=reason, message=message)


def _bring_to_front(app, row, documents, infos, entry):
    """Reuse or open the file with it active. Returns the document, or None after a failure."""
    index = pick_instance(infos, row["id"])
    if index is None:
        document = app.documents.open(row["data_file"], True)
        if document is None:
            _fail(entry, "open_failed", "Fusion did not open the document.")
            return None
        entry["opened"] = entry["still_open"] = True
    else:
        document = documents[index]
        entry["reused"] = True
        entry["warnings"].extend(instance_warnings(row, infos, index))
    if _safe(document, "isActive") is not True and not document.activate():
        _fail(entry, "activate_failed", (
            f"Fusion refused to activate {_safe(document, 'name')!r}, and a non-active document reads "
            "partial data. A hidden instance held by a parent cannot always be brought forward; "
            "close the parent, or open the file in its own tab."))
        return None
    return document


def _read(document, entry):
    """Fill in *entry* from the document's Design."""
    product = document.products.itemByProductType(DESIGN_PRODUCT)
    if product is None:
        _fail(entry, "no_design", "This document has no design product.")
        return None
    facts = read_design(adsk.fusion.Design.cast(product))
    entry["status"] = "read"
    entry["version"] = _safe(_safe(document, "dataFile"), "versionNumber")
    if facts["unreadable_volumes"]:
        entry["warnings"].append(f"{facts['unreadable_volumes']} body volume(s) could not be read; "
                                 "the totals leave them out.")
    if _is_int(facts["marker_position"]) and facts["marker_position"] < (facts["timeline_length"] or 0):
        entry["warnings"].append(f"The timeline marker is at {facts['marker_position']} of "
                                 f"{facts['timeline_length']}: later features are not in these numbers.")
    return facts


def _close_opened(app, document, entry, file_id, edited_ids):
    """Close a document this call opened, if that is safe, and verify it went."""
    if not _wait_settled(app, None, time.monotonic() + CLOSE_SETTLE_TIMEOUT_S, CLOSE_SETTLE_QUIET_S):
        entry["close_skipped"] = "not_settled"
        return
    block = close_block(_safe(document, "isModified"), _linked_file_ids(document), edited_ids)
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


def _new_entry(row):
    return {"file": row["file"], "id": row["id"], "name": row["name"], "status": None,
            "version": None, "latest_version": row["latest_version"], "elapsed_seconds": 0.0,
            "reused": False, "opened": False, "closed": False, "still_open": False, "warnings": []}


def _read_file(app, row, overall_deadline, entry):
    """Bring one file up, read it, and close it if this call opened it."""
    if not _wait_settled(app, None, min(time.monotonic() + SETTLE_TIMEOUT_S, overall_deadline)):
        _fail(entry, "not_settled", "Fusion did not go quiet, so the file was not touched.")
        return None

    documents, infos = _snapshot(app)
    edited_ids = {info.get("file_id") for info in infos if info.get("is_modified") is not False} - {None}
    document = None
    try:
        document = _bring_to_front(app, row, documents, infos, entry)
        if document is None:
            return None
        # open() blocks and cannot be polled, so its time does not count here.
        if not _wait_settled(app, document, min(time.monotonic() + SETTLE_TIMEOUT_S, overall_deadline)):
            _fail(entry, "not_settled", "The file did not finish loading in time; nothing was read, "
                                        "as a half-loaded assembly gives wrong counts.")
            return None
        return _read(document, entry)
    finally:
        if entry["opened"] and document is not None:
            _close_opened(app, document, entry, row["id"], edited_ids)


def _restore_active(app, document, name):
    """Put the starting document back in front."""
    result = {"name": name, "restored": False}
    if document is None:
        result["note"] = "No document was active at the start."
        return result
    try:
        if _safe(document, "isActive") is not True:
            document.activate()
        deadline = time.monotonic() + RESTORE_TIMEOUT_S
        while _safe(document, "isActive") is not True and time.monotonic() < deadline:
            _pump()
            time.sleep(_POLL_S)
        result["restored"] = _safe(document, "isActive") is True
    except Exception as exc:
        result["note"] = f"Could not re-activate it: {type(exc).__name__}: {exc}"
    if not result["restored"] and "note" not in result:
        result["note"] = "It did not become active again; closing a linked file may have closed it."
    return result


# -- Tool ---------------------------------------------------------------------


def _not_attempted(entry, reason, message):
    entry.update(status="not_attempted", reason=reason, message=message)


def _run(app, rows, started):
    """Read the files in turn; returns (entries, facts per entry)."""
    overall_deadline = started + TOTAL_BUDGET_S
    entries, facts = [], []
    for row in rows:
        entry = _new_entry(row)
        entries.append(entry)
        facts.append(None)
        if len(entries) == 2:
            first = entries[0]
            if first["status"] != "read":
                _not_attempted(entry, "stopped_after_failure", "Not attempted because the first file failed.")
                continue
            if not fits_budget(time.monotonic() - started, first["elapsed_seconds"]):
                _not_attempted(entry, "time_budget", (
                    "The first file took too long for a second one to fit inside the 120 s limit. Open the "
                    "larger file first (it is then reused, not reopened) and call again."))
                continue
        file_started = time.monotonic()
        try:
            facts[-1] = _read_file(app, row, overall_deadline, entry)
        except Exception as exc:
            _fail(entry, "error", f"{type(exc).__name__}: {exc}")
            log(f"[MCP] compare_files {row['name']!r}: {traceback.format_exc()}")
        entry["elapsed_seconds"] = round(time.monotonic() - file_started, 2)
        log(f"[MCP] compare_files {row['name']!r}: {entry['status']} in {entry['elapsed_seconds']}s")
    return entries, facts


def compare_files(arguments):
    """Compare two design files by their counts, volume, names and timeline length."""
    try:
        file_a, file_b, project_a, project_b = validate_arguments(arguments)
    except ValueError as exc:
        return _text(f"Error: {exc}", True)

    try:
        started = time.monotonic()
        app = get_app()
        rows, problems = _resolve_rows(app, [(file_a, project_a), (file_b, project_b)])
        if problems:
            return _text({"error": "Nothing was opened.", "problems": problems}, True)

        _, infos = _snapshot(app)
        active = _safe(app, "activeDocument")
        active_name = _safe(active, "name")
        entries, facts = _run(app, rows, started)
        restored = _restore_active(app, active, active_name)
        _, infos_after = _snapshot(app)

        complete = all(item is not None for item in facts)
        differences = compare_facts(facts[0], facts[1]) if complete else []
        for entry, item in zip(entries, facts):
            if item is not None:
                entry["facts"] = public_facts(item)
        return _text({
            "same": not differences if complete else None,
            "complete": complete,
            "differences": differences,
            "file_a": entries[0],
            "file_b": entries[1],
            "note": NOT_PROOF_NOTE,
            "active_document": restored,
            "documents_lost": lost_documents(infos, infos_after),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }, not complete)
    except Exception as exc:
        log(f"[MCP] compare_files failed: {exc}")
        return _text(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)
