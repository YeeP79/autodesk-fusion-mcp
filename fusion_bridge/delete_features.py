"""Delete timeline items by name, refusing anything ambiguous or depended on.

Deleting bodies does not stick in a parametric design (they regenerate from
their features), "delete everything after index N" catches unrelated items,
indices shift after every delete, and the same name can exist in several
components. So this tool deletes timeline objects resolved by name, scoped to
the active design, checks what later items consume them, deletes latest-first
and re-resolves each item by name and entityToken right before deleting it.

Dependency detection (best effort; Fusion has no public "dependents" API):
  * detected: feature inputs that come from a target's output, read from the
    feature's input properties (profiles, paths, rails, loft sections, input
    entities/faces/bodies, combine target/tool bodies, participant bodies,
    fillet/chamfer edge sets, split tools, mirror planes, to-entity extents),
    sketch reference planes/faces, construction geometry definitions and joint
    geometry. Profiles and sketch curves map to their sketch; faces, edges and
    vertices map to their body.
  * body lineage: edge sets, participant bodies and combine target/tool bodies
    only read with the marker right before the feature, so with the marker at
    the end they raise or come back empty. A feature's output (bodies, and the
    bodies of its faces) still reads, so any later item that outputs a body
    counts as using it. Fusion removes such items along with the body's creator.
  * strong: the consumed thing is a sketch or construction geometry the target
    is, or a body the target is the first timeline item to produce (it creates
    the body). Strong dependents block the call unless include_dependents.
  * weak: the consumed body was created earlier and only modified by the
    target (a fillet, a join). Reported as possibly_affected, never deleted:
    the body survives, only references to the target's own faces/edges break.
  * unresolved: a later feature nothing settles, also possibly_affected with a
    reason: its inputs could not be read and lineage does not cover them, it
    works on a body with no traceable creator, or an item being deleted reports
    no bodies, so what it created cannot be followed.
  * not detected: face/edge-level references to faces a modifying feature
    created, sketch geometry projected from bodies or other sketches, and
    parameters/expressions. Fusion's TimelineObject.isDeletable is used as a
    net: an item Fusion will not delete but for which nothing was detected is
    refused. It does not catch cascades: Fusion deletes a body's creator and
    its dependents without complaint.

The plan is still an estimate, so a real delete diffs the timeline before and
after and reports anything that went unplanned as also_removed.
"""

import collections
import difflib
import traceback

import adsk.core
import adsk.fusion

from .common.api import items, safe
from .common.args import bool_arg, error_pair, require_object, text_arg
from .common.results import error_result, tool_result
from .dispatch import get_app, log

# Feature/sketch/joint properties that hold inputs. Feature.bodies is output.
INPUT_ATTRS = (
    "profile", "profiles", "path", "guideRail", "loftSections", "centerLineOrRails",
    "inputEntities", "inputFaces", "inputBodies", "toolBodies", "targetBody",
    "participantBodies", "edgeSets", "edges", "splittingTool", "splitBodies",
    "mirrorPlane", "facesToSplit", "stitchSurfaces", "trimTool", "tools",
    "sourceEntities", "extentOne", "extentTwo", "extentDefinition", "startExtent",
    "referencePlane", "definition", "geometryOrOriginOne", "geometryOrOriginTwo",
)
# Properties followed when unwrapping a reference object (path entities, loft
# sections, extent definitions, construction definitions, joint geometry).
_NESTED_ATTRS = (
    "entity", "edges", "entityOne", "entityTwo", "planarEntity", "linearEntity",
    "circularEntity", "cylindricalFace", "edge", "face", "pointEntity", "point",
    "pointOne", "pointTwo", "pointThree", "path",
)
_TOPOLOGY_TYPES = {"BRepFace", "BRepEdge", "BRepVertex", "BRepLoop", "BRepCoEdge", "BRepShell", "BRepLump"}
_CONSTRUCTION_TYPES = {"ConstructionPlane", "ConstructionAxis", "ConstructionPoint"}
# Inputs that only name bodies, faces or edges: body lineage stands in for them
# when they cannot be read. A sketch profile that cannot be read has no stand-in.
_BODY_INPUT_ATTRS = {
    "edgeSets", "edges", "inputFaces", "inputBodies", "toolBodies", "targetBody",
    "participantBodies", "splitBodies", "facesToSplit",
}
# Features that change bodies something earlier created.
_MODIFY_ONLY_TYPES = {"FilletFeature", "ChamferFeature", "ShellFeature", "DraftFeature", "CombineFeature"}
# Features with no inputs beyond bodies, so a known output body settles them.
_BODY_ONLY_TYPES = _MODIFY_ONLY_TYPES | {"BaseFeature"}
_MODIFY_OPERATIONS = ("JoinFeatureOperation", "CutFeatureOperation", "IntersectFeatureOperation")
# A feature's faces usually sit on one or two bodies; a sample finds them.
_MAX_FACES_READ = 64
_MAX_DEPTH = 5
HEALTH_STATES = ("healthy", "warning", "error")


