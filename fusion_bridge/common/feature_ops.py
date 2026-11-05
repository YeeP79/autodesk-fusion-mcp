"""What every feature-building tool does around one write.

``preflight`` before anything changes, ``read_value`` and ``value_input`` for
dimensions, ``add_feature`` for the add itself, ``snapshot`` and ``baseline`` to
measure, ``undo`` to take a feature back, ``temp_copy`` for off-timeline booleans,
and ``body_changes`` and ``envelope`` for the result. The checks are written over
duck-typed objects so fakes can drive them.

``add_feature`` keeps a refused feature from costing a macro its earlier work:
Fusion's refusal comes back as a value. A ``TypeError`` still raises, because it
means our call is wrong and a retry would repeat it.
"""

import re

import adsk.core
import adsk.fusion

from ..document_guard import read_only_state
from .api import safe, strict_items
from .args import is_finite_number
from .bodies import (
    assign_keys,
    bbox,
    iter_body_entries,
    measure_summary,
    snapshot_records,
    store_baseline,
    volume_changed,
)
from .geometry import to_units, unit_labels
from .results import tool_result
from .timeline import (
    HEALTH_NAMES,
    PARAMETRIC_DESIGN,
    broken_refusal,
    in_rolled_back_window,
    marker_refusal,
    read_rows,
)

STATUSES = ("built", "planned", "partial", "suspect", "undone", "refused")
OK_STATUSES = ("built", "planned")

_ERROR_HEALTH = 2  # adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
READ_ONLY_WARNING = "the document is read-only: edits apply in this session but will not save"

# Digits and operators alone have no unit and no parameter, so Fusion would read
# them in the document's default units. An exponent is taken out first; a lone
# "e" is a parameter name.
_EXPONENT = re.compile(r"(?<=[\d.])[eE][+-]?\d+")
_BARE_NUMBER = re.compile(r"[\d\s.+\-*/^()]+")
# A copy this far off the proxy's box came back in component space.
_FRAME_TOLERANCE_CM = 1e-6


# -- Preflight ------------------------------------------------------------------


def _linked_paths(root):
    """Occurrence paths of linked components. Their children sit below these, unflagged."""
    return [safe(occ, "fullPathName") for occ in strict_items(safe(root, "allOccurrences"))
            if safe(occ, "isReferencedComponent", False)]


def _in_linked(path, linked):
    return any(path == top or path.startswith(top + "+") for top in linked)


def preflight(design, document=None, targets=()):
    """Return (refusals, warnings); nothing may be written unless refusals is empty.

    All refusals are listed together, so one call shows everything to fix.
    *targets* are body entries with an ``occurrence_path``. A read-only document
    only warns: the edit is real, it just will not save.
    """
    if design is None:
        return ["The active product is not a Fusion design."], []
    if safe(design, "designType") != PARAMETRIC_DESIGN:
        return ["This is a direct design: it has no timeline to add a feature to."], []

    refusals = []
    timeline = design.timeline
    rows, _ = read_rows(timeline)
    broken = broken_refusal(rows)
    if broken:
        refusals.append(broken)
    if not in_rolled_back_window():
        marker = marker_refusal(safe(timeline, "markerPosition"), safe(timeline, "count"))
        if marker:
            refusals.append(marker)

    edited = safe(design, "activeEditObject")
    if edited is not None and str(safe(edited, "objectType", "")).endswith("BaseFeature"):
        refusals.append(f"Base feature {safe(edited, 'name')!r} is being edited. "
                        "Finish it first (BaseFeature.finishEdit): features cannot be added inside it.")

    linked = _linked_paths(design.rootComponent)
    named = sorted({e["occurrence_path"] for e in targets if _in_linked(e["occurrence_path"], linked)})
    if named:
        refusals.append(f"These targets are in linked components, which cannot be edited here: {named}.")

    warnings = [READ_ONLY_WARNING] if document is not None and read_only_state(document) is True else []
    return refusals, warnings


# -- Values ---------------------------------------------------------------------


def is_bare_number(expression):
    return _BARE_NUMBER.fullmatch(_EXPONENT.sub("", expression)) is not None


