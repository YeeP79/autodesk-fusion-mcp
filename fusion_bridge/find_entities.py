"""Find faces and edges by what they are: find_entities.

Replaces hand-written face and edge hunts. Its refs are the input of every
feature-building tool, so results must not depend on when they were asked for:
a label is built from rounded geometry alone, and rows are ordered by label
because Fusion's face order is not guaranteed.

A scan works cheapest first: surface type and bounding box without describing
the entity, then the pure filters on the record, then the two filters that need
Fusion (``near``'s distance, ``edge_convexity``'s containment) on what is left.
Adjacent faces and creating features are read only for the rows returned.

Dimensions come from geometry, never names. Everything is world coordinates in
the caller's units; a read-only tool, so it has no dry run.
"""

import time
import traceback

import adsk.core
import adsk.fusion

from .common.api import items, safe
from .common.args import (
    bool_arg,
    error_pair,
    int_arg,
    is_finite_number,
    reject_unknown,
    require_object,
    text_arg,
)
from .common.bodies import (
    PARTIAL_SWEEP_DEG,
    SURFACE_TYPES,
    TRAP_NOTE,
    bbox,
    feature_map,
    native,
    parse_body_refs,
    resolve_targets,
)
from .common.entity_refs import (
    CURVE_TYPES,
    REF_TOLERANCE_CM,
    FusionScene,
    adjacent_refs,
    bbox_distance,
    describe_edge,
    describe_face,
    matches_edge,
    matches_face,
    parse_ref,
    public,
    read_edge_convexity,
    resolve,
    resolved_row,
    same_body,
    same_entity,
    unique_entities,
)
from .common.geometry import CM_PER_UNIT, UNIT_FACTORS, to_units, unit, unit_labels
from .common.results import error_result, ok_result
from .common.timeline import find_row, read_rows, timeline_of
from .dispatch import get_app, log
from .probe_geometry import Budget

SURFACE_NAMES = sorted(set(SURFACE_TYPES.values()))
CURVE_NAMES = sorted(set(CURVE_TYPES.values()))
SORT_KEYS = ("distance", "area", "length", "diameter")

DEFAULT_MAX_RESULTS = 50
MAX_RESULTS_LIMIT = 1000
# An unfiltered scan of a body this large is a mistake and a very long reply.
UNFILTERED_FACE_LIMIT = 500
# Entities examined per call; the time budget normally stops a scan first.
SCAN_LIMIT = 500_000

_ARGS = {"entity", "bodies", "include_hidden", "units", "surface_types", "curve_types", "normal", "axis", "diameter",
         "area", "length", "sweep_deg", "concavity", "edge_convexity", "near", "on_plane", "in_box", "of_face",
         "of_feature", "include_created_by", "sort_by", "max_results"}
_FILTERS = ("surface_types", "curve_types", "normal", "axis", "diameter", "area", "length", "sweep_deg", "concavity",
            "edge_convexity", "near", "on_plane", "in_box", "of_face", "of_feature")
_FACE_ONLY = ("area", "concavity")


# -- Argument validation (no Fusion) --------------------------------------------


def _object(arguments, key, known):
    value = arguments[key]
    if not isinstance(value, dict):
        raise ValueError(f"'{key}' must be an object")
    reject_unknown(value, known, f"unknown key(s) in '{key}'")
    return value


def _vector(container, key, where):
    value = container.get(key)
    if not isinstance(value, list) or len(value) != 3 or not all(is_finite_number(c) for c in value):
        raise ValueError(f"'{where}.{key}' must be an array of three finite numbers [x, y, z]")
    return tuple(float(c) for c in value)


def _direction(container, key, where):
    value = unit(_vector(container, key, where))
    if value is None:
        raise ValueError(f"'{where}.{key}' must not be a zero vector")
    return value


def _non_negative(container, key, where, default=None):
    value = container.get(key, default)
    if not is_finite_number(value) or value < 0:
        raise ValueError(f"'{where}.{key}' must be a number of at least 0")
    return float(value)


def _signed(container, where, default):
    value = container.get("signed", default)
    if not isinstance(value, bool):
        raise ValueError(f"'{where}.signed' must be a boolean")
    return value