# -- Small helpers -------------------------------------------------------------


def short_type(obj):
    """'adsk::fusion::ExtrudeFeature' -> 'ExtrudeFeature'."""
    object_type = safe(obj, "objectType")
    if not isinstance(object_type, str) or not object_type:
        return type(obj).__name__ if obj is not None else "None"
    return object_type.rsplit("::", 1)[-1]


def _native(obj):
    native = safe(obj, "nativeObject")
    return native if native is not None else obj


def _read(obj, name, errors):
    """Like ``safe``, but record a read that raised: Fusion raises for inputs it cannot serve."""
    try:
        return getattr(obj, name, None)
    except Exception:
        errors.append(name)
        return None


# -- Argument validation -------------------------------------------------------


@error_pair
def validate_arguments(arguments):
    """Return (params, error_text)."""
    require_object(arguments)
    names = arguments.get("names")
    if not isinstance(names, list) or not names:
        raise ValueError("'names' must be a non-empty list of timeline item names")
    if not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("every entry in 'names' must be a non-empty string")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"'names' contains duplicates: {duplicates}")
    return {
        "names": list(names),
        "component": text_arg(arguments, "component", suffix=" when given"),
        "include_dependents": bool_arg(arguments, "include_dependents"),
        "dry_run": bool_arg(arguments, "dry_run", True),
    }


# -- Pure planning logic (entries are plain dicts) -----------------------------
#
# An entry: position (walk order), index (Timeline index, -1 inside a collapsed
# group), name, entity_type, component, referenced (inside/inserting a linked
# component), in_collapsed_group, deletable (TimelineObject.isDeletable or None),
# health, token, produces (keys: what it outputs, including bodies it only
# modifies), consumes (keys), inputs_unreadable (bool: nothing settles what it
# operates on), and optionally is_feature, modifies_only (it never creates a
# body) and bodies_unknown (it should create a body but reports none).


def describe(entry):
    return {"index": entry["index"], "name": entry["name"],
            "entity_type": entry["entity_type"], "component": entry["component"]}


def resolve_targets(entries, names, component=None):
    """Resolve each name to exactly one entry. Returns (targets, problems).

    problems is empty on success; otherwise it maps name -> {"reason", "candidates"}.
    """
    local = [e for e in entries if not e["referenced"]]
    if component is not None:
        scoped = [e for e in local if e["component"] == component]
    else:
        scoped = local
    targets, problems = [], {}
    for name in names:
        matches = [e for e in scoped if e["name"] == name]
        if len(matches) == 1:
            targets.append(matches[0])
            continue
        if len(matches) > 1:
            problems[name] = {"reason": "ambiguous", "candidates": [describe(e) for e in matches]}
            continue
        linked = [e for e in entries if e["referenced"] and e["name"] == name]
        elsewhere = [e for e in local if e["name"] == name]
        if linked and not elsewhere:
            reason = "only in linked (external) components, which are never deleted"
            candidates = linked
        elif elsewhere:
            reason = f"not in component {component!r}"
            candidates = elsewhere
        else:
            reason = "not found"
            lowered = name.casefold()
            close = set(difflib.get_close_matches(name, [e["name"] for e in scoped], n=5, cutoff=0.6))
            candidates = [e for e in scoped if e["name"] in close or e["name"].casefold() == lowered][:10]
        problems[name] = {"reason": reason, "candidates": [describe(e) for e in candidates]}
    return targets, problems