def read_value(arguments, key, units, expression_key=None, required=True, minimum=None):
    """One dimension from its two keys: ``{"text": str, "number": float or None}``, or None.

    *key* is a number and *expression_key* (default ``<key>_expression``) a
    string; exactly one may be given, and a blank expression counts as absent.
    *units* is the unit a number is in: ``in``, ``cm``, ``mm`` or ``deg``. *minimum*
    is exclusive and applies to a number; an expression's value is Fusion's.
    Raises ValueError.
    """
    expression_key = expression_key or f"{key}_expression"
    number, expression = arguments.get(key), arguments.get(expression_key)
    if expression is not None and not isinstance(expression, str):
        raise ValueError(f"'{expression_key}' must be a string")
    if expression is not None and not expression.strip():
        expression = None
    if number is not None and expression is not None:
        raise ValueError(f"give one of '{key}' and '{expression_key}', not both")
    if number is None and expression is None:
        if required:
            raise ValueError(f"one of '{key}' and '{expression_key}' is required")
        return None
    if expression is not None:
        if is_bare_number(expression):
            raise ValueError(f"'{expression_key}' {expression!r} has no unit, so it would resolve in the "
                             f"document's default units; write it with one, or pass '{key}'")
        return {"text": expression.strip(), "number": None}
    if not is_finite_number(number):
        raise ValueError(f"'{key}' must be a finite number")
    if minimum is not None and number <= minimum:
        raise ValueError(f"'{key}' must be greater than {minimum}")
    return {"text": f"{float(number):.12g} {units}", "number": float(number)}


def value_input(value, units_manager, kind="cm"):
    """A ValueInput from ``read_value``'s result, so the dialog shows real units.

    *kind* is the unit type the expression must evaluate to: ``cm`` for a length,
    ``deg`` for an angle. Raises ValueError when Fusion cannot read it.
    """
    if not units_manager.isValidExpression(value["text"], kind):
        raise ValueError(f"{value['text']!r} is not a valid expression for a value in {kind}")
    return adsk.core.ValueInput.createByString(value["text"])


# -- Adding a feature -----------------------------------------------------------


def add_feature(add):
    """Run ``add()`` and report: ``{"feature", "status", "refusal", "health", "warnings"}``.

    ``status`` is ``built`` or ``refused``. A ``RuntimeError`` is Fusion refusing,
    kept word for word. A feature that came back in error is deleted and counted
    as refused, with its message; if it will not delete, that is said too.
    """
    try:
        feature = add()
    except TypeError:
        raise
    except RuntimeError as exc:
        return _add_refused(str(exc))
    if feature is None:
        return _add_refused("the add returned no feature")
    state = safe(feature, "healthState")
    message = safe(feature, "errorOrWarningMessage", "") or ""
    if state == _ERROR_HEALTH:
        outcome = _add_refused(message or "the feature came back in error")
        deleted, why = _delete(feature)
        if not deleted:
            outcome["warnings"].append(f"the failed feature is still in the timeline: {why}")
        return outcome
    return {"feature": feature, "status": "built", "refusal": None,
            "health": HEALTH_NAMES.get(state, "unknown"), "warnings": [message] if message else []}


def _add_refused(text):
    return {"feature": None, "status": "refused", "refusal": text, "health": None, "warnings": []}


def _delete(feature):
    """Return (deleted, why not)."""
    try:
        if feature.deleteMe() is False:
            return False, "deleteMe returned False"
    except RuntimeError as exc:
        return False, str(exc)
    return True, None


# -- Measuring ------------------------------------------------------------------


def snapshot(design):
    """Records of every body in the design, hidden ones included, keyed like check_solids'.

    A body nobody named is exactly the one a stray cut reaches.
    """
    entries = assign_keys(list(iter_body_entries(design.rootComponent)))
    return snapshot_records([measure_summary(e) for e in entries])


def baseline(design):
    """Return (records, token): a snapshot stored so ``check_solids(baseline_token=...)`` needs no extra call."""
    records = snapshot(design)
    return records, store_baseline(records, safe(safe(design, "parentDocument"), "name"))


def _record_ref(key, rec):
    ref = {"occurrence_path": rec["occurrence_path"], "body_name": rec["body"]}
    if key.endswith(f"#{rec['index']}"):
        ref["body_index"] = rec["index"]
    return ref


def _changed(before, after):
    return volume_changed(before["volume"], after["volume"]) or before["faces"] != after["faces"]


def _touched_row(key, before, after, role, units):
    delta = (after["volume"] or 0.0) - (before["volume"] or 0.0)
    return {"ref": _record_ref(key, after), "role": role,
            "volume_before": to_units(before["volume"], units, 3), "volume_after": to_units(after["volume"], units, 3),
            "delta": to_units(delta, units, 3),
            "percent": round(100.0 * delta / before["volume"], 2) if before["volume"] else None,
            "faces_before": before["faces"], "faces_after": after["faces"]}


def _whole_row(key, rec, units):
    return {"ref": _record_ref(key, rec), "volume": to_units(rec["volume"], units, 3), "faces": rec["faces"]}


def _other_instance(rec, target_records):
    """Another occurrence of a target's component: editing one instance edits them all."""
    return any(t["component"] == rec["component"] and t["body"] == rec["body"]
               and t["occurrence_path"] != rec["occurrence_path"] for t in target_records)