def _range(arguments, key):
    value = _object(arguments, key, {"min", "max"})
    low, high = value.get("min"), value.get("max")
    if low is None and high is None:
        raise ValueError(f"'{key}' needs 'min', 'max' or both")
    for bound in (low, high):
        if bound is not None and not is_finite_number(bound):
            raise ValueError(f"'{key}' bounds must be finite numbers")
    if low is not None and high is not None and low > high:
        raise ValueError(f"'{key}' is inverted: min {low} is above max {high}")
    return low, high


def _names(arguments, key, allowed):
    value = arguments[key]
    if not isinstance(value, list) or not value or any(v not in allowed for v in value):
        raise ValueError(f"'{key}' must be a non-empty array drawn from {allowed}")
    return set(value)


def _default_tolerance(units):
    return REF_TOLERANCE_CM / CM_PER_UNIT[units]


def _normal_filter(arguments):
    value = _object(arguments, "normal", {"direction", "tolerance_deg", "signed"})
    return {"direction": _direction(value, "direction", "normal"),
            "tolerance_deg": _non_negative(value, "tolerance_deg", "normal", 1.0),
            "signed": _signed(value, "normal", True)}


def _axis_filter(arguments, units):
    value = _object(arguments, "axis", {"direction", "tolerance_deg", "signed", "through", "through_tolerance"})
    wanted = {"direction": _direction(value, "direction", "axis"),
              "tolerance_deg": _non_negative(value, "tolerance_deg", "axis", 1.0),
              "signed": _signed(value, "axis", False)}
    if "through" in value:
        wanted["through"] = _vector(value, "through", "axis")
        wanted["through_tolerance"] = _non_negative(value, "through_tolerance", "axis", _default_tolerance(units))
    elif "through_tolerance" in value:
        raise ValueError("'axis.through_tolerance' needs 'axis.through'")
    return wanted


def _near_filter(arguments):
    value = _object(arguments, "near", {"point", "within"})
    return {"point": _vector(value, "point", "near"), "within": _non_negative(value, "within", "near")}


def _plane_filter(arguments, units):
    value = _object(arguments, "on_plane", {"point", "normal", "tolerance"})
    return {"point": _vector(value, "point", "on_plane"), "normal": _direction(value, "normal", "on_plane"),
            "tolerance": _non_negative(value, "tolerance", "on_plane", _default_tolerance(units))}


def _box_filter(arguments):
    value = _object(arguments, "in_box", {"min", "max"})
    low, high = _vector(value, "min", "in_box"), _vector(value, "max", "in_box")
    if any(a > b for a, b in zip(low, high)):
        raise ValueError("'in_box' is inverted: min is above max")
    return low, high


def _choice(arguments, key, allowed):
    value = arguments[key]
    if value not in allowed:
        raise ValueError(f"'{key}' must be one of {list(allowed)}")
    return value