def creators(entries):
    """Map key -> position of the first entry producing it (its creator)."""
    first = {}
    for entry in sorted(entries, key=lambda e: e["position"]):
        for key in entry["produces"]:
            first.setdefault(key, entry["position"])
    return first


def find_dependents(entries, target_positions):
    """Transitive dependents of the targets.

    Returns (strong, weak): strong maps dependent position -> list of
    {"on": position, "via": key}; weak maps position -> list of the same, for
    items that use a body a target modified but did not create. An item uses
    what it consumes and also what it outputs: a fillet's edges may be
    unreadable, but its bodies say which body it works on.
    """
    by_position = {e["position"]: e for e in entries}
    created_by = creators(entries)
    strong, weak = {}, {}
    frontier = sorted(set(target_positions))
    seen = set(frontier)
    while frontier:
        next_frontier = []
        for source_pos in frontier:
            source = by_position[source_pos]
            strong_keys = {k for k in source["produces"] if created_by.get(k) == source_pos}
            weak_keys = set(source["produces"]) - strong_keys
            for entry in entries:
                if entry["position"] <= source_pos:
                    continue
                uses = set(entry["consumes"]) | set(entry["produces"])
                hit = sorted(strong_keys & uses)
                if hit:
                    strong.setdefault(entry["position"], []).extend({"on": source_pos, "via": k} for k in hit)
                    if entry["position"] not in seen:
                        seen.add(entry["position"])
                        next_frontier.append(entry["position"])
                    continue
                soft = sorted(weak_keys & uses)
                if soft:
                    weak.setdefault(entry["position"], []).extend({"on": source_pos, "via": k} for k in soft)
        frontier = next_frontier
    for position in strong:
        weak.pop(position, None)
    for position in target_positions:
        strong.pop(position, None)
        weak.pop(position, None)
    return strong, weak


def unresolved_items(entries, removed_positions, classified):
    """Later features the plan cannot rule out. Returns position -> reason.

    *removed_positions* are the targets and their dependents; *classified* is
    every position already reported, which is skipped.
    """
    if not removed_positions:
        return {}
    first = min(removed_positions)
    by_position = {e["position"]: e for e in entries}
    # A body first seen on a feature that cannot create one lost its creator.
    # Only a creator at or after the first target could be one being deleted.
    orphans = {key for key, position in creators(entries).items()
               if position > first and by_position[position].get("modifies_only")}
    blind = [by_position[p]["name"] for p in sorted(removed_positions) if by_position[p].get("bodies_unknown")]
    found = {}
    for entry in entries:
        position = entry["position"]
        if position <= first or position in classified or position in removed_positions:
            continue
        if entry["inputs_unreadable"]:
            found[position] = "its inputs could not be read with the marker at the end"
        elif orphans & (set(entry["consumes"]) | set(entry["produces"])):
            found[position] = "it works on a body whose creating feature could not be traced"
        elif blind and entry.get("is_feature"):
            found[position] = f"{', '.join(blind)} reports no bodies, so what it created could not be followed"
    return found


