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
  * strong: the consumed thing is a sketch or construction geometry the target
    is, or a body the target is the first timeline item to produce (it creates
    the body). Strong dependents block the call unless include_dependents.
  * weak: the consumed body was created earlier and only modified by the
    target (a fillet, a join). Reported as possibly_affected, never deleted:
    the body survives, only references to the target's own faces/edges break.
  * not detected: face/edge-level references to faces a modifying feature
    created, sketch geometry projected from bodies or other sketches,
    parameters/expressions, and anything whose inputs cannot be read with the
    marker at the end. Fusion's TimelineObject.isDeletable is used as a net:
    an item Fusion will not delete but for which nothing was detected is refused.
"""

import difflib
import json
import traceback

import adsk.core
import adsk.fusion

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
_MAX_DEPTH = 5
HEALTH_STATES = ("healthy", "warning", "error")


# -- Small helpers -------------------------------------------------------------


def _error(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _ok(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": is_error}


def _attr(obj, name):
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def short_type(obj):
    """'adsk::fusion::ExtrudeFeature' -> 'ExtrudeFeature'."""
    object_type = _attr(obj, "objectType")
    if not isinstance(object_type, str) or not object_type:
        return type(obj).__name__ if obj is not None else "None"
    return object_type.rsplit("::", 1)[-1]


def _native(obj):
    native = _attr(obj, "nativeObject")
    return native if native is not None else obj


def _items(collection):
    count = _attr(collection, "count")
    if not isinstance(count, int) or isinstance(count, bool):
        return []
    out = []
    for i in range(count):
        try:
            out.append(collection.item(i))
        except Exception:
            pass
    return out


# -- Argument validation -------------------------------------------------------


def validate_arguments(arguments):
    """Return (params, error_text)."""
    if not isinstance(arguments, dict):
        return None, "Error: arguments must be an object"
    names = arguments.get("names")
    if not isinstance(names, list) or not names:
        return None, "Error: 'names' must be a non-empty list of timeline item names"
    if not all(isinstance(n, str) and n.strip() for n in names):
        return None, "Error: every entry in 'names' must be a non-empty string"
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        return None, f"Error: 'names' contains duplicates: {duplicates}"
    component = arguments.get("component")
    if component is not None and (not isinstance(component, str) or not component.strip()):
        return None, "Error: 'component' must be a non-empty string when given"
    params = {"names": list(names), "component": component}
    for key, default in (("include_dependents", False), ("dry_run", True)):
        value = arguments.get(key, default)
        if not isinstance(value, bool):
            return None, f"Error: '{key}' must be a boolean"
        params[key] = value
    return params, None


# -- Pure planning logic (entries are plain dicts) -----------------------------
#
# An entry: position (walk order), index (Timeline index, -1 inside a collapsed
# group), name, entity_type, component, referenced (inside/inserting a linked
# component), in_collapsed_group, deletable (TimelineObject.isDeletable or None),
# health, token, produces (keys), consumes (keys), inputs_unreadable (bool).


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
    items that consume a body a target modified but did not create.
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
                hit = sorted(strong_keys & set(entry["consumes"]))
                if hit:
                    strong.setdefault(entry["position"], []).extend({"on": source_pos, "via": k} for k in hit)
                    if entry["position"] not in seen:
                        seen.add(entry["position"])
                        next_frontier.append(entry["position"])
                    continue
                soft = sorted(weak_keys & set(entry["consumes"]))
                if soft:
                    weak.setdefault(entry["position"], []).extend({"on": source_pos, "via": k} for k in soft)
        frontier = next_frontier
    for position in strong:
        weak.pop(position, None)
    for position in target_positions:
        strong.pop(position, None)
        weak.pop(position, None)
    return strong, weak


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
    possibly_affected = [dict(describe(by_position[p]), consumes=reasons(weak[p])) for p in sorted(weak)]
    delete_positions = set(target_positions) | (set(strong) if include_dependents else set())
    order = sorted(delete_positions, reverse=True)

    unreadable = [describe(e) for e in entries
                  if e["inputs_unreadable"] and e["position"] > min(target_positions, default=-1)]
    plan = {
        "positions": order,
        "delete_order": [dict(describe(by_position[p]), role="requested" if p in target_positions else "dependent")
                         for p in order],
        "dependents": dependents,
        "possibly_affected": possibly_affected,
        "uninspected_later_items": unreadable,
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


def new_errors(before, after):
    """Error items in *after* that were not in error in *before* (by name/component/type)."""
    def ident(item):
        return item["name"], item["component"], item["entity_type"]
    previous = {ident(e) for e in before}
    return [e for e in after if ident(e) not in previous]


# -- Reading the timeline from Fusion ------------------------------------------


class EntityKeys:
    """Assign stable string keys to Fusion objects by API equality.

    entityToken strings for the same entity can differ, so compare objects with
    == (Base.__eq__ compares the underlying entity).
    """

    def __init__(self):
        self._known = []
        self.labels = {}

    def key(self, obj, kind):
        obj = _native(obj)
        for other, existing in self._known:
            try:
                if other == obj:
                    return existing
            except Exception:
                continue
        key = f"{kind}#{len(self._known)}"
        self._known.append((obj, key))
        name = _attr(obj, "name")
        self.labels[key] = f"{short_type(obj)} {name!r}" if name else short_type(obj)
        return key


def collect_references(obj, keys, out, depth=0):
    """Add keys for the sketches, bodies and construction geometry *obj* refers to."""
    if obj is None or depth > _MAX_DEPTH or isinstance(obj, (str, int, float, bool)):
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            collect_references(item, keys, out, depth + 1)
        return
    kind = short_type(obj)
    if kind.startswith("BRepBody"):
        out.add(keys.key(obj, "body"))
    elif kind.replace("Proxy", "") in _TOPOLOGY_TYPES:
        body = _attr(obj, "body")
        if body is None:
            body = _attr(_attr(obj, "face"), "body")
        collect_references(body, keys, out, depth + 1)
    elif kind == "Sketch":
        out.add(keys.key(obj, "sketch"))
    elif kind.replace("Proxy", "") in _CONSTRUCTION_TYPES:
        out.add(keys.key(obj, "construction"))
    elif _attr(obj, "parentSketch") is not None:
        out.add(keys.key(obj.parentSketch, "sketch"))
    elif _items(obj):
        for item in _items(obj):
            collect_references(item, keys, out, depth + 1)
    else:
        for name in _NESTED_ATTRS:
            collect_references(_attr(obj, name), keys, out, depth + 1)


def _health(timeline_object):
    states = getattr(adsk.fusion, "FeatureHealthStates", None)
    state = _attr(timeline_object, "healthState")
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
    for occurrence in _items(_attr(_attr(design, "rootComponent"), "allOccurrences")):
        if _attr(occurrence, "isReferencedComponent"):
            component = _attr(occurrence, "component")
            ids.add(_attr(component, "id"))
            for child in _items(_attr(component, "allOccurrences")):
                ids.add(_attr(_attr(child, "component"), "id"))
    ids.discard(None)
    return ids


def _owning_component(entity, root):
    if entity is None:
        return None
    if short_type(entity).startswith("Occurrence"):
        return _attr(entity, "sourceComponent") or root
    return _attr(entity, "parentComponent") or _attr(entity, "component")


def _walk(timeline):
    """Yield (timeline_object, in_collapsed_group) in timeline order."""
    for obj in _items(timeline):
        if _attr(obj, "isGroup"):
            group = adsk.fusion.TimelineGroup.cast(obj) if hasattr(adsk.fusion, "TimelineGroup") else obj
            for child in _items(group):
                yield child, True
        else:
            yield obj, False


def snapshot(design, with_links=True):
    """Read the timeline into entries; returns (entries, handles, keys).

    handles maps position -> (TimelineObject, entity).
    """
    keys = EntityKeys()
    referenced_ids = _referenced_component_ids(design)
    root = _attr(design, "rootComponent")
    entries, handles = [], {}
    for position, (obj, collapsed) in enumerate(_walk(design.timeline)):
        entity = _attr(obj, "entity")
        component = _owning_component(entity, root)
        referenced = bool(_attr(component, "id") in referenced_ids or
                          (short_type(entity).startswith("Occurrence") and _attr(entity, "isReferencedComponent")))
        produces, consumes, unreadable = set(), set(), False
        if with_links and entity is not None:
            try:
                kind = short_type(entity)
                if kind == "Sketch":
                    produces.add(keys.key(entity, "sketch"))
                elif kind in _CONSTRUCTION_TYPES:
                    produces.add(keys.key(entity, "construction"))
                for body in _items(_attr(entity, "bodies")):
                    produces.add(keys.key(body, "body"))
                for name in INPUT_ATTRS:
                    collect_references(_attr(entity, name), keys, consumes)
            except Exception:
                unreadable = True
        deletable = _attr(obj, "isDeletable")
        entries.append({
            "position": position,
            "index": _attr(obj, "index") if isinstance(_attr(obj, "index"), int) else -1,
            "name": _attr(obj, "name") or "",
            "entity_type": short_type(entity) if entity is not None else "Unknown",
            "component": _attr(component, "name"),
            "referenced": referenced,
            "in_collapsed_group": collapsed,
            "deletable": deletable if isinstance(deletable, bool) else None,
            "health": _health(obj),
            "message": _attr(obj, "errorOrWarningMessage") or "",
            "token": _attr(entity, "entityToken"),
            "produces": sorted(produces),
            "consumes": sorted(consumes),
            "inputs_unreadable": unreadable,
        })
        handles[position] = (obj, entity)
    return entries, handles, keys


def _same_entity(design, token, entity):
    """Confirm *entity* is the one *token* named at planning time."""
    if token is None:
        return True
    if _attr(entity, "entityToken") == token:
        return True
    try:
        return any(found == entity for found in design.findEntityByToken(token) or [])
    except Exception:
        return False


def _gone(design, token):
    if token is None:
        return None
    try:
        return not any(_attr(found, "isValid") for found in design.findEntityByToken(token) or [])
    except Exception:
        return None


def _delete(timeline_object, entity):
    if hasattr(timeline_object, "deleteObject"):
        return bool(timeline_object.deleteObject())
    return bool(entity.deleteMe())


# -- Tool handler ----------------------------------------------------------------


def delete_features(arguments):
    """Plan (dry_run, default) or perform deletion of named timeline items."""
    params, problem = validate_arguments(arguments)
    if problem:
        return _error(problem)
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return _error("Error: the active product is not a Fusion design")
        direct = getattr(getattr(adsk.fusion, "DesignTypes", None), "DirectDesignType", None)
        if direct is not None and design.designType == direct:
            return _error("Error: the active design has no timeline (direct modeling)")
        timeline = design.timeline
        if timeline.markerPosition < timeline.count:
            return _error("Error: the timeline marker is rolled back; move it to the end before deleting "
                          "(inputs of rolled-back items cannot be inspected)")

        entries, handles, keys = snapshot(design)
        targets, problems = resolve_targets(entries, params["names"], params["component"])
        if problems:
            return _error({"error": "every name must resolve to exactly one timeline item; nothing was deleted",
                           "unresolved": problems})
        plan, refusal = build_plan(entries, targets, params["include_dependents"], keys.labels)
        counts_before, errors_before = health_summary(entries)
        if refusal:
            refusal.pop("positions", None)
            refusal["note"] = "nothing was deleted"
            return _error(refusal)

        order = plan.pop("positions")
        by_position = {e["position"]: e for e in entries}
        result = {"document": app.activeDocument.name if app.activeDocument else None,
                  "dry_run": params["dry_run"], **plan, "timeline_health_before": counts_before}
        if errors_before:
            result["warning"] = ("the timeline already has errors; Fusion may roll back API edits while "
                                 "features are broken")
        if params["dry_run"]:
            result["note"] = "dry run: nothing was deleted; call again with dry_run=false to delete in this order"
            return _ok(result)

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
            if _attr(obj, "isDeletable") is False:
                failure = f"Fusion refused to delete {planned['name']!r}: it still has dependents"
                break
            if not _delete(obj, entity):
                failure = f"Fusion did not delete {planned['name']!r}"
                break
            deleted.append(dict(planned, verified_gone=_gone(design, token)))
            log(f"[MCP] delete_features: deleted {planned['name']!r}")

        after, _, _ = snapshot(design, with_links=False)
        counts_after, errors_after = health_summary(after)
        result.update({
            "deleted": deleted,
            "timeline_health": counts_after,
            "timeline_errors": errors_after,
            "new_errors": new_errors(errors_before, errors_after),
        })
        if failure:
            remaining = plan["delete_order"][len(deleted):]
            result["stopped"] = failure
            result["not_deleted"] = remaining
        if any(item["verified_gone"] is False for item in deleted):
            result["warning"] = "some deleted items still resolve by entityToken; check Fusion (Ctrl+Z undoes)"
        return _ok(result, is_error=bool(failure) or any(i["verified_gone"] is False for i in deleted))
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] delete_features failed: {exc}")
        return _error(f"Error: {exc}\n{tb}")

