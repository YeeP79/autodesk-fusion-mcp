"""Run Python with the timeline marker rolled back before a named item.

New features land at the marker, so features that must exist before a later
item (a mirror that should pick them up) need the marker moved first:
``rollTo(True)``, add, ``moveToEnd()``. By hand that pattern leaves the design
rolled back whenever the script raises, and nothing says what the new features
did to the items after them. This tool always restores the marker and reports
the difference.

Rules:

- The item is looked up by name, once, right before rolling: indices shift.
  A name repeated across components is refused, not guessed.
- A timeline with errors, or a marker not at the end, is refused: the health
  comparison would mean nothing and Fusion rolls back edits on a broken
  timeline anyway.
- ``moveToEnd()`` runs in a ``finally``, even for SystemExit. It can raise a
  compute-failure dump and still move the marker, so success is judged by
  re-reading the marker, not by the call.
- Only the code runs inside ``rolled_back_window``, so a ``primitives`` write
  there lands at the marker. The rule that the marker be at the end holds
  everywhere else, restoring included.
- A child of a collapsed group is not a timeline slot (``index`` is -1) and an
  expanded group cannot be rolled to, so both are refused with what to do.

Before/after items are matched by name and order, not entityToken (a token can
differ between reads of the same entity), so an item the script renamed shows
up as one removed and one added.

Rolling back also drops participant bodies from features edited in that state,
with no error. Participant counts are compared across the call to catch it.
"""

import difflib
import json
import traceback

import adsk.core
import adsk.fusion

from . import python_exec
from .common.results import tool_result
from .common.timeline import (
    broken_refusal,
    find_row,
    marker_refusal,
    read_rows,
    rolled_back_window,
    timeline_of as _timeline_of,
)
from .dispatch import get_app, log

MAX_LISTED = 200
MAX_ERROR_CHARS = 2000
RESTORE_ATTEMPTS = 2
_ARGS = {"before", "code", "session_id", "dry_run"}