@error_pair
def validate_args(arguments):
    """Return (options, error_text). ``filters`` holds only what was asked for."""
    require_object(arguments)
    reject_unknown(arguments, _ARGS)
    entity = arguments.get("entity")
    if entity not in ("face", "edge"):
        raise ValueError("'entity' must be 'face' or 'edge'")
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")

    if entity == "edge":
        wrong = [key for key in ("surface_types", *_FACE_ONLY) if key in arguments]
        if wrong:
            raise ValueError(f"{wrong} apply to faces, not edges")
    elif "curve_types" in arguments or "edge_convexity" in arguments:
        raise ValueError("'curve_types' and 'edge_convexity' apply to edges, not faces")

    filters = {}
    if "surface_types" in arguments:
        filters["surface_types"] = _names(arguments, "surface_types", SURFACE_NAMES)
    if "curve_types" in arguments:
        filters["curve_types"] = _names(arguments, "curve_types", CURVE_NAMES)
    if "normal" in arguments:
        filters["normal"] = _normal_filter(arguments)
    if "axis" in arguments:
        filters["axis"] = _axis_filter(arguments, units)
    for key in ("diameter", "area", "length", "sweep_deg"):
        if key in arguments:
            filters[key] = _range(arguments, key)
    if "concavity" in arguments:
        filters["concavity"] = _choice(arguments, "concavity", ("concave", "convex"))
    if "on_plane" in arguments:
        filters["on_plane"] = _plane_filter(arguments, units)
    if "in_box" in arguments:
        filters["in_box"] = _box_filter(arguments)

    sort_by = arguments.get("sort_by")
    if sort_by is not None and sort_by not in SORT_KEYS:
        raise ValueError(f"'sort_by' must be one of {list(SORT_KEYS)}")
    if sort_by == "distance" and "near" not in arguments:
        raise ValueError("'sort_by: distance' needs 'near'")
    if sort_by == "area" and entity == "edge":
        raise ValueError("'sort_by: area' applies to faces, not edges")

    return {
        "entity": entity,
        "units": units,
        "bodies": parse_body_refs(arguments["bodies"]) if "bodies" in arguments else None,
        "include_hidden": bool_arg(arguments, "include_hidden", False),
        "filters": filters,
        "near": _near_filter(arguments) if "near" in arguments else None,
        "edge_convexity": (_choice(arguments, "edge_convexity", ("convex", "concave", "tangent"))
                           if "edge_convexity" in arguments else None),
        "of_face": parse_ref(arguments["of_face"], units, "of_face") if "of_face" in arguments else None,
        "of_feature": text_arg(arguments, "of_feature"),
        "include_created_by": bool_arg(arguments, "include_created_by", False),
        "sort_by": sort_by,
        "max_results": int_arg(arguments, "max_results", DEFAULT_MAX_RESULTS, 1, MAX_RESULTS_LIMIT),
        "unfiltered": not any(key in arguments for key in _FILTERS),
    }


# -- Choosing what to scan ------------------------------------------------------


def choose_bodies(entries, opts):
    """The bodies to scan. Returns (targets, warnings, error_payload).

    Named bodies are scanned hidden or not. Otherwise visible solids only: a
    hidden body is often stale reference geometry.
    """
    if opts["bodies"] is not None:
        return resolve_targets(entries, opts["bodies"])
    targets = [e for e in entries if e["solid"] and (e["visible"] or opts["include_hidden"])]
    hidden = sum(1 for e in entries if e["solid"] and not e["visible"])
    warnings = []
    if hidden and not opts["include_hidden"]:
        warnings.append(f"{hidden} hidden bodies were not scanned (often stale reference geometry); "
                        "pass include_hidden or name them in 'bodies'.")
    return targets, warnings, None


def face_scope(face, targets, entries, opts):
    """Narrow the scan to the body of an ``of_face``. Returns (targets, error_payload)."""
    body = safe(face, "body")
    owner = next((e for e in entries if same_body(body, e)), None)
    if owner is None:
        return None, {"error": "the of_face face is on no body of this design"}
    if opts["bodies"] is not None and not any(owner is t for t in targets):
        return None, {"error": f"the of_face face is on {owner['key']}, which 'bodies' does not name"}
    return [owner], None


def scope_members(face, kind):
    """The edges of *face*, or the faces sharing an edge with it, each once."""
    edges = items(safe(face, "edges"))
    if kind == "edge":
        return edges
    around = unique_entities(other for edge in edges for other in items(safe(edge, "faces")))
    return [other for other in around if not same_entity(other, face)]


def feature_members(design, name, kind):
    """What ``of_feature`` names: {tempId: [native bodies]}. Returns (members, warnings, error_payload).

    Feature.faces holds native faces, and tempIds are unique only within a body,
    so both are kept. Every occurrence of the component then matches.
    """
    timeline = timeline_of(design)
    if timeline is None:
        return None, [], {"error": "of_feature needs a timeline; this is a direct design"}
    rows, objects = read_rows(timeline)
    position, refusal = find_row(rows, name)
    if refusal:
        return None, [], {"error": refusal}
    faces = safe(safe(objects[position], "entity"), "faces")
    if faces is None:
        return None, [], {"error": f"{name!r} is a {rows[position].get('entity_type')}, which has no faces"}
    members = {}
    for face in items(faces):
        for entity in (items(safe(face, "edges")) if kind == "edge" else [face]):
            members.setdefault(safe(entity, "tempId"), []).append(safe(entity, "body"))
    warnings = []
    if any(row["kind"] == "feature" for row in rows[position + 1:]):
        warnings.append(f"{name!r} is not the last feature: a later Combine or cut may have consumed faces "
                        "it made, and those are not listed here.")
    return members, warnings, None


