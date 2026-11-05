"""Read-only body inspection: list_bodies and describe_body.

These replace throwaway probe scripts. Everything is reported in world
coordinates: bodies inside occurrences are read through the occurrence's
body proxies, whose geometry Fusion returns in the root assembly context.
Frame-independent measurements (cylinder concavity, axial length, sweep)
are computed on the native face so they never mix two frames.

The Fusion-free helpers at the top take plain values or duck-typed objects
so they are testable with fakes.
"""

import math
import traceback

import adsk.core
import adsk.fusion

from .common.results import error_result, ok_result
from .dispatch import get_app, log

# Fusion lengths are cm; factor converts cm to the chosen unit.
UNIT_FACTORS = {"in": 1.0 / 2.54, "cm": 1.0, "mm": 10.0}
CM3_PER_IN3 = 2.54 ** 3
DEFAULT_MAX_RESULTS = 200
MAX_RESULTS_LIMIT = 1000
DEFAULT_MAX_FACES = 25
MAX_FACES_LIMIT = 500
FEATURE_SCAN_LIMIT = 5000
ROOT_PATH = "root"
_DIGITS = 5

# adsk.core.SurfaceTypes values (Plane=0 ... NURBS=7).
SURFACE_TYPES = {
    0: "plane", 1: "cylinder", 2: "cone", 3: "sphere", 4: "torus",
    5: "elliptical_cylinder", 6: "elliptical_cone", 7: "nurbs",
}
ANALYTIC_GROUPS = {"plane": "planes", "cylinder": "cylinders", "cone": "cones",
                   "torus": "tori", "sphere": "spheres"}
# A cylinder sweeping less than this is a partial cylinder (fillet/round), not a bore or boss.
PARTIAL_SWEEP_DEG = 135.0
_FILLET_FEATURES = ("FilletFeature", "ChamferFeature", "RuleFilletFeature")

TRAP_NOTE = (
    "Fillet faces are cylinders (straight edge) or tori (curved edge) and can match a bore "
    "or tube OD by radius. Check concavity (concave = bore/inner fillet, convex = boss/OD/rounded edge), "
    "sweep_deg (a fillet sweeps ~90 deg, a bore or OD 360) and created_by before treating a "
    "cylinder as a bore or OD."
)


# -- Pure helpers (no Fusion) -------------------------------------------------


def unit_labels(units):
    return {"length": units, "area": f"{units}^2", "volume": f"{units}^3"}


def length(value_cm, units):
    return round(value_cm * UNIT_FACTORS[units], _DIGITS)


def area(value_cm2, units):
    return round(value_cm2 * UNIT_FACTORS[units] ** 2, _DIGITS)


def volume(value_cm3, units):
    return round(value_cm3 * UNIT_FACTORS[units] ** 3, _DIGITS)


def point(xyz_cm, units):
    return [length(c, units) for c in xyz_cm]


def direction(xyz):
    """Unit vector rounded for display; None for a zero vector."""
    n = math.sqrt(sum(c * c for c in xyz))
    if n == 0:
        return None
    return [round(c / n, 6) + 0.0 for c in xyz]


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _unit(v):
    n = math.sqrt(_dot(v, v))
    return tuple(c / n for c in v) if n else None


def _xyz(p):
    return (p.x, p.y, p.z)


def _bool_arg(arguments, key, default):
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        return None, f"Error: '{key}' must be a boolean"
    return value, None


def _int_arg(arguments, key, default, low, high):
    value = arguments.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        return None, f"Error: '{key}' must be an integer from {low} to {high}"
    return value, None


def _str_arg(arguments, key, required=False):
    value = arguments.get(key)
    if value is None:
        return (None, f"Error: '{key}' is required") if required else (None, None)
    if not isinstance(value, str) or (required and not value.strip()):
        return None, f"Error: '{key}' must be a non-empty string"
    return value, None


def _units_arg(arguments):
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        return None, f"Error: 'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}"
    return units, None


def validate_list_args(arguments):
    """Return (options, error_text)."""
    if not isinstance(arguments, dict):
        return None, "Error: arguments must be an object"
    opts = {}
    checks = [
        ("units", lambda: _units_arg(arguments)),
        ("include_hidden", lambda: _bool_arg(arguments, "include_hidden", False)),
        ("name_contains", lambda: _str_arg(arguments, "name_contains")),
        ("component_contains", lambda: _str_arg(arguments, "component_contains")),
        ("max_results", lambda: _int_arg(arguments, "max_results", DEFAULT_MAX_RESULTS, 1, MAX_RESULTS_LIMIT)),
    ]
    for key, check in checks:
        value, err = check()
        if err:
            return None, err
        opts[key] = value
    return opts, None