def _attr(obj, name, default=None):
    """Read an attribute; Fusion properties can raise on stale or partial objects."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _truncate(text, limit=MAX_ERROR_CHARS):
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated {len(text) - limit} more characters]"


# -- Validation ---------------------------------------------------------------


def validate_arguments(arguments):
    """Return (before, code, session_id, dry_run), or raise ValueError."""
    unknown = sorted(set(arguments) - _ARGS)
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(unknown)}")
    before = arguments.get("before")
    if not isinstance(before, str) or not before.strip():
        raise ValueError("'before' must be the name of a timeline item")
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("'code' must be non-empty Python source")
    session_id = arguments.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError("'session_id' must be a non-empty string")
    dry_run = arguments.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError("'dry_run' must be a boolean")
    # A syntax error found after rolling back would cost two full recomputes.
    try:
        compile(code, "<mcp-script>", "exec")
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"'code' does not compile: {exc}") from exc
    return before.strip(), code, session_id, dry_run


# -- Pure logic ---------------------------------------------------------------


def health_counts(rows):
    """Count features by health. Groups carry no health of their own."""
    counts = {}
    for row in rows:
        if row["kind"] == "feature":
            counts[row["health"]] = counts.get(row["health"], 0) + 1
    return counts


def brief(row):
    """The fields of a row worth reporting."""
    keys = ("index", "name", "kind", "entity_type", "component", "group", "health", "message")
    return {key: row[key] for key in keys if row.get(key) not in (None, "")}


def _list(rows):
    return [brief(row) for row in rows[:MAX_LISTED]]


def resolve_target(rows, name):
    """Return (position in rows, None), or (None, refusal text)."""
    position, refusal = find_row(rows, name)
    if refusal:
        return None, refusal
    row = rows[position]
    if row["rollable"]:
        return position, None
    if row["kind"] == "group":
        return None, (f"{name!r} is an expanded group, which Fusion cannot roll to. "
                      "Name the first item in it, or collapse the group.")
    return None, (f"{name!r} is inside collapsed group {row.get('group')!r}, so it has no timeline slot. "
                  "Expand the group (TimelineGroup.isCollapsed = False), or name the group.")


def preflight(rows, marker, count, name):
    """Return (position, refusals). Nothing may run unless refusals is empty."""
    position, problem = resolve_target(rows, name)
    found = [problem, marker_refusal(marker, count), broken_refusal(rows)]
    return position, [refusal for refusal in found if refusal]


def rolled_ok(returned, marker, index, is_rolled_back):
    """Did rollTo really put the marker before the item? It can return False."""
    return bool(returned) and (marker == index or is_rolled_back is True)


def align(before, after, insert_at):
    """Match rows across the call; returns (pairs, added, removed).

    Items land at the marker, so everything before *insert_at* and the run of
    items at the end normally line up one for one. Trimming both ends first
    keeps a new item from pairing with a later item of the same name in another
    component. Whatever is left (the script deleted or renamed something) is
    matched by name. *pairs* holds positions (in before, in after).
    """
    names_b = [row["name"] for row in before]
    names_a = [row["name"] for row in after]
    head = 0
    while head < min(insert_at, len(names_b), len(names_a)) and names_b[head] == names_a[head]:
        head += 1
    tail = 0
    while (tail < min(len(names_b), len(names_a)) - head
           and names_b[len(names_b) - 1 - tail] == names_a[len(names_a) - 1 - tail]):
        tail += 1

    pairs = [(i, i) for i in range(head)]
    middle_b = names_b[head:len(names_b) - tail]
    middle_a = names_a[head:len(names_a) - tail]
    matcher = difflib.SequenceMatcher(None, middle_b, middle_a, autojunk=False)
    for block in matcher.get_matching_blocks():
        pairs.extend((head + block.a + k, head + block.b + k) for k in range(block.size))
    pairs.extend((len(names_b) - 1 - k, len(names_a) - 1 - k) for k in range(tail - 1, -1, -1))

    matched_b = {b for b, _ in pairs}
    matched_a = {a for _, a in pairs}
    added = [row for i, row in enumerate(after) if i not in matched_a]
    removed = [row for i, row in enumerate(before) if i not in matched_b]
    return pairs, added, removed


def compare_rows(before, after, position):
    """What the call changed, from the rows read before rolling and after restoring."""
    pairs, added, removed = align(before, after, position)
    later_errors, earlier_errors, later_warnings, participants = [], [], [], []
    target_after = None
    for b, a in pairs:
        old, new = before[b], after[a]
        if b == position:
            target_after = a
        if new["health"] == "error" and old["health"] != "error":
            (later_errors if b >= position else earlier_errors).append(brief(new))
        elif new["health"] == "warning" and old["health"] != "warning" and b >= position:
            later_warnings.append(brief(new))
        was, now = old.get("participant_bodies"), new.get("participant_bodies")
        if was is not None and was != (now or 0):
            participants.append({"name": new["name"], "before": was, "after": now})

    misplaced = []
    if target_after is not None:
        matched = {a for _, a in pairs}
        misplaced = [row["name"] for i, row in enumerate(after) if i > target_after and i not in matched]
    return {
        "items_added": _list(added),
        "items_removed": _list(removed),
        "later_items_in_error": later_errors,
        "earlier_items_in_error": earlier_errors,
        "later_items_with_new_warnings": later_warnings,
        "participant_bodies_changed": participants,
        "landed_after_target": misplaced,
        "target_found_after": target_after is not None,
        "added_errors": [row["name"] for row in added if row["health"] == "error"],
    }


def warnings_for(name, changes, code_ok):
    """Plain-language flags for things the caller should not miss."""
    warnings = []
    if code_ok and not changes["items_added"]:
        warnings.append("The code ran but added no timeline items.")
    if changes["landed_after_target"]:
        warnings.append(f"{changes['landed_after_target']} landed after {name!r}, not before it: "
                        "the code moved the marker itself.")
    if not changes["target_found_after"]:
        warnings.append(f"{name!r} is no longer in the timeline under that name.")
    if changes["participant_bodies_changed"]:
        warnings.append("Participant bodies changed on features listed in participant_bodies_changed. "
                        "Editing a feature while rolled back can drop them; set them again.")
    return warnings


def overall_status(outcome, changes):
    """The single word a caller branches on, worst first."""
    if not outcome["restore"]["restored"]:
        return "marker_not_restored"
    if not outcome["rolled"]:
        return "roll_failed"
    if outcome.get("exception") or not (outcome.get("python") or {}).get("success"):
        return "code_failed"
    if changes is None:
        return "unverified"
    if changes["later_items_in_error"] or changes["earlier_items_in_error"] or changes["added_errors"]:
        return "items_in_error"
    return "ok"


# -- Fusion reads -------------------------------------------------------------


def _active_design(app):
    return adsk.fusion.Design.cast(app.activeProduct)


# -- The rolled-back window ---------------------------------------------------


def restore_marker(timeline, document):
    """Move the marker to the end and prove it. Never raises."""
    result = {"restored": False, "marker_position": None, "count": None, "errors": []}
    for _ in range(RESTORE_ATTEMPTS):
        # The script may have left another document in front.
        try:
            if document is not None and _attr(document, "isActive") is False:
                document.activate()
                result["reactivated_document"] = True
        except Exception as exc:
            result["errors"].append(_truncate(f"activate: {type(exc).__name__}: {exc}"))
        try:
            if timeline.moveToEnd() is False:
                result["errors"].append("moveToEnd returned False")
        except Exception as exc:
            result["errors"].append(_truncate(f"moveToEnd: {type(exc).__name__}: {exc}"))
        marker, count = _attr(timeline, "markerPosition"), _attr(timeline, "count")
        result.update(marker_position=marker, count=count)
        if isinstance(marker, int) and isinstance(count, int) and marker >= count:
            result["restored"] = True
            break
    if not result["restored"]:
        result["message"] = ("The marker is NOT at the end. Drag it to the end in Fusion, or run "
                             "design.timeline.moveToEnd(), before doing anything else.")
    return result


def _run_code(code, session_id):
    """Run through execute_python's runner; returns its JSON payload as a dict."""
    # Without a session_id the shared "default" session would leak between
    # callers, which MCP 2026-07-28 refuses for execute_python too.
    arguments = {"code": code, "persistent": session_id is not None}
    if session_id is not None:
        arguments["session_id"] = session_id
    result = python_exec.run_python(arguments)
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except ValueError:
        return {"success": not result.get("isError"), "error": text}


