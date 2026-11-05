"""Enumerate the B-Rep bodies of a design, duck-typed so fakes can drive it.

Bodies inside occurrences are read through the occurrence's body proxies, whose
geometry Fusion returns in the root assembly context, so everything here is in
world coordinates. A body hidden by itself or by its occurrence is often stale
reference geometry; callers decide what to do with one (``is_hidden``).
"""

import math
import time
import uuid
from collections import OrderedDict

from .api import safe, strict_items
from .args import is_int
from .geometry import display_direction, display_point, dot, sub, to_units, unit, xyz

ROOT_PATH = "root"
# How far a temporary copy's box may sit from its source's and still be the same place.
PLACEMENT_TOLERANCE_CM = 1e-3

# adsk.core.SurfaceTypes values (Plane=0 ... NURBS=7).
SURFACE_TYPES = {
    0: "plane", 1: "cylinder", 2: "cone", 3: "sphere", 4: "torus",
    5: "elliptical_cylinder", 6: "elliptical_cone", 7: "nurbs",
}
# A cylinder sweeping less than this is a partial cylinder (fillet/round), not a bore or boss.
PARTIAL_SWEEP_DEG = 135.0
FEATURE_SCAN_LIMIT = 5000

TRAP_NOTE = (
    "Fillet faces are cylinders (straight edge) or tori (curved edge) and can match a bore "
    "or tube OD by radius. Check concavity (concave = bore/inner fillet, convex = boss/OD/rounded edge), "
    "sweep_deg (a fillet sweeps ~90 deg, a bore or OD 360) and created_by before treating a "
    "cylinder as a bore or OD."
)

# Snapshot diffs report volumes to this many digits.
SNAPSHOT_DIGITS = 6
VOLUME_REL_TOLERANCE = 1e-4           # 0.01 % volume change is noise
VOLUME_ABS_TOLERANCE_CM3 = 1e-6
MOVE_TOLERANCE_CM = 1e-4
BASELINE_STORE_LIMIT = 20

# Module-level baseline store: token -> {"created", "document", "records"}.
# Session-only (lost when the add-in reloads), most recent last.
_BASELINES = OrderedDict()


def iter_body_entries(root):
    """Yield one entry per B-Rep body: root bodies, then every occurrence's proxies.

    Keys starting with ``_`` hold Fusion objects and never reach a report.
    """
    containers = [(None, root)]
    containers.extend((occ, occ) for occ in strict_items(safe(root, "allOccurrences")))
    for occ, owner in containers:
        component = safe(occ, "component") if occ is not None else root
        path = safe(occ, "fullPathName") if occ is not None else ROOT_PATH
        for index, body in enumerate(strict_items(safe(owner, "bRepBodies"))):
            yield {
                "occurrence_path": path,
                "component": safe(component, "name"),
                "linked": bool(safe(occ, "isReferencedComponent", False)) if occ is not None else False,
                "body": safe(body, "name"),
                "index": index,
                "visible": safe(body, "isVisible"),
                "occurrence_visible": safe(occ, "isVisible") if occ is not None else None,
                "_body": body,
            }


def is_under(path, paths):
    """True when occurrence *path* sits below one of *paths* ('A:1' holds 'A:1+B:1')."""
    parts = path.split("+")
    return any("+".join(parts[:depth]) in paths for depth in range(1, len(parts)))


def find_occurrence(root, path):
    """The occurrence whose ``fullPathName`` is *path*, or None."""
    return next((occ for occ in strict_items(safe(root, "allOccurrences"))
                 if safe(occ, "fullPathName") == path), None)


def occurrence_frame(occurrence):
    """The occurrence's ``(origin, x, y, z)`` in world coordinates, or None in root."""
    if occurrence is None:
        return None
    return tuple(xyz(part) for part in occurrence.transform2.getAsCoordinateSystem())


def is_hidden(entry):
    """Hidden if the body is hidden or its owning occurrence is."""
    return entry["visible"] is False or entry["occurrence_visible"] is False


