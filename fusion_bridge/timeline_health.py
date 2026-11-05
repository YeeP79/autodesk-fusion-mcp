"""Timeline health report, optional suppression of broken items, and a short
summary for Fusion's compute-failure dump.

While the timeline has broken features, Fusion rolls back API edits and raises
an exception whose text lists every failed feature, one per line
(``Extrude11 / Compute Failed // NO_TARGET_BODY - No target body!``). That dump
can run to many kilobytes and hides the actual cause, so the edit tools prepend
a one-line explanation (see ``rollback_summary``).

The parsing and formatting helpers are pure so they can be tested without Fusion.
"""

import json
import re
import traceback

import adsk.core
import adsk.fusion

from .common.results import error_result, ok_result
from .dispatch import get_app, log

# Fallback enum values, used when adsk.fusion.FeatureHealthStates is unavailable.
_DEFAULT_STATES = {"healthy": 0, "warning": 1, "error": 2, "unknown": 3}
MAX_ORIGINAL_CHARS = 2000

# One failed-feature line: "<name> / <reason> // <CODE> - <detail>". The first
# line carries the exception's numeric prefix ("3 : ").
_FAILURE_LINE = re.compile(
    r"^(?:\d+\s*:\s*)?(?P<name>[^\n/]+?) / [^\n]*?// [A-Z][A-Z0-9_]+ - ", re.MULTILINE
)
_REASON_MARKERS = ("Compute Failed", "Reference Failures", "Source Lost")


# -- Pure helpers -------------------------------------------------------------


def parse_failed_features(text):
    """Return feature names from a compute-failure dump, in order, de-duplicated."""
    names = []
    for match in _FAILURE_LINE.finditer(text or ""):
        name = match.group("name").strip()
        if name and name not in names:
            names.append(name)
    return names


def is_compute_failure_dump(text):
    """True when *text* looks like Fusion's list of failed timeline features.

    Requires at least one known reason marker and either two or more
    failed-feature lines or a single line marked "Compute Failed", so an
    ordinary error that happens to contain " / " is not misread.
    """
    if not isinstance(text, str) or not any(m in text for m in _REASON_MARKERS):
        return False
    lines = _FAILURE_LINE.findall(text)
    return len(lines) >= 2 or (len(lines) == 1 and "Compute Failed" in text)


