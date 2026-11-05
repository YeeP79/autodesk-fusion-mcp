"""Read the health of timeline items, list them, and check the marker."""

import difflib
from contextlib import contextmanager

import adsk.fusion

from .api import safe, strict_items

PARAMETRIC_DESIGN = 1  # adsk.fusion.DesignTypes.ParametricDesignType
# adsk.fusion.FeatureHealthStates, with the states DEFAULT_STATES folds away.
HEALTH_NAMES = {0: "healthy", 1: "warning", 2: "error", 3: "suppressed", 4: "rolled_back", 5: "unknown"}

# Fallback enum values, used when adsk.fusion.FeatureHealthStates is unavailable.
DEFAULT_STATES = {"healthy": 0, "warning": 1, "error": 2, "unknown": 3}


def health_states():
    """``{"healthy": value, ...}`` from this build's FeatureHealthStates."""
    enum = getattr(adsk.fusion, "FeatureHealthStates", None)
    if enum is None:
        return DEFAULT_STATES
    return {
        "healthy": getattr(enum, "HealthyFeatureHealthState", 0),
        "warning": getattr(enum, "WarningFeatureHealthState", 1),
        "error": getattr(enum, "ErrorFeatureHealthState", 2),
        "unknown": getattr(enum, "UnknownFeatureHealthState", 3),
    }


def state_name(value, states=None):
    """Map a FeatureHealthStates value to 'healthy' / 'warning' / 'error' / 'unknown'."""
    for name, enum_value in (states or DEFAULT_STATES).items():
        if value == enum_value:
            return name
    return "unknown"


def health_of(timeline_object, states=None):
    """The health of one timeline item; 'unknown' when it cannot be read."""
    return state_name(safe(timeline_object, "healthState"), states or health_states())


def health_counts(design):
    """Count timeline items by health state; name the ones in error."""
    states = health_states()
    counts = {"healthy": 0, "warning": 0, "error": 0}
    errors = []
    for item in design.timeline:
        state = item.healthState
        if state == states["error"]:
            counts["error"] += 1
            errors.append(item.name)
        elif state == states["healthy"]:
            counts["healthy"] += 1
        else:
            counts["warning"] += 1
    return counts, errors


def _row(obj, position_index, group=None, rollable=True):
    entity = safe(obj, "entity")
    object_type = safe(entity, "objectType") if entity is not None else None
    participants = safe(entity, "participantBodies") if entity is not None else None
    try:
        participant_count = None if participants is None else len(participants)
    except TypeError:
        participant_count = None
    return {
        "index": position_index if isinstance(position_index, int) and position_index >= 0 else None,
        "name": safe(obj, "name", "") or "",
        "kind": "feature",
        "entity_type": object_type.rsplit("::", 1)[-1] if isinstance(object_type, str) else None,
        "component": safe(safe(entity, "parentComponent"), "name") if entity is not None else None,
        "group": group,
        "rollable": rollable,
        "health": HEALTH_NAMES.get(safe(obj, "healthState"), "unknown"),
        "message": safe(obj, "errorOrWarningMessage", "") or "",
        "participant_bodies": participant_count,
    }


def read_rows(timeline):
    """Return (rows, TimelineObjects), in timeline order.

    A collapsed group is one slot whose children have index -1. An expanded
    group's children hold their own slots, so they appear once, not again under
    the group.
    """
    top = strict_items(timeline)
    slots = {safe(obj, "index") for obj in top if not safe(obj, "isGroup")}
    rows, objects = [], []
    for position, obj in enumerate(top):
        index = safe(obj, "index", position)
        if not safe(obj, "isGroup"):
            rows.append(_row(obj, index))
            objects.append(obj)
            continue
        group = adsk.fusion.TimelineGroup.cast(obj) if hasattr(adsk.fusion, "TimelineGroup") else obj
        collapsed = safe(group, "isCollapsed") is not False
        name = safe(group, "name", "") or ""
        rows.append({"index": index if collapsed else None, "name": name, "kind": "group",
                     "entity_type": "TimelineGroup", "component": None, "group": None,
                     "rollable": collapsed, "health": "group", "message": "", "participant_bodies": None})
        objects.append(group)
        for child in strict_items(group):
            child_index = safe(child, "index", -1)
            if isinstance(child_index, int) and child_index >= 0 and child_index in slots:
                continue
            in_slot = isinstance(child_index, int) and child_index >= 0
            rows.append(_row(child, child_index, group=name, rollable=in_slot))
            objects.append(child)
    return rows, objects


def find_row(rows, name):
    """Return (position in rows, None), or (None, refusal text): names are looked up, never guessed."""
    hits = [i for i, row in enumerate(rows) if row["name"] == name]
    if not hits:
        close = difflib.get_close_matches(name, [row["name"] for row in rows], n=5)
        hint = f" Close matches: {close}." if close else ""
        return None, f"No timeline item named {name!r}.{hint}"
    if len(hits) > 1:
        listed = "; ".join(
            f"index {rows[i]['index']}, {rows[i].get('entity_type')}, component {rows[i].get('component')}"
            for i in hits)
        return None, (f"{len(hits)} timeline items are named {name!r} ({listed}). "
                      "Rename one so the name is unique, then call again.")
    return hits[0], None


def timeline_of(design):
    """The design's timeline, or None for a direct design."""
    if safe(design, "designType") != PARAMETRIC_DESIGN:
        return None
    return safe(design, "timeline")


def marker_refusal(marker, count):
    """Why a marker short of the end stops a write; None at the end."""
    if marker == count:
        return None
    return (f"The timeline marker is at {marker} of {count}, not the end. "
            "Move it to the end first, so nothing rolled back is mistaken for this call's work.")


def broken_refusal(rows):
    """Why features in error stop a write (Fusion rolls back API edits); None when there are none."""
    broken = [row for row in rows if row["kind"] == "feature" and row["health"] == "error"]
    if not broken:
        return None
    names = ", ".join(repr(row["name"]) for row in broken[:10])
    return (f"The timeline already has {len(broken)} item(s) in error ({names}). "
            "Fix or suppress them first (timeline_health).")


_rolled_back_windows = 0


@contextmanager
def rolled_back_window():
    """Marks code running with the marker rolled back on purpose (timeline_insert).

    A write there is meant to land mid-timeline, so preflight drops its marker rule.
    """
    global _rolled_back_windows
    _rolled_back_windows += 1
    try:
        yield
    finally:
        _rolled_back_windows -= 1


def in_rolled_back_window():
    return _rolled_back_windows > 0