def build_plan(entries, targets, include_dependents, labels=None):
    """Return (plan, refusal). plan is a dict ready for JSON; refusal a dict or None."""
    labels = labels or {}
    by_position = {e["position"]: e for e in entries}
    target_positions = [t["position"] for t in targets]
    strong, weak = find_dependents(entries, target_positions)

    def reasons(links):
        return [{"on": by_position[link["on"]]["name"], "via": labels.get(link["via"], link["via"])}
                for link in links]

    dependents = [dict(describe(by_position[p]), consumes=reasons(strong[p])) for p in sorted(strong)]
    unresolved = unresolved_items(entries, set(target_positions) | set(strong), set(weak))
    possibly_affected = [dict(describe(by_position[p]), consumes=reasons(weak[p])) for p in weak]
    possibly_affected += [dict(describe(by_position[p]), consumes=[], reason=why) for p, why in unresolved.items()]
    possibly_affected.sort(key=lambda item: (item["index"], item["name"]))
    delete_positions = set(target_positions) | (set(strong) if include_dependents else set())
    order = sorted(delete_positions, reverse=True)

    plan = {
        "positions": order,
        "delete_order": [dict(describe(by_position[p]), role="requested" if p in target_positions else "dependent")
                         for p in order],
        "dependents": dependents,
        "possibly_affected": possibly_affected,
    }

    blockers = []
    if dependents and not include_dependents:
        blockers.append("later timeline items consume the targets; pass include_dependents=true to delete them too")
    collapsed = [describe(by_position[p]) for p in order if by_position[p]["in_collapsed_group"]]
    if collapsed:
        blockers.append("items inside a collapsed timeline group cannot be deleted standalone; expand the group first")
        plan["in_collapsed_group"] = collapsed
    # Fusion refuses items with dependents. An item that nothing in the plan
    # depends on but that Fusion still will not delete has dependents we missed.
    planned_on = {link["on"] for p in order if p in strong for link in strong[p]}
    undetected = [describe(by_position[p]) for p in order
                  if by_position[p]["deletable"] is False and p not in planned_on
                  and not by_position[p]["in_collapsed_group"]]
    if undetected:
        blockers.append("Fusion reports these items are not deletable (dependents this tool could not identify)")
        plan["not_deletable"] = undetected
    if blockers:
        return plan, {"error": "; ".join(blockers), **plan}
    return plan, None


def match_for_delete(entries, planned):
    """Find the single current entry for a planned item by name/component/type.

    Returns (entry, error). Token confirmation happens in the Fusion layer.
    """
    matches = [e for e in entries if not e["referenced"] and e["name"] == planned["name"]
               and e["component"] == planned["component"] and e["entity_type"] == planned["entity_type"]]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"{planned['name']!r} is no longer in the timeline"
    return None, f"{planned['name']!r} now matches {len(matches)} timeline items"


def health_summary(entries):
    counts = {state: 0 for state in HEALTH_STATES}
    errors = []
    for entry in entries:
        if entry["health"] in counts:
            counts[entry["health"]] += 1
        if entry["health"] == "error":
            errors.append(dict(describe(entry), message=entry.get("message", "")))
    return counts, errors


def _ident(item):
    return item["name"], item["component"], item["entity_type"]


def new_errors(before, after):
    """Error items in *after* that were not in error in *before* (by name/component/type)."""
    previous = {_ident(e) for e in before}
    return [e for e in after if _ident(e) not in previous]


def removed_items(before, after):
    """Entries of *before* no longer in *after*.

    An item survives if its entityToken is still there, or failing that its
    name/component/type: tokens for one entity can differ between reads, so a
    token miss alone is not a removal. Equal names are counted, not collapsed.
    """
    tokens = {e["token"] for e in after if e["token"]}
    unclaimed = collections.Counter(_ident(e) for e in after)
    unmatched = []
    for entry in before:
        if entry["token"] and entry["token"] in tokens:
            unclaimed[_ident(entry)] -= 1
        else:
            unmatched.append(entry)
    removed = []
    for entry in unmatched:
        if unclaimed[_ident(entry)] > 0:
            unclaimed[_ident(entry)] -= 1
        else:
            removed.append(entry)
    return removed


# -- Reading the timeline from Fusion ------------------------------------------


class EntityKeys:
    """Assign stable string keys to Fusion objects by API equality.

    entityToken strings for the same entity can differ, so compare objects with
    == (Base.__eq__ compares the underlying entity).
    """

    def __init__(self):
        self._known = []
        self._by_token = {}
        self.labels = {}

    def key(self, obj, kind):
        obj = _native(obj)
        token = safe(obj, "entityToken")
        if token in self._by_token:
            return self._by_token[token]
        for other, existing in self._known:
            try:
                if other == obj:
                    if token is not None:
                        self._by_token[token] = existing
                    return existing
            except Exception:
                continue
        key = f"{kind}#{len(self._known)}"
        self._known.append((obj, key))
        if token is not None:
            self._by_token[token] = key
        name = safe(obj, "name")
        self.labels[key] = f"{short_type(obj)} {name!r}" if name else short_type(obj)
        return key