def validate_describe_args(arguments):
    """Return (options, error_text)."""
    if not isinstance(arguments, dict):
        return None, "Error: arguments must be an object"
    opts = {}
    checks = [
        ("occurrence_path", lambda: _str_arg(arguments, "occurrence_path", required=True)),
        ("body_name", lambda: _str_arg(arguments, "body_name", required=True)),
        ("units", lambda: _units_arg(arguments)),
        ("max_faces", lambda: _int_arg(arguments, "max_faces", DEFAULT_MAX_FACES, 1, MAX_FACES_LIMIT)),
    ]
    for key, check in checks:
        value, err = check()
        if err:
            return None, err
        opts[key] = value
    if "body_index" in arguments:
        value, err = _int_arg(arguments, "body_index", 0, 0, 1_000_000)
        if err:
            return None, err
        opts["body_index"] = value
    else:
        opts["body_index"] = None
    return opts, None


def is_hidden(entry):
    """Hidden if the body is hidden or its owning occurrence is."""
    return entry["visible"] is False or entry["occurrence_visible"] is False


def matches_filters(entry, opts):
    """Apply include_hidden / name_contains / component_contains (case-insensitive)."""
    if not opts["include_hidden"] and is_hidden(entry):
        return False
    name = opts.get("name_contains")
    if name and name.lower() not in (entry["body"] or "").lower():
        return False
    comp = opts.get("component_contains")
    if comp and comp.lower() not in (entry["component"] or "").lower():
        return False
    return True


def resolve_body(entries, occurrence_path, body_name, body_index=None):
    """Pick exactly one entry by occurrence path + body name (+ optional index).

    Returns (entry, error_payload). error_payload lists candidates so the
    caller can retry with an unambiguous body_index or path.
    """
    def brief(e):
        return {"occurrence_path": e["occurrence_path"], "body": e["body"], "body_index": e["index"],
                "visible": not is_hidden(e)}

    exact = [e for e in entries if e["occurrence_path"] == occurrence_path and e["body"] == body_name]
    if body_index is not None:
        exact = [e for e in exact if e["index"] == body_index]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, {"error": "ambiguous: several bodies match; pass body_index",
                      "candidates": [brief(e) for e in exact]}
    lowered = body_name.lower()
    near = [e for e in entries if e["body"] == body_name or e["occurrence_path"] == occurrence_path
            or lowered in (e["body"] or "").lower()]
    return None, {"error": "no body matches occurrence_path + body_name"
                           + (" + body_index" if body_index is not None else ""),
                  "candidates": [brief(e) for e in near[:50]],
                  "candidates_truncated": len(near) > 50}


def cylinder_concavity(sample_point, outward_normal, axis_origin, axis_dir):
    """'convex' when the outward face normal points away from the axis (OD/boss),
    'concave' when it points toward it (bore/inner fillet), else None."""
    axis = _unit(axis_dir)
    if axis is None or outward_normal is None:
        return None
    rel = _sub(sample_point, axis_origin)
    along = _dot(rel, axis)
    radial = _sub(rel, tuple(along * c for c in axis))
    d = _dot(radial, outward_normal)
    if abs(d) < 1e-9:
        return None
    return "convex" if d > 0 else "concave"


def axial_extent(points, axis_origin, axis_dir):
    """Length of the projection of *points* onto the axis (0 for <2 points)."""
    axis = _unit(axis_dir)
    if axis is None or not points:
        return 0.0
    ts = [_dot(_sub(p, axis_origin), axis) for p in points]
    return max(ts) - min(ts)


def sweep_degrees(face_area, radius, axial_length):
    """Approximate angular sweep of a cylinder face from area = r * theta * L.
    None when length is unknown; clamped to 360 (cut ends under-report length)."""
    if radius <= 0 or axial_length <= 1e-9:
        return None
    return round(min(360.0, math.degrees(face_area / (radius * axial_length))), 1)