def run_rolled_back(timeline, target, index, document, code, session_id):
    """Roll before *target*, run *code*, and always restore the marker."""
    outcome = {"rolled": False}
    try:
        returned = target.rollTo(True)
        marker = _attr(timeline, "markerPosition")
        outcome["marker_during"] = marker
        if rolled_ok(returned, marker, index, _attr(target, "isRolledBack")):
            outcome["rolled"] = True
            with rolled_back_window():
                outcome["python"] = _run_code(code, session_id)
        else:
            outcome["exception"] = f"rollTo returned {returned!r} and left the marker at {marker}; the code did not run."
    except BaseException as exc:  # SystemExit from a script must not skip the report either
        outcome["exception"] = _truncate(f"{type(exc).__name__}: {exc}")
    finally:
        outcome["restore"] = restore_marker(timeline, document)
    return outcome


# -- Tool ---------------------------------------------------------------------


def _describe_plan(rows, position, marker, count):
    target = rows[position]
    later = rows[position + 1:]
    return {
        "item": brief(target),
        "marker_position": marker,
        "timeline_count": count,
        "health_counts": health_counts(rows),
        "items_after": _list(later),
        "items_after_count": len(later),
    }


def _report(name, plan, rows, position, outcome, timeline):
    report = {"before": name, "dry_run": False, "item": plan["item"], "inserted_at": plan["item"].get("index")}
    if "python" in outcome:
        report["python"] = outcome["python"]
    if outcome.get("exception"):
        report["exception"] = outcome["exception"]
    report["marker"] = outcome["restore"]
    report["health_before"] = plan["health_counts"]

    changes = None
    try:
        rows_after, _ = read_rows(timeline)
        changes = compare_rows(rows, rows_after, position)
        report["health_after"] = health_counts(rows_after)
    except Exception as exc:
        report["read_after_error"] = _truncate(f"{type(exc).__name__}: {exc}")

    report["status"] = overall_status(outcome, changes)
    if changes is not None:
        code_ok = bool((outcome.get("python") or {}).get("success"))
        report["warnings"] = warnings_for(name, changes, code_ok)
        for key in ("target_found_after", "added_errors"):
            changes.pop(key)
        report.update(changes)
    if report["status"] == "code_failed":
        report["note"] = "Anything the code added before it failed is still in the timeline; see items_added."
    return report, report["status"] != "ok"


def timeline_insert(arguments):
    """Run Python with the marker before a named timeline item, then restore it."""
    try:
        before, code, session_id, dry_run = validate_arguments(arguments)
    except ValueError as exc:
        return tool_result(f"Error: {exc}", True)

    try:
        app = get_app()
        design = _active_design(app)
        if design is None:
            return tool_result("Error: the active product is not a Fusion design", True)
        timeline = _timeline_of(design)
        if timeline is None:
            return tool_result("Error: this is a direct design; it has no timeline", True)

        rows, objects = read_rows(timeline)
        marker, count = timeline.markerPosition, timeline.count
        position, refusals = preflight(rows, marker, count, before)
        if refusals:
            return tool_result({"error": "Nothing was run.", "dry_run": dry_run, "refusals": refusals,
                          "marker_position": marker, "timeline_count": count,
                          "health_counts": health_counts(rows)}, True)

        plan = _describe_plan(rows, position, marker, count)
        if dry_run:
            return tool_result({"dry_run": True, "before": before, **plan})

        outcome = run_rolled_back(timeline, objects[position], rows[position]["index"],
                                  _attr(app, "activeDocument"), code, session_id)
        report, is_error = _report(before, plan, rows, position, outcome, timeline)
        log(f"[MCP] timeline_insert before {before!r}: {report['status']}")
        return tool_result(report, is_error)
    except Exception as exc:
        log(f"[MCP] timeline_insert failed: {exc}")
        return tool_result(f"Error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}", True)