def collect_references(obj, keys, out, depth=0, errors=None):
    """Add keys for the sketches, bodies and construction geometry *obj* refers to.

    Nested reads that raise are appended to *errors*.
    """
    errors = [] if errors is None else errors
    if obj is None or depth > _MAX_DEPTH or isinstance(obj, (str, int, float, bool)):
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            collect_references(item, keys, out, depth + 1, errors)
        return
    kind = short_type(obj)
    if kind.startswith("BRepBody"):
        out.add(keys.key(obj, "body"))
    elif kind.replace("Proxy", "") in _TOPOLOGY_TYPES:
        body = safe(obj, "body")
        if body is None:
            body = safe(safe(obj, "face"), "body")
        collect_references(body, keys, out, depth + 1, errors)
    elif kind == "Sketch":
        out.add(keys.key(obj, "sketch"))
    elif kind.replace("Proxy", "") in _CONSTRUCTION_TYPES:
        out.add(keys.key(obj, "construction"))
    elif safe(obj, "parentSketch") is not None:
        out.add(keys.key(obj.parentSketch, "sketch"))
    elif items(obj):
        for item in items(obj):
            collect_references(item, keys, out, depth + 1, errors)
    else:
        for name in _NESTED_ATTRS:
            collect_references(_read(obj, name, errors), keys, out, depth + 1, errors)


def read_outputs(entity, keys):
    """Keys of the bodies *entity* outputs: its bodies and the bodies of its faces.

    Unlike a feature's inputs these read with the marker at the end. Feature.bodies
    is created or modified bodies; faces can miss some, so both are used.
    """
    out = set()
    for body in items(safe(entity, "bodies")):
        out.add(keys.key(body, "body"))
    for face in items(safe(entity, "faces"), limit=_MAX_FACES_READ):
        collect_references(face, keys, out)
    return out


def read_inputs(entity, keys):
    """Return (consumed keys, names of the input properties that raised)."""
    consumes, failed = set(), set()
    for name in INPUT_ATTRS:
        errors = []
        collect_references(_read(entity, name, errors), keys, consumes, errors=errors)
        if errors:
            failed.add(name)
    return consumes, failed


def inputs_unresolved(kind, failed, consumes, produces, modifies_only=False):
    """True when nothing read settles what this timeline item operates on."""
    if not kind.endswith("Feature"):
        return bool(failed)
    if failed - _BODY_INPUT_ATTRS:
        return True
    if not produces and (failed or modifies_only or kind in _BODY_ONLY_TYPES):
        return True
    return not consumes and kind not in _BODY_ONLY_TYPES


def _modifies_only(entity, kind):
    """True for a feature that changes existing bodies and never creates one."""
    if kind in _MODIFY_ONLY_TYPES:
        return True
    operations = getattr(adsk.fusion, "FeatureOperations", None)
    operation = safe(entity, "operation")
    if operations is None or operation is None:
        return False
    return any(operation == getattr(operations, name, object()) for name in _MODIFY_OPERATIONS)


def _health(timeline_object):
    states = getattr(adsk.fusion, "FeatureHealthStates", None)
    state = safe(timeline_object, "healthState")
    if states is None or state is None:
        return "unknown"
    if state == getattr(states, "HealthyFeatureHealthState", object()):
        return "healthy"
    if state == getattr(states, "WarningFeatureHealthState", object()):
        return "warning"
    if state == getattr(states, "ErrorFeatureHealthState", object()):
        return "error"
    return "unknown"