def bbox(entity):
    """``(min, max)`` corners of the entity's bounding box in cm, or None."""
    box = safe(entity, "boundingBox")
    return None if box is None else (xyz(box.minPoint), xyz(box.maxPoint))


def native(entity):
    """The native object behind a proxy; *entity* itself when it is not one."""
    found = safe(entity, "nativeObject")
    return found if found is not None else entity


def bbox_overlap(a, b, tol=0.0):
    """True when two (min, max) boxes overlap (or touch within tol)."""
    if a is None or b is None:
        return True
    (amin, amax), (bmin, bmax) = a, b
    return all(amin[i] <= bmax[i] + tol and bmin[i] <= amax[i] + tol for i in range(3))


def bbox_shift(before, after):
    """Largest movement of any box coordinate; None when a box is missing."""
    if before is None or after is None:
        return None
    return max(abs(a - b) for pb, pa in zip(before, after) for b, a in zip(pb, pa))


def path_of(entity):
    """The occurrence path of a body, face or edge; ROOT_PATH when it has no context."""
    occ = safe(entity, "assemblyContext")
    return safe(occ, "fullPathName") if occ is not None else ROOT_PATH


def body_entries(root):
    """Every body in the design, read through occurrence proxies (world coordinates).

    Unlike ``iter_body_entries``, ``visible`` is the final answer (the body and
    its occurrence), and each entry carries what choosing targets needs.
    """
    for entry in iter_body_entries(root):
        body = entry["_body"]
        yield {
            "occurrence_path": entry["occurrence_path"],
            "component": entry["component"],
            "linked": entry["linked"],
            "body": entry["body"],
            "index": entry["index"],
            "visible": not is_hidden(entry),
            "solid": bool(safe(body, "isSolid", False)),
            "bbox": bbox(body),
            "_body": body,
        }


def label(target):
    return f"{target['occurrence_path']}/{target['body']}"


def assign_keys(entries):
    """Give each entry a stable display key: '<occurrence_path>/<body>' (+ '#<i>' for duplicate names)."""
    seen = {}
    for e in entries:
        seen[(e["occurrence_path"], e["body"])] = seen.get((e["occurrence_path"], e["body"]), 0) + 1
    for e in entries:
        base = f"{e['occurrence_path']}/{e['body']}"
        e["key"] = f"{base}#{e['index']}" if seen[(e["occurrence_path"], e["body"])] > 1 else base
    return entries


def parse_body_ref(value, where):
    """A BodyRef object as ``(occurrence_path, body_name, body_index)``; raises ValueError."""
    if not isinstance(value, dict) or set(value) - {"occurrence_path", "body_name", "body_index"}:
        raise ValueError(f"{where} takes occurrence_path, body_name and optional body_index")
    path, name, index = value.get("occurrence_path"), value.get("body_name"), value.get("body_index")
    if not isinstance(path, str) or not path.strip() or not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where} needs a non-empty occurrence_path and body_name")
    if index is not None and (not is_int(index) or index < 0):
        raise ValueError("'body_index' must be a non-negative integer")
    return path, name, index