def cylinder_hint(record):
    """Human-readable warning for cylinders that are probably not bores/ODs."""
    created = record.get("created_by_type") or ""
    if any(created.endswith(t) for t in _FILLET_FEATURES):
        return f"created by {created}: a fillet/chamfer face, not a bore or OD"
    sweep = record.get("sweep_deg")
    if sweep is not None and sweep < PARTIAL_SWEEP_DEG:
        return f"partial cylinder (~{sweep:g} deg sweep): likely a fillet or round, not a bore or OD"
    return None


def sort_and_cap(groups, max_faces):
    """Sort each face group largest first and cap it; return (groups, totals, truncated)."""
    keys = {
        "cylinders": lambda r: (-r["diameter"], -r["area"]),
        "tori": lambda r: (-r["major_radius"], -r["minor_radius"]),
        "spheres": lambda r: (-r["radius"],),
    }
    out, totals, truncated = {}, {}, False
    for name, records in groups.items():
        records = sorted(records, key=keys.get(name, lambda r: (-r["area"],)))
        totals[name] = len(records)
        if len(records) > max_faces:
            truncated = True
        out[name] = records[:max_faces]
    return out, totals, truncated


# -- Fusion access (duck-typed so fakes can drive it) ---------------------------


def _safe(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _items(collection):
    count = _safe(collection, "count", 0) or 0
    for i in range(count):
        yield collection.item(i)


def iter_body_entries(root):
    """Yield one entry per B-Rep body: root bodies, then every occurrence's proxies."""
    containers = [(None, root)]
    containers.extend((occ, occ) for occ in _items(_safe(root, "allOccurrences")))
    for occ, owner in containers:
        bodies = _safe(owner, "bRepBodies")
        component = _safe(occ, "component") if occ is not None else root
        for index, body in enumerate(_items(bodies)):
            yield {
                "occurrence_path": _safe(occ, "fullPathName") if occ is not None else ROOT_PATH,
                "component": _safe(component, "name"),
                "linked": bool(_safe(occ, "isReferencedComponent", False)) if occ is not None else False,
                "body": _safe(body, "name"),
                "index": index,
                "visible": _safe(body, "isVisible"),
                "occurrence_visible": _safe(occ, "isVisible") if occ is not None else None,
                "_body": body,
            }


def _bbox(entity, units):
    bb = _safe(entity, "boundingBox")
    if bb is None:
        return None, None
    return point(_xyz(bb.minPoint), units), point(_xyz(bb.maxPoint), units)


def body_row(entry, units):
    body = entry["_body"]
    vol_cm3 = _safe(body, "volume")
    bb_min, bb_max = _bbox(body, units)
    row = {k: v for k, v in entry.items() if not k.startswith("_")}
    row["body_index"] = row.pop("index")
    row["solid"] = _safe(body, "isSolid")
    row["volume_in3"] = round(vol_cm3 / CM3_PER_IN3, _DIGITS) if vol_cm3 is not None else None
    row["volume_cm3"] = round(vol_cm3, _DIGITS) if vol_cm3 is not None else None
    a = _safe(body, "area")
    row["area"] = area(a, units) if a is not None else None
    row["bbox_min"] = bb_min
    row["bbox_max"] = bb_max
    return row


def list_body_rows(root, opts):
    """Filter, cap and format body rows (metrics only for the returned rows)."""
    entries = list(iter_body_entries(root))
    hidden = sum(1 for e in entries if is_hidden(e))
    matched = [e for e in entries if matches_filters(e, opts)]
    limit = opts["max_results"]
    result = {
        "units": unit_labels(opts["units"]),
        "total_bodies": len(entries),
        "hidden_bodies": hidden,
        "include_hidden": opts["include_hidden"],
        "total_matched": len(matched),
        "returned": min(limit, len(matched)),
        "truncated": len(matched) > limit,
        "bodies": [body_row(e, opts["units"]) for e in matched[:limit]],
    }
    if not opts["include_hidden"] and hidden:
        result["note"] = (f"{hidden} hidden bodies excluded (hidden bodies are often stale "
                          "reference geometry); pass include_hidden=true to see them.")
    return result


def _native(entity):
    return _safe(entity, "nativeObject") or entity


def _outward_normal(face):
    """Face normal at pointOnFace from the face evaluator (honors face orientation)."""
    try:
        p = face.pointOnFace
        ok, n = face.evaluator.getNormalAtPoint(p)
        if ok and n is not None:
            return _xyz(p), _xyz(n)
    except Exception:
        pass
    return None, None


def _vertex_points(face):
    pts = []
    for v in _items(_safe(face, "vertices")):
        g = _safe(v, "geometry")
        if g is not None:
            pts.append(_xyz(g))
    return pts


def feature_map(component, temp_ids, native_body):
    """Map face tempId -> (feature name, feature type) for features that own them.

    Best effort: direct-modeled bodies have no features, and Feature.faces is
    not available on every feature type, so missing entries mean 'unknown'.
    """
    found = {}
    if not temp_ids:
        return found, None
    features = _safe(component, "features")
    if features is None:
        return found, "component.features unavailable"
    count = _safe(features, "count", 0) or 0
    note = None
    if count > FEATURE_SCAN_LIMIT:
        note = f"only the first {FEATURE_SCAN_LIMIT} of {count} features scanned"
    for i in range(min(count, FEATURE_SCAN_LIMIT)):
        try:
            feature = features.item(i)
            faces = feature.faces
        except Exception:
            continue
        for f in _items(faces):
            tid = _safe(f, "tempId")
            if tid in temp_ids and tid not in found:
                try:
                    if native_body is not None and f.body != native_body:
                        continue
                except Exception:
                    continue
                found[tid] = (_safe(feature, "name"), _object_type(feature))
        if len(found) == len(temp_ids):
            break
    return found, note


def _object_type(obj):
    t = _safe(obj, "objectType") or type(obj).__name__
    return t.split("::")[-1]


def _face_record(face, kind, units):
    """World-frame record for one analytic face (geometry from the proxy)."""
    g = face.geometry
    native = _native(face)
    face_area = _safe(face, "area", 0.0) or 0.0
    reversed_ = _safe(face, "isParamReversed")
    rec = {"area": area(face_area, units), "is_param_reversed": reversed_, "_temp_id": _safe(native, "tempId")}
    if kind == "plane":
        p, n = _outward_normal(face)
        if n is None:
            n = _xyz(g.normal)
            if reversed_:
                n = tuple(-c for c in n)
            p = _xyz(_safe(face, "pointOnFace") or g.origin)
        rec.update({"point": point(p, units), "normal": direction(n)})
    elif kind == "cylinder":
        r = g.radius
        rec.update({"diameter": length(2 * r, units), "radius": length(r, units),
                    "axis_origin": point(_xyz(g.origin), units), "axis_direction": direction(_xyz(g.axis))})
        ng = native.geometry
        n_origin, n_axis = _xyz(ng.origin), _xyz(ng.axis)
        p, n = _outward_normal(native)
        concavity = cylinder_concavity(p, n, n_origin, n_axis) if n is not None else None
        basis = "evaluator"
        if reversed_ is not None:
            # A cylinder surface's own normal points away from its axis; a reversed face flips it.
            by_param = "concave" if reversed_ else "convex"
            if concavity is None:
                concavity, basis = by_param, "isParamReversed"
            elif concavity != by_param:
                basis = "evaluator (disagrees with isParamReversed)"
        rec["concavity"] = concavity
        rec["concavity_basis"] = basis if concavity else None
        axial = axial_extent(_vertex_points(native), n_origin, n_axis)
        rec["length_along_axis"] = length(axial, units) if axial > 0 else None
        rec["sweep_deg"] = sweep_degrees(_safe(native, "area", face_area) or face_area, ng.radius, axial)
    elif kind == "cone":
        rec.update({"radius": length(g.radius, units), "half_angle_deg": round(math.degrees(g.halfAngle), 4),
                    "axis_origin": point(_xyz(g.origin), units), "axis_direction": direction(_xyz(g.axis))})
    elif kind == "torus":
        rec.update({"major_radius": length(g.majorRadius, units), "minor_radius": length(g.minorRadius, units),
                    "center": point(_xyz(g.origin), units), "axis_direction": direction(_xyz(g.axis))})
    elif kind == "sphere":
        rec.update({"radius": length(g.radius, units), "center": point(_xyz(g.origin), units)})
    return rec


def _physical(body, units):
    props = _safe(body, "physicalProperties")
    if props is None:
        return None
    out = {}
    com = _safe(props, "centerOfMass")
    if com is not None:
        out["center_of_mass"] = point(_xyz(com), units)
    try:
        ok, x, y, z = props.getPrincipalAxes()
        if ok:
            out["principal_axes"] = {"x": direction(_xyz(x)), "y": direction(_xyz(y)), "z": direction(_xyz(z))}
    except Exception:
        pass
    return out or None


def describe(entry, units, max_faces):
    """Build the describe_body payload for one resolved entry."""
    body = entry["_body"]
    by_type = {}
    groups = {g: [] for g in ANALYTIC_GROUPS.values()}
    for face in _items(_safe(body, "faces")):
        geometry = _safe(face, "geometry")
        kind = SURFACE_TYPES.get(_safe(geometry, "surfaceType"), "unknown")
        by_type[kind] = by_type.get(kind, 0) + 1
        if kind in ANALYTIC_GROUPS:
            try:
                groups[ANALYTIC_GROUPS[kind]].append(_face_record(face, kind, units))
            except Exception as exc:
                log(f"[MCP] describe_body: skipped a {kind} face: {exc}")
    groups, totals, truncated = sort_and_cap(groups, max_faces)

    # Creating feature, looked up only for the reported cylinders and tori.
    wanted = {r["_temp_id"] for g in ("cylinders", "tori") for r in groups[g] if r["_temp_id"] is not None}
    native_body = _native(body)
    creators, feature_note = feature_map(_safe(native_body, "parentComponent"), wanted, native_body)
    for group, records in groups.items():
        for r in records:
            tid = r.pop("_temp_id")
            if group in ("cylinders", "tori"):
                r["created_by"], r["created_by_type"] = creators.get(tid, (None, None))
            if group == "cylinders":
                hint = cylinder_hint(r)
                if hint:
                    r["hint"] = hint

    bb_min, bb_max = _bbox(body, units)
    vol = _safe(body, "volume")
    body_area = _safe(body, "area")
    payload = {
        "body": {"occurrence_path": entry["occurrence_path"], "component": entry["component"],
                 "linked": entry["linked"], "body": entry["body"], "body_index": entry["index"],
                 "visible": entry["visible"], "occurrence_visible": entry["occurrence_visible"]},
        "units": unit_labels(units),
        "is_solid": _safe(body, "isSolid"),
        "volume": volume(vol, units) if vol is not None else None,
        "area": area(body_area, units) if body_area is not None else None,
        "bbox_min": bb_min,
        "bbox_max": bb_max,
        "counts": {
            "faces": sum(by_type.values()),
            "edges": _safe(_safe(body, "edges"), "count"),
            "shells": _safe(_safe(body, "shells"), "count"),
            "lumps": _safe(_safe(body, "lumps"), "count"),
            "faces_by_type": by_type,
        },
        "physical": _physical(body, units),
        "face_totals": totals,
        "faces_truncated": truncated,
        **groups,
        "notes": [TRAP_NOTE,
                  "created_by is best effort (null for direct-modeled bodies or features that do not expose faces).",
                  "length_along_axis and sweep_deg are estimated from face vertices; cut ends can under-report "
                  "length (sweep is then clamped to 360)."],
    }
    if feature_note:
        payload["notes"].append(feature_note)
    return payload


def _root_component():
    app = get_app()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return None
    return design.rootComponent


# -- Tool handlers --------------------------------------------------------------


def list_bodies(arguments):
    """List every B-Rep body in the active design in world coordinates."""
    opts, err = validate_list_args(arguments)
    if err:
        return error_result(err)
    try:
        root = _root_component()
        if root is None:
            return error_result("Error: the active product is not a Fusion design")
        result = list_body_rows(root, opts)
        log(f"[MCP] list_bodies: {result['returned']}/{result['total_matched']} rows")
        return ok_result(result)
    except Exception as exc:
        log(f"[MCP] list_bodies failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")


def describe_body(arguments):
    """Describe one body's faces, analytic surfaces and physical properties."""
    opts, err = validate_describe_args(arguments)
    if err:
        return error_result(err)
    try:
        root = _root_component()
        if root is None:
            return error_result("Error: the active product is not a Fusion design")
        entry, problem = resolve_body(list(iter_body_entries(root)), opts["occurrence_path"],
                                      opts["body_name"], opts["body_index"])
        if problem:
            return error_result(problem)
        payload = describe(entry, opts["units"], opts["max_faces"])
        log(f"[MCP] describe_body: {entry['occurrence_path']} / {entry['body']}")
        return ok_result(payload)
    except Exception as exc:
        log(f"[MCP] describe_body failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