def _referenced_component_ids(design):
    ids = set()
    for occurrence in items(safe(safe(design, "rootComponent"), "allOccurrences")):
        if safe(occurrence, "isReferencedComponent"):
            component = safe(occurrence, "component")
            ids.add(safe(component, "id"))
            for child in items(safe(component, "allOccurrences")):
                ids.add(safe(safe(child, "component"), "id"))
    ids.discard(None)
    return ids


def _owning_component(entity, root):
    if entity is None:
        return None
    if short_type(entity).startswith("Occurrence"):
        return safe(entity, "sourceComponent") or root
    return safe(entity, "parentComponent") or safe(entity, "component")


def _walk(timeline):
    """Yield (timeline_object, in_collapsed_group) in timeline order."""
    for obj in items(timeline):
        if safe(obj, "isGroup"):
            group = adsk.fusion.TimelineGroup.cast(obj) if hasattr(adsk.fusion, "TimelineGroup") else obj
            for child in items(group):
                yield child, True
        else:
            yield obj, False


def snapshot(design, with_links=True):
    """Read the timeline into entries; returns (entries, handles, keys).

    handles maps position -> (TimelineObject, entity).
    """
    keys = EntityKeys()
    referenced_ids = _referenced_component_ids(design)
    root = safe(design, "rootComponent")
    entries, handles = [], {}
    for position, (obj, collapsed) in enumerate(_walk(design.timeline)):
        entity = safe(obj, "entity")
        component = _owning_component(entity, root)
        referenced = bool(safe(component, "id") in referenced_ids or
                          (short_type(entity).startswith("Occurrence") and safe(entity, "isReferencedComponent")))
        produces, consumes, unreadable = set(), set(), False
        kind = short_type(entity) if entity is not None else ""
        is_feature = kind.endswith("Feature")
        modifies_only = False
        if with_links and entity is not None:
            try:
                if kind == "Sketch":
                    produces.add(keys.key(entity, "sketch"))
                elif kind in _CONSTRUCTION_TYPES:
                    produces.add(keys.key(entity, "construction"))
                produces |= read_outputs(entity, keys)
                consumes, failed = read_inputs(entity, keys)
                modifies_only = _modifies_only(entity, kind)
                unreadable = inputs_unresolved(kind, failed, consumes, produces, modifies_only)
            except Exception:
                unreadable = True
        deletable = safe(obj, "isDeletable")
        entries.append({
            "position": position,
            "index": safe(obj, "index") if isinstance(safe(obj, "index"), int) else -1,
            "name": safe(obj, "name") or "",
            "entity_type": short_type(entity) if entity is not None else "Unknown",
            "component": safe(component, "name"),
            "referenced": referenced,
            "in_collapsed_group": collapsed,
            "deletable": deletable if isinstance(deletable, bool) else None,
            "health": _health(obj),
            "message": safe(obj, "errorOrWarningMessage") or "",
            "token": safe(entity, "entityToken"),
            "produces": sorted(produces),
            "consumes": sorted(consumes),
            "inputs_unreadable": unreadable,
            "is_feature": is_feature,
            "modifies_only": modifies_only,
            "bodies_unknown": with_links and is_feature and not modifies_only and not produces,
        })
        handles[position] = (obj, entity)
    return entries, handles, keys


def _same_entity(design, token, entity):
    """Confirm *entity* is the one *token* named at planning time."""
    if token is None:
        return True
    if safe(entity, "entityToken") == token:
        return True
    try:
        return any(found == entity for found in design.findEntityByToken(token) or [])
    except Exception:
        return False


def _gone(design, token):
    if token is None:
        return None
    try:
        return not any(safe(found, "isValid") for found in design.findEntityByToken(token) or [])
    except Exception:
        return None


def _delete(timeline_object, entity):
    if hasattr(timeline_object, "deleteObject"):
        return bool(timeline_object.deleteObject())
    return bool(entity.deleteMe())


def _warn(result, text):
    earlier = result.get("warning")
    result["warning"] = f"{text} {earlier}" if earlier else text


# -- Tool handler ----------------------------------------------------------------