def body_changes(before, after, target_keys, units):
    """The envelope's ``bodies``: what two snapshots say a write did.

    A changed body that is neither a target nor another instance of one goes in
    ``outside_target``; a body that went away is ``consumed`` whoever owned it,
    and ``lost_outside`` tells the two kinds apart.
    """
    targets = set(target_keys)
    target_records = [before[k] for k in targets if k in before]
    result = {"touched": [], "created": [], "consumed": [], "outside_target": []}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old is None:
            result["created"].append(_whole_row(key, new, units))
        elif new is None:
            result["consumed"].append(_whole_row(key, old, units))
        elif _changed(old, new):
            if key in targets:
                result["touched"].append(_touched_row(key, old, new, "target", units))
            elif _other_instance(new, target_records):
                result["touched"].append(_touched_row(key, old, new, "other_instance", units))
            else:
                result["outside_target"].append(_touched_row(key, old, new, "outside", units))
    return result


def unchanged(before, after, keys):
    """The bodies of *keys* a write left as they were."""
    return [k for k in keys if k in before and k in after and not _changed(before[k], after[k])]


def lost_outside(before, after, target_keys):
    """Keys of bodies nobody targeted that lost volume or vanished: the collateral to act on."""
    targets = set(target_keys)
    target_records = [before[k] for k in targets if k in before]
    lost = []
    for key, old in before.items():
        if key in targets or _other_instance(old, target_records):
            continue
        new = after.get(key)
        if new is None or (volume_changed(old["volume"], new["volume"])
                           and (new["volume"] or 0.0) < (old["volume"] or 0.0)):
            lost.append(key)
    return sorted(lost)


# -- Undo -----------------------------------------------------------------------


def undo(design, feature, before):
    """Delete *feature* and prove the design went back: ``{"undone": bool, "warnings": [...]}``.

    A feature that deleted but left volumes different is not undone: the caller
    stays ``refused`` and the warning names the bodies.
    """
    deleted, why = _delete(feature)
    if not deleted:
        return {"undone": False, "warnings": [f"the feature could not be deleted: {why}"]}
    after = snapshot(design)
    differ = sorted(k for k in set(before) | set(after)
                    if k not in before or k not in after or volume_changed(before[k]["volume"], after[k]["volume"]))
    if differ:
        return {"undone": False, "warnings": [f"the feature was deleted but these bodies did not return: {differ[:20]}"]}
    return {"undone": True, "warnings": []}


# -- Temporary bodies -----------------------------------------------------------


def _box_gap(a, b):
    if a is None or b is None:
        return 0.0
    return max(abs(x - y) for pa, pb in zip(a, b) for x, y in zip(pa, pb))


def temp_copy(body, manager=None):
    """A temporary copy of *body* in world coordinates, never in the design.

    ``booleanOperation`` modifies its target, so take a fresh copy per operation.
    A copy of a proxy may come back in component space (CM-2): its box is
    compared with the proxy's and ``transform2`` applied if they differ. A body
    whose transform maps its box onto itself cannot be told apart this way.
    Raises RuntimeError when Fusion gives no copy.
    """
    manager = manager or adsk.fusion.TemporaryBRepManager.get()
    copy = manager.copy(body)
    if copy is None:
        raise RuntimeError(f"TemporaryBRepManager.copy returned nothing for {safe(body, 'name')!r}")
    occurrence = safe(body, "assemblyContext")
    if occurrence is not None and _box_gap(bbox(copy), bbox(body)) > _FRAME_TOLERANCE_CM:
        manager.transform(copy, occurrence.transform2)
    return copy


# -- The result -----------------------------------------------------------------


def feature_row(feature, health=None):
    """The envelope's ``feature``: what was built and where it landed."""
    row_type = str(safe(feature, "objectType", "")).rsplit("::", 1)[-1] or None
    timeline_object = safe(feature, "timelineObject")
    return {"name": safe(feature, "name"), "type": row_type,
            "component": safe(safe(feature, "parentComponent"), "name"),
            "timeline_index": safe(timeline_object, "index"),
            "health": health or HEALTH_NAMES.get(safe(feature, "healthState"), "unknown")}


def envelope(status, units, dry_run=False, feature=None, bodies=None, baseline_token=None,
             resolved=(), warnings=(), **extra):
    """The shared result of every write; tool-specific keys ride in *extra*."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    return {"status": status, "ok": status in OK_STATUSES, "dry_run": dry_run, "units": unit_labels(units),
            "feature": feature,
            "bodies": bodies or {"touched": [], "created": [], "consumed": [], "outside_target": []},
            "baseline_token": baseline_token, "resolved": list(resolved), "warnings": list(warnings), **extra}


def write_result(payload):
    """An envelope as an MCP result: ``isError`` is ``not ok``, so partial is an error on purpose."""
    return tool_result(payload, not payload["ok"])


def refused(units, refusals, dry_run=False, **extra):
    """An envelope for a call that changed nothing; ``refusals`` keeps every reason."""
    return envelope("refused", units, dry_run=dry_run, refusals=list(refusals), **extra)