def parse_body_refs(value, key="bodies"):
    """A list of BodyRefs as ``parse_body_ref`` tuples, repeats dropped; raises ValueError."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{key}' must be a non-empty array; omit it for all visible solids")
    wanted = []
    for item in value:
        ref = parse_body_ref(item, f"each '{key}' entry")
        if ref not in wanted:
            wanted.append(ref)
    return wanted


def resolve_targets(entries, wanted):
    """Pick the bodies to work on. Returns (targets, warnings, error_payload).

    Without *wanted*, every visible solid. Otherwise each request must match
    exactly one entry; a miss or an ambiguity is refused with candidates, since
    working on the wrong body answers the question wrongly without looking wrong.
    """
    def brief(e):
        return {"occurrence_path": e["occurrence_path"], "body_name": e["body"], "body_index": e["index"],
                "visible": e["visible"], "solid": e["solid"]}

    if wanted is None:
        targets = [e for e in entries if e["visible"] and e["solid"]]
        if not targets:
            return None, [], {"error": "the active design has no visible solid bodies"}
        return targets, [], None

    targets, warnings = [], []
    for path, name, index in wanted:
        matches = [e for e in entries if e["occurrence_path"] == path and e["body"] == name
                   and (index is None or e["index"] == index)]
        if len(matches) != 1:
            near = [e for e in entries if e["body"] == name or e["occurrence_path"] == path]
            return None, [], {
                "error": (f"ambiguous: several bodies match {path}/{name}; pass body_index" if matches
                          else f"no body matches {path}/{name}"),
                "candidates": [brief(e) for e in (matches or near)[:50]],
            }
        target = matches[0]
        if target not in targets:
            targets.append(target)
        if not target["solid"]:
            warnings.append(f"{label(target)} is not solid: containment and intervals are not meaningful for it")
    return targets, warnings, None


def temp_copy(manager, body):
    """A temporary copy of *body* sitting where *body* sits, in world coordinates.

    *manager* is the ``TemporaryBRepManager``. The copy of an occurrence proxy
    may come back in component space, so it is compared with the proxy's box and
    moved by the occurrence's transform when it landed elsewhere. Raises
    RuntimeError when no copy can be placed: a misplaced copy would compare
    wrongly without looking wrong.
    """
    def placed(copy):
        shift = bbox_shift(bbox(copy), bbox(body))
        return shift is not None and shift <= PLACEMENT_TOLERANCE_CM

    copy = manager.copy(body)
    if copy is None:
        raise RuntimeError(f"no temporary copy of {safe(body, 'name')}")
    if placed(copy):
        return copy
    occurrence = safe(body, "assemblyContext")
    if occurrence is not None and manager.transform(copy, occurrence.transform2) and placed(copy):
        return copy
    raise RuntimeError(f"the temporary copy of {safe(body, 'name')} did not land on the body")


def volume_of(body):
    """A boolean result's volume; an empty result is zero, not an error."""
    if not strict_items(safe(body, "lumps")):
        return 0.0
    volume = safe(body, "volume")
    if volume is None:
        raise RuntimeError("a boolean result has no readable volume")
    return volume


# -- Faces --------------------------------------------------------------------


def cylinder_concavity(sample_point, outward_normal, axis_origin, axis_dir):
    """'convex' when the outward face normal points away from the axis (OD/boss),
    'concave' when it points toward it (bore/inner fillet), else None."""
    axis = unit(axis_dir)
    if axis is None or outward_normal is None:
        return None
    rel = sub(sample_point, axis_origin)
    along = dot(rel, axis)
    radial = sub(rel, tuple(along * c for c in axis))
    d = dot(radial, outward_normal)
    if abs(d) < 1e-9:
        return None
    return "convex" if d > 0 else "concave"


def axial_extent(points, axis_origin, axis_dir):
    """Length of the projection of *points* onto the axis (0 for <2 points)."""
    axis = unit(axis_dir)
    if axis is None or not points:
        return 0.0
    ts = [dot(sub(p, axis_origin), axis) for p in points]
    return max(ts) - min(ts)


def sweep_degrees(face_area, radius, axial_length):
    """Approximate angular sweep of a cylinder face from area = r * theta * L.
    None when length is unknown; clamped to 360 (cut ends under-report length)."""
    if radius <= 0 or axial_length <= 1e-9:
        return None
    return round(min(360.0, math.degrees(face_area / (radius * axial_length))), 1)


def outward_normal(face):
    """Face normal at pointOnFace from the face evaluator (honors face orientation)."""
    try:
        p = face.pointOnFace
        ok, n = face.evaluator.getNormalAtPoint(p)
        if ok and n is not None:
            return xyz(p), xyz(n)
    except Exception:
        pass
    return None, None