def delete_features(arguments):
    """Plan (dry_run, default) or perform deletion of named timeline items."""
    params, problem = validate_arguments(arguments)
    if problem:
        return error_result(problem)
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        direct = getattr(getattr(adsk.fusion, "DesignTypes", None), "DirectDesignType", None)
        if direct is not None and design.designType == direct:
            return error_result("Error: the active design has no timeline (direct modeling)")
        timeline = design.timeline
        if timeline.markerPosition < timeline.count:
            return error_result("Error: the timeline marker is rolled back; move it to the end before deleting "
                          "(inputs of rolled-back items cannot be inspected)")

        entries, handles, keys = snapshot(design)
        targets, problems = resolve_targets(entries, params["names"], params["component"])
        if problems:
            return error_result({"error": "every name must resolve to exactly one timeline item; nothing was deleted",
                           "unresolved": problems})
        plan, refusal = build_plan(entries, targets, params["include_dependents"], keys.labels)
        counts_before, errors_before = health_summary(entries)
        if refusal:
            refusal.pop("positions", None)
            refusal["note"] = "nothing was deleted"
            return error_result(refusal)

        order = plan.pop("positions")
        by_position = {e["position"]: e for e in entries}
        result = {"document": app.activeDocument.name if app.activeDocument else None,
                  "dry_run": params["dry_run"], **plan, "timeline_health_before": counts_before}
        if errors_before:
            result["warning"] = ("the timeline already has errors; Fusion may roll back API edits while "
                                 "features are broken")
        if params["dry_run"]:
            result["note"] = "dry run: nothing was deleted; call again with dry_run=false to delete in this order"
            return tool_result(result)

        deleted, failure = [], None
        for planned, position in zip(plan["delete_order"], order):
            # Indices shift after every delete: re-read and re-resolve each time.
            current, current_handles, _ = snapshot(design, with_links=False)
            entry, failure = match_for_delete(current, planned)
            if entry is None:
                break
            obj, entity = current_handles[entry["position"]]
            token = by_position[position]["token"]
            if not _same_entity(design, token, entity):
                failure = f"{planned['name']!r} no longer refers to the planned entity (entityToken changed)"
                break
            if safe(obj, "isDeletable") is False:
                failure = f"Fusion refused to delete {planned['name']!r}: it still has dependents"
                break
            # A raise here must still reach the timeline diff below.
            try:
                if not _delete(obj, entity):
                    failure = f"Fusion did not delete {planned['name']!r}"
            except Exception as exc:
                failure = f"deleting {planned['name']!r} raised: {exc}"
            if failure:
                break
            deleted.append(dict(planned, verified_gone=_gone(design, token)))
            log(f"[MCP] delete_features: deleted {planned['name']!r}")

        after, _, _ = snapshot(design, with_links=False)
        counts_after, errors_after = health_summary(after)
        planned_positions = set(order)
        also_removed = [describe(e) for e in removed_items(entries, after) if e["position"] not in planned_positions]
        result.update({
            "also_removed": also_removed,
            "timeline_items": {"before": len(entries), "after": len(after)},
            "deleted": deleted,
            "timeline_health": counts_after,
            "timeline_errors": errors_after,
            "new_errors": new_errors(errors_before, errors_after),
        })
        if failure:
            remaining = plan["delete_order"][len(deleted):]
            result["stopped"] = failure
            result["not_deleted"] = remaining
        still_there = any(item["verified_gone"] is False for item in deleted)
        if still_there:
            _warn(result, "some deleted items still resolve by entityToken; check Fusion (Ctrl+Z undoes).")
        if also_removed:
            names = ", ".join(repr(item["name"]) for item in also_removed)
            _warn(result, f"FUSION ALSO REMOVED {len(also_removed)} ITEM(S) THAT WERE NOT IN THE PLAN: {names}. "
                          "Check them (Ctrl+Z undoes).")
            log(f"[MCP] delete_features: Fusion also removed {names}")
            # Lead the response with it: the plan said otherwise.
            result = {"warning": result.pop("warning"), "also_removed": result.pop("also_removed"), **result}
        return tool_result(result, is_error=bool(failure) or still_there or bool(also_removed))
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] delete_features failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")

