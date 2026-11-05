"""Read-only body inspection: list_bodies and describe_body.

These replace throwaway probe scripts. Everything is reported in world
coordinates: bodies inside occurrences are read through the occurrence's
body proxies, whose geometry Fusion returns in the root assembly context.
Frame-independent measurements (cylinder concavity, axial length, sweep)
are computed on the native face so they never mix two frames.

The Fusion-free helpers at the top take plain values or duck-typed objects
so they are testable with fakes.
"""

import traceback

import adsk.core
import adsk.fusion

from .common.api import safe, strict_items
from .common.args import bool_arg, error_pair, int_arg, require_object, text_arg
from .common.bodies import (
    PARTIAL_SWEEP_DEG,
    SURFACE_TYPES,
    TRAP_NOTE,
    axial_extent,  # noqa: F401 (re-exported)
    bbox,
    cylinder_concavity,  # noqa: F401 (re-exported)
    face_record as _face_record,
    feature_map,
    is_hidden,
    iter_body_entries,
    native,
    sweep_degrees,  # noqa: F401 (re-exported)
)
from .common.geometry import (
    DIGITS,
    UNIT_FACTORS,
    display_direction as direction,
    display_point as point,
    to_units,
    unit_labels,
    xyz,
)
from .common.results import error_result, ok_result
from .dispatch import get_app, log

CM3_PER_IN3 = 2.54 ** 3
DEFAULT_MAX_RESULTS = 200
MAX_RESULTS_LIMIT = 1000
DEFAULT_MAX_FACES = 25
MAX_FACES_LIMIT = 500

ANALYTIC_GROUPS = {"plane": "planes", "cylinder": "cylinders", "cone": "cones",
                   "torus": "tori", "sphere": "spheres"}
_FILLET_FEATURES = ("FilletFeature", "ChamferFeature", "RuleFilletFeature")

# -- Pure helpers (no Fusion) -------------------------------------------------


def length(value_cm, units):
    return to_units(value_cm, units)


def area(value_cm2, units):
    return to_units(value_cm2, units, 2)


def volume(value_cm3, units):
    return to_units(value_cm3, units, 3)


def _units_arg(arguments):
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    return units


@error_pair
def validate_list_args(arguments):
    """Return (options, error_text)."""
    require_object(arguments)
    return {
        "units": _units_arg(arguments),
        "include_hidden": bool_arg(arguments, "include_hidden", False),
        "name_contains": text_arg(arguments, "name_contains", blank_ok=True),
        "component_contains": text_arg(arguments, "component_contains", blank_ok=True),
        "max_results": int_arg(arguments, "max_results", DEFAULT_MAX_RESULTS, 1, MAX_RESULTS_LIMIT),
    }


@error_pair
def validate_describe_args(arguments):
    """Return (options, error_text)."""
    require_object(arguments)
    return {
        "occurrence_path": text_arg(arguments, "occurrence_path", required=True),
        "body_name": text_arg(arguments, "body_name", required=True),
        "units": _units_arg(arguments),
        "max_faces": int_arg(arguments, "max_faces", DEFAULT_MAX_FACES, 1, MAX_FACES_LIMIT),
        "body_index": int_arg(arguments, "body_index", 0, 0, 1_000_000) if "body_index" in arguments else None,
    }


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


def _bbox(entity, units):
    box = bbox(entity)
    return (None, None) if box is None else (point(box[0], units), point(box[1], units))


def body_row(entry, units):
    body = entry["_body"]
    vol_cm3 = safe(body, "volume")
    bb_min, bb_max = _bbox(body, units)
    row = {k: v for k, v in entry.items() if not k.startswith("_")}
    row["body_index"] = row.pop("index")
    row["solid"] = safe(body, "isSolid")
    row["volume_in3"] = round(vol_cm3 / CM3_PER_IN3, DIGITS) if vol_cm3 is not None else None
    row["volume_cm3"] = round(vol_cm3, DIGITS) if vol_cm3 is not None else None
    a = safe(body, "area")
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


def _physical(body, units):
    props = safe(body, "physicalProperties")
    if props is None:
        return None
    out = {}
    com = safe(props, "centerOfMass")
    if com is not None:
        out["center_of_mass"] = point(xyz(com), units)
    try:
        ok, x, y, z = props.getPrincipalAxes()
        if ok:
            out["principal_axes"] = {"x": direction(xyz(x)), "y": direction(xyz(y)), "z": direction(xyz(z))}
    except Exception:
        pass
    return out or None


def describe(entry, units, max_faces):
    """Build the describe_body payload for one resolved entry."""
    body = entry["_body"]
    by_type = {}
    groups = {g: [] for g in ANALYTIC_GROUPS.values()}
    for face in strict_items(safe(body, "faces")):
        geometry = safe(face, "geometry")
        kind = SURFACE_TYPES.get(safe(geometry, "surfaceType"), "unknown")
        by_type[kind] = by_type.get(kind, 0) + 1
        if kind in ANALYTIC_GROUPS:
            try:
                groups[ANALYTIC_GROUPS[kind]].append(_face_record(face, kind, units))
            except Exception as exc:
                log(f"[MCP] describe_body: skipped a {kind} face: {exc}")
    groups, totals, truncated = sort_and_cap(groups, max_faces)

    # Creating feature, looked up only for the reported cylinders and tori.
    wanted = {r["_temp_id"] for g in ("cylinders", "tori") for r in groups[g] if r["_temp_id"] is not None}
    native_body = native(body)
    creators, feature_note = feature_map(safe(native_body, "parentComponent"), wanted, native_body)
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
    vol = safe(body, "volume")
    body_area = safe(body, "area")
    payload = {
        "body": {"occurrence_path": entry["occurrence_path"], "component": entry["component"],
                 "linked": entry["linked"], "body": entry["body"], "body_index": entry["index"],
                 "visible": entry["visible"], "occurrence_visible": entry["occurrence_visible"]},
        "units": unit_labels(units),
        "is_solid": safe(body, "isSolid"),
        "volume": volume(vol, units) if vol is not None else None,
        "area": area(body_area, units) if body_area is not None else None,
        "bbox_min": bb_min,
        "bbox_max": bb_max,
        "counts": {
            "faces": sum(by_type.values()),
            "edges": safe(safe(body, "edges"), "count"),
            "shells": safe(safe(body, "shells"), "count"),
            "lumps": safe(safe(body, "lumps"), "count"),
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