def in_members(rec, entry, members):
    bodies = members.get(rec["_temp_id"])
    if not bodies:
        return False
    mine = native(entry["_body"])
    return any(same_entity(body, mine) for body in bodies)


# -- Scanning -------------------------------------------------------------------


def _quick_reject(entity, opts):
    """Reject on surface type and bounding box, before reading anything else."""
    wanted = opts["filters"].get("surface_types")
    if wanted is not None and SURFACE_TYPES.get(safe(safe(entity, "geometry"), "surfaceType"), "unknown") not in wanted:
        return True
    near = opts["near"]
    if near is not None:
        box = bbox(entity)
        if box is not None:
            cm = CM_PER_UNIT[opts["units"]]
            point = tuple(c * cm for c in near["point"])
            return bbox_distance(box, point) > near["within"] * cm + REF_TOLERANCE_CM
    return False


def _candidate(entity, entry, opts, members, scene):
    """A matching record for *entity*, or None."""
    units, filters = opts["units"], opts["filters"]
    if opts["entity"] == "face":
        rec = describe_face(entity, units, entry)
        if not matches_face(rec, filters):
            return None
    else:
        rec = describe_edge(entity, units, entry, adjacent=False, samples="on_plane" in filters)
        if not matches_edge(rec, filters):
            return None
    if members is not None and not in_members(rec, entry, members):
        return None
    near = opts["near"]
    if near is not None:
        cm = CM_PER_UNIT[units]
        distance = scene.distance(entity, tuple(c * cm for c in near["point"]))
        if distance is None or distance / cm > near["within"]:
            return None
        rec["distance"] = to_units(distance, units)
    if opts["edge_convexity"] is not None:
        rec["convexity"] = read_edge_convexity(entity, entry, scene)
        if rec["convexity"] != opts["edge_convexity"]:
            return None
    rec["_owner"] = entry
    return rec


def scan(targets, opts, scene, budget, members=None, scope=None):
    """Walk the candidates. Returns (records, counts); *scope* replaces a body's own list."""
    records = []
    counts = {"bodies_scanned": 0, "scanned": 0, "scan_capped": False, "unreadable": 0}
    collection = "faces" if opts["entity"] == "face" else "edges"
    for entry in targets:
        if budget.exhausted(counts["scanned"]):
            counts["scan_capped"] = True
            break
        counts["bodies_scanned"] += 1
        for entity in (scope if scope is not None else items(safe(entry["_body"], collection))):
            if budget.exhausted(counts["scanned"]):
                counts["scan_capped"] = True
                break
            counts["scanned"] += 1
            if _quick_reject(entity, opts):
                continue
            try:
                rec = _candidate(entity, entry, opts, members, scene)
            except Exception as exc:
                counts["unreadable"] += 1
                log(f"[MCP] find_entities: skipped an entity: {exc}")
                continue
            if rec is not None:
                records.append(rec)
    return records, counts


def order(records, sort_by):
    """Stable whatever order Fusion listed things in: by label, then body and point.

    ``sort_by`` goes first: nearest for distance, largest for the rest. A row
    without that value goes last.
    """
    def tie_break(rec):
        ref = rec["ref"]
        body = ref.get("body", {})
        return (rec["label"], body.get("occurrence_path", ""), body.get("body_name", ""),
                body.get("body_index", -1), ref.get("point", []))

    def value(rec):
        if sort_by == "length":
            return rec.get("length", rec.get("length_along_axis"))
        return rec.get(sort_by)

    ordered = sorted(records, key=tie_break)
    if sort_by is None:
        return ordered
    known = [r for r in ordered if value(r) is not None]
    unknown = [r for r in ordered if value(r) is None]
    return sorted(known, key=value, reverse=sort_by != "distance") + unknown