def vertex_points(face):
    pts = []
    for v in strict_items(safe(face, "vertices")):
        g = safe(v, "geometry")
        if g is not None:
            pts.append(xyz(g))
    return pts


def object_type(obj):
    t = safe(obj, "objectType") or type(obj).__name__
    return t.split("::")[-1]


def feature_map(component, temp_ids, native_body):
    """Map face tempId -> (feature name, feature type) for features that own them.

    Best effort: direct-modeled bodies have no features, and Feature.faces is
    not available on every feature type, so missing entries mean 'unknown'.
    """
    found = {}
    if not temp_ids:
        return found, None
    features = safe(component, "features")
    if features is None:
        return found, "component.features unavailable"
    count = safe(features, "count", 0) or 0
    note = None
    if count > FEATURE_SCAN_LIMIT:
        note = f"only the first {FEATURE_SCAN_LIMIT} of {count} features scanned"
    for i in range(min(count, FEATURE_SCAN_LIMIT)):
        try:
            feature = features.item(i)
            faces = feature.faces
        except Exception:
            continue
        for f in strict_items(faces):
            tid = safe(f, "tempId")
            if tid in temp_ids and tid not in found:
                try:
                    if native_body is not None and f.body != native_body:
                        continue
                except Exception:
                    continue
                found[tid] = (safe(feature, "name"), object_type(feature))
        if len(found) == len(temp_ids):
            break
    return found, note


def face_record(face, kind, units):
    """World-frame record for one analytic face (geometry from the proxy)."""
    g = face.geometry
    native_face = native(face)
    face_area = safe(face, "area", 0.0) or 0.0
    reversed_ = safe(face, "isParamReversed")
    rec = {"area": to_units(face_area, units, 2), "is_param_reversed": reversed_,
           "_temp_id": safe(native_face, "tempId")}
    if kind == "plane":
        p, n = outward_normal(face)
        if n is None:
            n = xyz(g.normal)
            if reversed_:
                n = tuple(-c for c in n)
            p = xyz(safe(face, "pointOnFace") or g.origin)
        rec.update({"point": display_point(p, units), "normal": display_direction(n)})
    elif kind == "cylinder":
        r = g.radius
        rec.update({"diameter": to_units(2 * r, units), "radius": to_units(r, units),
                    "axis_origin": display_point(xyz(g.origin), units),
                    "axis_direction": display_direction(xyz(g.axis))})
        ng = native_face.geometry
        n_origin, n_axis = xyz(ng.origin), xyz(ng.axis)
        p, n = outward_normal(native_face)
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
        axial = axial_extent(vertex_points(native_face), n_origin, n_axis)
        rec["length_along_axis"] = to_units(axial, units) if axial > 0 else None
        rec["sweep_deg"] = sweep_degrees(safe(native_face, "area", face_area) or face_area, ng.radius, axial)
    elif kind == "cone":
        rec.update({"radius": to_units(g.radius, units), "half_angle_deg": round(math.degrees(g.halfAngle), 4),
                    "axis_origin": display_point(xyz(g.origin), units),
                    "axis_direction": display_direction(xyz(g.axis))})
    elif kind == "torus":
        rec.update({"major_radius": to_units(g.majorRadius, units), "minor_radius": to_units(g.minorRadius, units),
                    "center": display_point(xyz(g.origin), units), "axis_direction": display_direction(xyz(g.axis))})
    elif kind == "sphere":
        rec.update({"radius": to_units(g.radius, units), "center": display_point(xyz(g.origin), units)})
    return rec


# -- Snapshots and baselines --------------------------------------------------