def truncate(text, limit=MAX_ORIGINAL_CHARS):
    """Cut *text* to *limit* characters, noting how much was dropped."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated {len(text) - limit} more characters]"


def format_rollback_summary(count, earliest_name, earliest_index):
    """One-line explanation of a rolled-back edit. *earliest_index* may be None."""
    noun = "item is" if count == 1 else "items are"
    where = f"{earliest_name} at step {earliest_index}" if earliest_index is not None else earliest_name
    return (
        f"Edit refused: Fusion rolled it back because {count} timeline {noun} broken "
        f"(earliest: {where}). Fix or suppress them first (timeline_health)."
    )


def state_name(value, states=None):
    """Map a FeatureHealthStates value to 'healthy' / 'warning' / 'error' / 'unknown'."""
    for name, enum_value in (states or _DEFAULT_STATES).items():
        if value == enum_value:
            return name
    return "unknown"


def build_report(marker_position, count, active_component, is_root, items):
    """Assemble the timeline_health result from plain values.

    *items* are dicts with index, name, entity_type, health, message, suppressed.
    """
    counts = {"healthy": 0, "warning": 0, "error": 0, "unknown": 0}
    for item in items:
        counts[item["health"]] = counts.get(item["health"], 0) + 1
    warnings = []
    if marker_position < count:
        warnings.append(
            f"Timeline marker is rolled back to {marker_position} of {count}; "
            "later features are not computed. Move it to the end before editing."
        )
    if not is_root:
        warnings.append(
            f"Active component is '{active_component}', not the root component; "
            "edits land in that component."
        )
    return {
        "marker_position": marker_position,
        "count": count,
        "rolled_back": marker_position < count,
        "active_component": active_component,
        "active_is_root": is_root,
        "health_counts": counts,
        "non_healthy": [i for i in items if i["health"] != "healthy"],
        "warnings": warnings,
    }


# -- Fusion access ------------------------------------------------------------


def _health_states():
    enum = getattr(adsk.fusion, "FeatureHealthStates", None)
    if enum is None:
        return _DEFAULT_STATES
    return {
        "healthy": getattr(enum, "HealthyFeatureHealthState", 0),
        "warning": getattr(enum, "WarningFeatureHealthState", 1),
        "error": getattr(enum, "ErrorFeatureHealthState", 2),
        "unknown": getattr(enum, "UnknownFeatureHealthState", 3),
    }


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _active_design():
    return adsk.fusion.Design.cast(get_app().activeProduct)


def _read_items(timeline, states):
    """Return (item dict, TimelineObject) pairs for every non-group timeline item."""
    rows = []
    for i in range(timeline.count):
        obj = timeline.item(i)
        if _safe(lambda: obj.isGroup, False):
            continue
        entity = _safe(lambda: obj.entity)
        rows.append((
            {
                "index": _safe(lambda: obj.index, i),
                "name": _safe(lambda: obj.name, ""),
                "entity_type": getattr(entity, "objectType", None) if entity is not None else None,
                "health": state_name(_safe(lambda: obj.healthState), states),
                "message": _safe(lambda: obj.errorOrWarningMessage, "") or "",
                "suppressed": bool(_safe(lambda: obj.isSuppressed, False)),
            },
            obj,
        ))
    return rows


def _report(design, states):
    timeline = design.timeline
    rows = _read_items(timeline, states)
    active = design.activeComponent
    is_root = active == design.rootComponent
    return build_report(
        timeline.markerPosition, timeline.count, active.name, is_root, [r for r, _ in rows]
    ), rows


def _suppress_broken(rows, states):
    """Suppress error-state items earliest first; skip suppressed or recovered ones."""
    suppressed, skipped, failed = [], [], []
    for row, obj in sorted(rows, key=lambda pair: pair[0]["index"]):
        if row["health"] != "error":
            continue
        label = {"index": row["index"], "name": row["name"]}
        if row["suppressed"] or _safe(lambda: obj.isSuppressed, False):
            skipped.append({**label, "reason": "already suppressed"})
            continue
        # Suppressing an earlier item can heal or break later ones; re-check.
        if state_name(_safe(lambda: obj.healthState), states) != "error":
            skipped.append({**label, "reason": "no longer in error"})
            continue
        try:
            obj.isSuppressed = True
            suppressed.append(label)
        except Exception as exc:
            failed.append({**label, "error": truncate(str(exc), 300)})
    return {"suppressed": suppressed, "skipped": skipped, "failed": failed}


def timeline_health(arguments):
    """Report timeline health for the active design; optionally suppress broken items."""
    suppress = arguments.get("suppress_broken", False)
    confirm = arguments.get("confirm", False)
    if not isinstance(suppress, bool):
        return error_result("Error: 'suppress_broken' must be a boolean")
    if not isinstance(confirm, bool):
        return error_result("Error: 'confirm' must be a boolean")
    if suppress and not confirm:
        return error_result(
            "Error: suppress_broken changes the design; pass confirm: true alongside it. "
            "Run timeline_health without it first to see what would be suppressed."
        )

    try:
        design = _active_design()
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        states = _health_states()
        report, rows = _report(design, states)
        if suppress:
            action = _suppress_broken(rows, states)
            report, _ = _report(design, states)
            report["suppress_broken"] = action
            log(f"[MCP] timeline_health suppressed {len(action['suppressed'])} item(s)")
        return ok_result(json.dumps(report, indent=2))
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] timeline_health failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")


# -- Rollback summary for the edit tools --------------------------------------


def _live_broken_summary():
    """(count, earliest name, earliest index) from the live timeline, or None."""
    try:
        design = _active_design()
        if design is None:
            return None
        rows = _read_items(design.timeline, _health_states())
        broken = [r for r, _ in rows if r["health"] in ("error", "warning") and not r["suppressed"]]
        if not broken:
            return None
        first = min(broken, key=lambda r: r["index"])
        return len(broken), first["name"], first["index"]
    except Exception:
        return None


def rollback_summary(text, live_reader=None):
    """Summary line for a compute-failure dump, or None for any other error.

    Counts come from the live timeline when it can be read (the dump's order is
    not guaranteed to be timeline order), otherwise from the dump itself.
    """
    if not is_compute_failure_dump(text):
        return None
    live = (live_reader or _live_broken_summary)()
    if live:
        return format_rollback_summary(*live)
    names = parse_failed_features(text)
    return format_rollback_summary(len(names), names[0], None)