def add_creators(records):
    """Fill ``created_by`` on the returned faces; one feature walk per body."""
    by_body = {}
    for rec in records:
        by_body.setdefault(id(rec["_owner"]), []).append(rec)
    notes = []
    for group in by_body.values():
        native_body = native(group[0]["_owner"]["_body"])
        wanted = {r["_temp_id"] for r in group if r["_temp_id"] is not None}
        found, note = feature_map(safe(native_body, "parentComponent"), wanted, native_body)
        if note:
            notes.append(note)
        for rec in group:
            rec["created_by"], rec["created_by_type"] = found.get(rec["_temp_id"], (None, None))
    return notes


def build_result(records, counts, opts, warnings, resolved, started=None):
    """Sort, cap, finish the returned rows and assemble the payload."""
    ordered = order(records, opts["sort_by"])
    returned = ordered[:opts["max_results"]]
    warnings = list(warnings)
    if opts["entity"] == "edge":
        for rec in returned:
            rec["faces"] = adjacent_refs(rec["_entity"], opts["units"], rec["_owner"])
    elif opts["include_created_by"]:
        warnings.extend(add_creators(returned))
    if any(r["type"] == "cylinder" and r.get("sweep_deg") is not None and r["sweep_deg"] < PARTIAL_SWEEP_DEG
           for r in returned):
        warnings.append(TRAP_NOTE)
    if counts["unreadable"]:
        warnings.append(f"{counts['unreadable']} entities could not be read and were skipped.")
    if counts["scan_capped"]:
        warnings.append("The scan stopped at its budget: matches past that point are missing. Narrow 'bodies'.")
    payload = {
        "units": unit_labels(opts["units"]),
        "bodies_scanned": counts["bodies_scanned"],
        "scanned": counts["scanned"],
        "matched": len(ordered),
        "returned": len(returned),
        "truncated": len(ordered) > len(returned),
        "scan_capped": counts["scan_capped"],
        "entities": [public(rec) for rec in returned],
    }
    if resolved:
        payload["resolved"] = resolved
    if warnings:
        payload["warnings"] = warnings
    if started is not None:
        payload["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return payload


def run(design, scene, opts, budget, started=None):
    """Return (payload, error_payload)."""
    entries = scene.entries()
    targets, warnings, problem = choose_bodies(entries, opts)
    if problem:
        return None, problem
    warnings = list(warnings)

    resolved, scope = [], None
    if opts["of_face"] is not None:
        ref = {**opts["of_face"], "kind": opts["of_face"]["kind"] or "face"}
        found = resolve(scene, ref)
        if found.refusal:
            return None, {**found.refusal, "error": f"of_face: {found.refusal['error']}"}
        warnings.extend(found.warnings)
        resolved.append(resolved_row(found, "of_face"))
        targets, problem = face_scope(found.entity, targets, entries, opts)
        if problem:
            return None, problem
        scope = scope_members(found.entity, opts["entity"])

    if opts["unfiltered"]:
        large = [e["key"] for e in targets
                 if (safe(safe(e["_body"], "faces"), "count") or 0) > UNFILTERED_FACE_LIMIT]
        if large:
            return None, {"error": f"no filter on bodies over {UNFILTERED_FACE_LIMIT} faces: {large[:10]}. "
                                   "Add a filter, or narrow 'bodies'."}

    members = None
    if opts["of_feature"] is not None:
        members, notes, problem = feature_members(design, opts["of_feature"], opts["entity"])
        if problem:
            return None, problem
        warnings.extend(notes)

    records, counts = scan(targets, opts, scene, budget, members, scope)
    return build_result(records, counts, opts, warnings, resolved, started), None


# -- Tool -----------------------------------------------------------------------


def find_entities(arguments):
    """Find faces or edges of the active design by geometry, returning EntityRefs."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        started = time.monotonic()
        design = adsk.fusion.Design.cast(get_app().activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        payload, problem = run(design, FusionScene(design), opts, Budget(max_samples=SCAN_LIMIT), started)
        if problem:
            return error_result(problem)
        log(f"[MCP] find_entities: {payload['returned']}/{payload['matched']} of {payload['scanned']} scanned")
        # Compact on purpose: indenting puts every coordinate on its own line.
        return ok_result(payload, indent=None)
    except Exception as exc:
        log(f"[MCP] find_entities failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