def measure_summary(entry):
    """Cheap metrics of one keyed entry, used by snapshots and check_solids.

    Reads ``body.volume``, never ``physicalProperties``, which is far slower.
    """
    body = entry["_body"]
    return {
        "key": entry["key"], "occurrence_path": entry["occurrence_path"], "component": entry["component"],
        "body": entry["body"], "index": entry["index"],
        "is_solid": safe(body, "isSolid"),
        "volume_cm3": safe(body, "volume"),
        "lumps": safe(safe(body, "lumps"), "count"),
        "shells": safe(safe(body, "shells"), "count"),
        "face_count": safe(safe(body, "faces"), "count"),
        "edge_count": safe(safe(body, "edges"), "count"),
        "bbox": bbox(body),
    }


def snapshot_records(measured):
    """{key: {volume, faces, bbox, component, occurrence_path, body, index}} for every measured body."""
    return {m["key"]: {"volume": m.get("volume_cm3"), "faces": m.get("face_count"), "bbox": m.get("bbox"),
                       "component": m.get("component"), "occurrence_path": m.get("occurrence_path"),
                       "body": m.get("body"), "index": m.get("index")}
            for m in measured}


def store_baseline(records, document, now=None):
    token = uuid.uuid4().hex[:12]
    _BASELINES[token] = {"created": time.time() if now is None else now, "document": document, "records": records}
    while len(_BASELINES) > BASELINE_STORE_LIMIT:
        _BASELINES.popitem(last=False)
    return token


def get_baseline(token):
    return _BASELINES.get(token)


def in_component(entry, edited_component):
    """True when the body belongs to the component the caller says it edited.

    Matches the component name, the exact occurrence path, or any occurrence
    nested below that path ('Frame:1' covers 'Frame:1+Gusset:1').
    """
    if edited_component is None:
        return False
    path = entry.get("occurrence_path") or ""
    return (entry.get("component") == edited_component or path == edited_component
            or path.startswith(edited_component + "+"))


def volume_changed(before, after):
    if before is None or after is None:
        return before != after
    return abs(after - before) > max(VOLUME_ABS_TOLERANCE_CM3, VOLUME_REL_TOLERANCE * abs(before))


def _report_units(value_cm, units, power=1):
    return to_units(value_cm, units, power, SNAPSHOT_DIGITS)


def diff_snapshots(before, after, edited_component, units):
    """Compare two snapshot record maps. Returns problem rows.

    Changes to bodies outside edited_component are warnings (errors when the
    body lost volume or vanished: the signature of a cut that reached into
    another component); changes inside it are info. With no edited_component
    every change is a warning.
    """
    rows = []

    def severity(rec, loss):
        if edited_component is None:
            return "warning"
        if in_component(rec, edited_component):
            return "info"
        return "error" if loss else "warning"

    def base(kind, key, rec, sev, **detail):
        row = {"type": kind, "severity": sev, "body": key}
        if edited_component is not None:
            row["outside_edited_component"] = not in_component(rec, edited_component)
        row.update(detail)
        return row

    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b is None:
            rows.append(base("body_appeared", key, a, severity(a, False), volume=_report_units(a["volume"], units, 3)))
            continue
        if a is None:
            rows.append(base("body_disappeared", key, b, severity(b, True), volume=_report_units(b["volume"], units, 3)))
            continue
        if volume_changed(b["volume"], a["volume"]):
            delta = (a["volume"] or 0.0) - (b["volume"] or 0.0)
            pct = round(100.0 * delta / b["volume"], 3) if b["volume"] else None
            rows.append(base("volume_changed", key, a, severity(a, delta < 0),
                             before=_report_units(b["volume"], units, 3), after=_report_units(a["volume"], units, 3),
                             delta=_report_units(delta, units, 3), percent=pct))
        if b["faces"] != a["faces"]:
            rows.append(base("face_count_changed", key, a, severity(a, False), before=b["faces"], after=a["faces"]))
        shift = bbox_shift(b["bbox"], a["bbox"])
        if shift is not None and shift > MOVE_TOLERANCE_CM and not volume_changed(b["volume"], a["volume"]):
            rows.append(base("body_moved", key, a, "info", max_bbox_shift=_report_units(shift, units)))
    return rows
