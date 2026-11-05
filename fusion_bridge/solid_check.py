"""Geometry sanity checks to run after modeling features: check_solids.

Catches the failures that look fine in the viewport until a part is built:
lofts that twisted into fins (sliver or tiny faces, a face count far above
the neighboring bodies), cuts that also removed material from bodies in
other components, base-feature/union artifacts (extra lumps or shells, zero
volume, non-solid bodies) and bodies that overlap when they should not.

Everything is measured on the occurrence proxies, so geometry is in the root
assembly context. The Fusion-free helpers at the top take plain values (or
duck-typed objects) so the metric, threshold and diff logic is testable with
fakes; the Fusion layer only gathers numbers.
"""

import math
import time
import traceback

import adsk.core
import adsk.fusion

from .common.api import safe, strict_items
from .common.args import is_finite_number, is_int
from .common import geometry
from .common.bodies import (
    BASELINE_STORE_LIMIT,
    VOLUME_ABS_TOLERANCE_CM3,
    _BASELINES,  # noqa: F401 (re-exported)
    assign_keys,
    diff_snapshots,
    get_baseline,
    in_component,  # noqa: F401 (re-exported)
    iter_body_entries,
    measure_summary,
    path_of as _entity_path,
    snapshot_records,
    store_baseline,
)
from .common.geometry import UNIT_FACTORS, to_cm, unit_labels
from .common.results import error_result, ok_result
from .dispatch import get_app, log

_DIGITS = 6

CHECKS = ["validity", "slivers", "lumps", "interference", "volume_change"]
DEFAULT_CHECKS = ["validity", "slivers", "lumps", "volume_change"]

# Default thresholds, in inches (converted to the caller's units on input).
DEFAULT_THRESHOLDS_IN = {
    "min_face_area": 1e-4,       # in^2
    "min_edge_length": 1e-3,     # in
    "sliver_width": 2e-3,        # in; mean width of a face = 2 * area / perimeter
    "min_overlap_volume": 1e-6,  # in^3
}
DEFAULT_SLIVER_ASPECT = 50.0          # face length / mean width
DEFAULT_FACE_COUNT_FACTOR = 5.0       # body faces vs median of the checked bodies
FACE_COUNT_MIN_EXCESS = 20            # and at least this many faces above the median
FACE_COUNT_MIN_PEERS = 3              # need this many bodies for a median to mean anything

DEFAULT_MAX_DETAILS = 50
MAX_DETAILS_LIMIT = 500
MAX_BODY_REFS = 500
MAX_INTERFERENCE_PAIRS = 300
MAX_FACE_SCAN = 20000                 # per-face metrics skipped above this (face_count still reported)
MAX_EXAMPLES = 3                      # worst offending faces/edges listed per body

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


# -- Units ----------------------------------------------------------------------


def to_units(value_cm, units, power=1):
    return geometry.to_units(value_cm, units, power, _DIGITS)


# -- Argument validation --------------------------------------------------------


def _number(arguments, key, default):
    value = arguments.get(key, default)
    if not is_finite_number(value) or value <= 0:
        return None, f"Error: '{key}' must be a positive number"
    return float(value), None


def _body_ref(ref, where):
    if not isinstance(ref, dict) or set(ref) - {"occurrence_path", "body_name"}:
        return None, f"Error: {where} must be an object with occurrence_path and body_name"
    path, name = ref.get("occurrence_path"), ref.get("body_name")
    if not isinstance(path, str) or not path.strip() or not isinstance(name, str) or not name.strip():
        return None, f"Error: {where} needs non-empty string occurrence_path and body_name"
    return (path, name), None


def validate_args(arguments):
    """Return (options, error_text). Thresholds come back in cm units."""
    if not isinstance(arguments, dict):
        return None, "Error: arguments must be an object"
    opts = {}

    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        return None, f"Error: 'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}"
    opts["units"] = units

    refs = arguments.get("bodies")
    if refs is not None:
        if not isinstance(refs, list) or not refs or len(refs) > MAX_BODY_REFS:
            return None, f"Error: 'bodies' must be a non-empty list of at most {MAX_BODY_REFS} body references"
        parsed = []
        for i, ref in enumerate(refs):
            value, err = _body_ref(ref, f"bodies[{i}]")
            if err:
                return None, err
            parsed.append(value)
        refs = parsed
    opts["bodies"] = refs

    pairs = arguments.get("interference_pairs")
    checks = arguments.get("checks")
    if checks is None:
        checks = list(DEFAULT_CHECKS)
        if pairs is not None:
            checks.append("interference")
    elif (not isinstance(checks, list) or not checks
          or any(not isinstance(c, str) or c not in CHECKS for c in checks)):
        return None, f"Error: 'checks' must be a non-empty list drawn from {CHECKS}"
    opts["checks"] = [c for c in CHECKS if c in checks]

    if pairs is None or pairs == "all":
        opts["interference_pairs"] = "all"
    elif isinstance(pairs, list) and pairs:
        if len(pairs) > MAX_INTERFERENCE_PAIRS:
            return None, f"Error: 'interference_pairs' allows at most {MAX_INTERFERENCE_PAIRS} pairs"
        parsed = []
        for i, pair in enumerate(pairs):
            if not isinstance(pair, list) or len(pair) != 2:
                return None, f"Error: interference_pairs[{i}] must be a list of two body references"
            a, err = _body_ref(pair[0], f"interference_pairs[{i}][0]")
            if err:
                return None, err
            b, err = _body_ref(pair[1], f"interference_pairs[{i}][1]")
            if err:
                return None, err
            if a == b:
                return None, f"Error: interference_pairs[{i}] pairs a body with itself"
            parsed.append((a, b))
        opts["interference_pairs"] = parsed
    else:
        return None, "Error: 'interference_pairs' must be 'all' or a non-empty list of body-reference pairs"

    token = arguments.get("baseline_token")
    if token is not None and (not isinstance(token, str) or not token.strip()):
        return None, "Error: 'baseline_token' must be a non-empty string"
    opts["baseline_token"] = token

    snapshot = arguments.get("snapshot", token is None)
    if not isinstance(snapshot, bool):
        return None, "Error: 'snapshot' must be a boolean"
    opts["snapshot"] = snapshot

    edited = arguments.get("edited_component")
    if edited is not None and (not isinstance(edited, str) or not edited.strip()):
        return None, "Error: 'edited_component' must be a non-empty string"
    opts["edited_component"] = edited

    thresholds = {}
    for key, default_in in DEFAULT_THRESHOLDS_IN.items():
        power = 3 if key == "min_overlap_volume" else 2 if key == "min_face_area" else 1
        if key in arguments:
            value, err = _number(arguments, key, None)
            if err:
                return None, err
            thresholds[key] = to_cm(value, units, power)
        else:
            thresholds[key] = to_cm(default_in, "in", power)
    for key, default in (("sliver_aspect", DEFAULT_SLIVER_ASPECT), ("face_count_factor", DEFAULT_FACE_COUNT_FACTOR)):
        value, err = _number(arguments, key, default)
        if err:
            return None, err
        thresholds[key] = value
    opts["thresholds"] = thresholds

    max_details = arguments.get("max_details", DEFAULT_MAX_DETAILS)
    if not is_int(max_details) or not 1 <= max_details <= MAX_DETAILS_LIMIT:
        return None, f"Error: 'max_details' must be an integer from 1 to {MAX_DETAILS_LIMIT}"
    opts["max_details"] = max_details
    return opts, None


def thresholds_for_display(thresholds, units):
    return {
        "min_face_area": to_units(thresholds["min_face_area"], units, 2),
        "min_edge_length": to_units(thresholds["min_edge_length"], units),
        "sliver_width": to_units(thresholds["sliver_width"], units),
        "sliver_aspect": thresholds["sliver_aspect"],
        "face_count_factor": thresholds["face_count_factor"],
        "min_overlap_volume": to_units(thresholds["min_overlap_volume"], units, 3),
    }


# -- Body identity -------------------------------------------------------------


def select_entries(entries, refs):
    """Default: visible bodies. With refs: every body matching a (path, name) ref, hidden or not.

    Returns (selected, unresolved_refs).
    """
    if refs is None:
        return [e for e in entries if e["visible"] is not False and e["occurrence_visible"] is not False], []
    wanted = set(refs)
    selected = [e for e in entries if (e["occurrence_path"], e["body"]) in wanted]
    found = {(e["occurrence_path"], e["body"]) for e in selected}
    return selected, [r for r in refs if r not in found]


# -- Per-body checks (pure) -----------------------------------------------------


def _problem(kind, severity, metrics, **detail):
    row = {"type": kind, "severity": severity, "body": metrics["key"]}
    row.update(detail)
    return row


def face_width(area_cm2, perimeter_cm):
    """Mean width of a face: 2A/P (exact for a long thin strip, the wall for an annulus)."""
    if not perimeter_cm or perimeter_cm <= 0:
        return None
    return 2.0 * area_cm2 / perimeter_cm


def validity_problems(metrics, thresholds, units):
    """non_solid, zero_volume, tiny_faces and short_edges for one measured body."""
    problems = []
    if metrics.get("is_solid") is False:
        problems.append(_problem("non_solid", "error", metrics,
                                 note="surface/open body; downstream volume and interference checks skip it"))
    vol = metrics.get("volume_cm3")
    if metrics.get("is_solid") is not False and (vol is None or vol <= VOLUME_ABS_TOLERANCE_CM3):
        problems.append(_problem("zero_volume", "error", metrics, volume=to_units(vol, units, 3)))

    faces = metrics.get("faces")
    if faces is not None:
        tiny = sorted((f for f in faces if f["area"] < thresholds["min_face_area"]), key=lambda f: f["area"])
        if tiny:
            problems.append(_problem("tiny_faces", "warning", metrics, count=len(tiny),
                                     smallest_area=to_units(tiny[0]["area"], units, 2),
                                     examples=[{"face_index": f["index"], "area": to_units(f["area"], units, 2)}
                                               for f in tiny[:MAX_EXAMPLES]]))
    edges = metrics.get("edge_lengths")
    if edges is not None:
        short = sorted(x for x in edges if x < thresholds["min_edge_length"])
        if short:
            problems.append(_problem("short_edges", "warning", metrics, count=len(short),
                                     shortest=to_units(short[0], units)))
    return problems


def sliver_problems(metrics, thresholds, units):
    """Faces that are long and very thin (mean width below sliver_width, length/width above sliver_aspect)."""
    faces = metrics.get("faces")
    if not faces:
        return []
    slivers = []
    for f in faces:
        width = face_width(f["area"], f.get("perimeter"))
        if width is None or width >= thresholds["sliver_width"]:
            continue
        length = f["perimeter"] / 2.0
        aspect = length / width if width > 0 else math.inf
        if aspect >= thresholds["sliver_aspect"]:
            slivers.append((width, aspect, f))
    if not slivers:
        return []
    slivers.sort(key=lambda s: s[0])
    return [_problem("sliver_faces", "warning", metrics, count=len(slivers),
                     examples=[{"face_index": f["index"], "width": to_units(w, units),
                                "length": to_units(f["perimeter"] / 2.0, units),
                                "aspect": round(a, 1) if math.isfinite(a) else None}
                               for w, a, f in slivers[:MAX_EXAMPLES]])]


def median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def face_count_outliers(metrics_list, factor):
    """Bodies whose face count is far above the median of the checked solid bodies."""
    counts = [m["face_count"] for m in metrics_list if m.get("face_count") and m.get("is_solid") is not False]
    if len(counts) < FACE_COUNT_MIN_PEERS:
        return []
    med = median(counts)
    limit = max(med * factor, med + FACE_COUNT_MIN_EXCESS)
    return [_problem("face_count_outlier", "warning", m, face_count=m["face_count"], median_face_count=med)
            for m in metrics_list
            if m.get("face_count") and m.get("is_solid") is not False and m["face_count"] > limit]


def lump_problems(metrics):
    """multiple_lumps (disjoint pieces in one body) and extra_shells (internal voids)."""
    problems = []
    lumps, shells = metrics.get("lumps"), metrics.get("shells")
    if lumps is not None and lumps > 1:
        problems.append(_problem("multiple_lumps", "warning", metrics, lumps=lumps,
                                 note="one body made of disjoint pieces; usually a union or base-feature artifact"))
    if lumps is not None and shells is not None and shells > lumps:
        problems.append(_problem("extra_shells", "warning", metrics, shells=shells, lumps=lumps,
                                 note="more shells than lumps means enclosed voids"))
    return problems


# -- Interference (pure parts) -------------------------------------------------


def bbox_overlap(a, b, tol=0.0):
    """True when two (min, max) boxes overlap (or touch within tol)."""
    if a is None or b is None:
        return True
    (amin, amax), (bmin, bmax) = a, b
    return all(amin[i] <= bmax[i] + tol and bmin[i] <= amax[i] + tol for i in range(3))


def resolve_pairs(selected, pairs, entries):
    """Turn 'all' or ref pairs into entry pairs. Returns (entry_pairs, error_text)."""
    solids = [e for e in selected if e.get("is_solid", True) is not False]
    if pairs == "all":
        n = len(solids)
        count = n * (n - 1) // 2
        if count > MAX_INTERFERENCE_PAIRS:
            return None, (f"Error: {n} bodies make {count} interference pairs (limit {MAX_INTERFERENCE_PAIRS}); "
                          "narrow 'bodies' or pass explicit 'interference_pairs'")
        return [(solids[i], solids[j]) for i in range(n) for j in range(i + 1, n)], None
    by_ref = {}
    for e in entries:
        by_ref.setdefault((e["occurrence_path"], e["body"]), []).append(e)
    out = []
    for a, b in pairs:
        for ref in (a, b):
            if ref not in by_ref:
                return None, f"Error: interference body not found: occurrence_path={ref[0]!r} body_name={ref[1]!r}"
        for ea in by_ref[a]:
            for eb in by_ref[b]:
                if ea is not eb:
                    out.append((ea, eb))
    if len(out) > MAX_INTERFERENCE_PAIRS:
        return None, f"Error: interference pairs expand to {len(out)} (limit {MAX_INTERFERENCE_PAIRS})"
    return out, None


def interference_problems(overlaps, thresholds, units):
    """overlaps: [(key_a, key_b, volume_cm3)] -> problems above min_overlap_volume, largest first."""
    rows = [(a, b, v) for a, b, v in overlaps if v is not None and v >= thresholds["min_overlap_volume"]]
    rows.sort(key=lambda r: -r[2])
    return [{"type": "interference", "severity": "error", "body": a, "other_body": b,
             "overlap_volume": to_units(v, units, 3)} for a, b, v in rows]


# -- Summary -------------------------------------------------------------------


def summarize(problems, max_details):
    """Counts by type and severity, then the detail rows sorted by severity and capped."""
    by_type, by_severity = {}, {"error": 0, "warning": 0, "info": 0}
    for p in problems:
        by_type[p["type"]] = by_type.get(p["type"], 0) + 1
        by_severity[p["severity"]] = by_severity.get(p["severity"], 0) + 1
    ordered = sorted(problems, key=lambda p: (SEVERITY_ORDER.get(p["severity"], 9), p["type"], p.get("body", "")))
    return {
        "ok": by_severity["error"] == 0 and by_severity["warning"] == 0,
        "summary": {"problems": len(problems), "by_severity": by_severity, "by_type": by_type,
                    "bodies_with_problems": len({p.get("body") for p in problems if p["severity"] != "info"})},
        "problems": ordered[:max_details],
        "problems_truncated": len(ordered) > max_details,
    }


# -- Fusion layer ---------------------------------------------------------------


def measure_details(entry, metrics):
    """Add per-face area/perimeter and per-edge lengths (skipped for very large bodies)."""
    body = entry["_body"]
    if (metrics.get("face_count") or 0) > MAX_FACE_SCAN:
        metrics["face_scan_skipped"] = True
        return metrics
    faces = []
    for index, face in enumerate(strict_items(safe(body, "faces"))):
        area_cm2 = safe(face, "area")
        if area_cm2 is None:
            continue
        perimeter = sum((safe(e, "length", 0.0) or 0.0) for e in strict_items(safe(face, "edges")))
        faces.append({"index": index, "area": area_cm2, "perimeter": perimeter})
    metrics["faces"] = faces
    metrics["edge_lengths"] = [x for x in (safe(e, "length") for e in strict_items(safe(body, "edges"))) if x is not None]
    return metrics


def _match_entity(entity, candidates):
    """Map an interference result entity back to one of the input entries."""
    for e in candidates:
        try:
            if entity == e["_body"]:
                return e
        except Exception:
            pass
    path, name = _entity_path(entity), safe(entity, "name")
    for e in candidates:
        if e["occurrence_path"] == path and e["body"] == name:
            return e
    return None


def _analyze_interference(design, entry_pairs, pairs_mode):
    """Overlaps via Design.analyzeInterference. Returns [(key_a, key_b, volume_cm3)]."""
    def run(entries):
        collection = adsk.core.ObjectCollection.create()
        for e in entries:
            collection.add(e["_body"])
        interference_input = design.createInterferenceInput(collection)
        if interference_input is None:
            raise RuntimeError("Design.createInterferenceInput returned null")
        interference_input.areCoincidentFacesIncluded = False
        found = []
        for result in strict_items(design.analyzeInterference(interference_input)):
            ea, eb = _match_entity(result.entityOne, entries), _match_entity(result.entityTwo, entries)
            found.append((ea["key"] if ea else safe(result.entityOne, "name"),
                          eb["key"] if eb else safe(result.entityTwo, "name"),
                          safe(safe(result, "interferenceBody"), "volume")))
        return found

    if pairs_mode == "all":
        unique = []
        for a, b in entry_pairs:
            for e in (a, b):
                if all(e is not u for u in unique):
                    unique.append(e)
        return run(unique) if len(unique) > 1 else []
    overlaps = []
    for a, b in entry_pairs:
        overlaps.extend(run([a, b]))
    return overlaps


def _boolean_interference(entry_pairs):
    """Fallback: intersect temporary copies pairwise (bounding boxes first)."""
    manager = adsk.fusion.TemporaryBRepManager.get()
    overlaps = []
    for a, b in entry_pairs:
        if not bbox_overlap(a.get("bbox"), b.get("bbox")):
            continue
        target = manager.copy(a["_body"])
        tool = manager.copy(b["_body"])
        if target is None or tool is None:
            continue
        if manager.booleanOperation(target, tool, adsk.fusion.BooleanTypes.IntersectionBooleanType):
            vol = safe(target, "volume")
            if vol:
                overlaps.append((a["key"], b["key"], vol))
    return overlaps


def run_interference(design, entry_pairs, pairs_mode):
    """Returns (overlaps, method, note)."""
    if not entry_pairs:
        return [], None, None
    if callable(safe(design, "createInterferenceInput")):
        try:
            return _analyze_interference(design, entry_pairs, pairs_mode), "analyzeInterference", None
        except Exception as exc:
            note = f"analyzeInterference failed ({exc}); fell back to temporary B-Rep intersection"
            log(f"[MCP] check_solids: {note}")
            return _boolean_interference(entry_pairs), "temporary_brep_intersection", note
    return _boolean_interference(entry_pairs), "temporary_brep_intersection", None


def _document_name(app):
    doc = safe(app, "activeDocument")
    return safe(doc, "name") if doc is not None else None


def run_checks(design, document, opts):
    """Gather metrics for the active design and assemble the report."""
    units, thresholds, checks = opts["units"], opts["thresholds"], opts["checks"]
    entries = assign_keys(list(iter_body_entries(design.rootComponent)))
    selected, unresolved = select_entries(entries, opts["bodies"])
    if unresolved:
        names = sorted({f"{p}/{n}" for p, n in unresolved})
        return None, f"Error: bodies not found in the active design: {names[:20]}"

    summaries = {e["key"]: measure_summary(e) for e in selected}
    for e in selected:
        e["is_solid"] = summaries[e["key"]]["is_solid"]
        e["bbox"] = summaries[e["key"]]["bbox"]
    if "validity" in checks or "slivers" in checks:
        for e in selected:
            measure_details(e, summaries[e["key"]])

    problems, notes = [], []
    for e in selected:
        m = summaries[e["key"]]
        if "validity" in checks:
            problems.extend(validity_problems(m, thresholds, units))
        if "slivers" in checks:
            problems.extend(sliver_problems(m, thresholds, units))
        if "lumps" in checks:
            problems.extend(lump_problems(m))
    if "slivers" in checks:
        problems.extend(face_count_outliers(list(summaries.values()), thresholds["face_count_factor"]))
    skipped = [k for k, m in summaries.items() if m.get("face_scan_skipped")]
    if skipped:
        notes.append(f"per-face checks skipped for {len(skipped)} bodies above {MAX_FACE_SCAN} faces: {skipped[:5]}")

    report = {"document": document, "units": unit_labels(units), "checks_run": checks,
              "bodies_checked": len(selected), "thresholds": thresholds_for_display(thresholds, units)}

    if "interference" in checks:
        entry_pairs, err = resolve_pairs(selected, opts["interference_pairs"], entries)
        if err:
            return None, err
        for a, b in entry_pairs:
            for e in (a, b):
                if "bbox" not in e:
                    s = measure_summary(e)
                    e["is_solid"], e["bbox"] = s["is_solid"], s["bbox"]
        entry_pairs = [(a, b) for a, b in entry_pairs if a.get("is_solid") is not False and b.get("is_solid") is not False]
        overlaps, method, note = run_interference(design, entry_pairs, opts["interference_pairs"])
        problems.extend(interference_problems(overlaps, thresholds, units))
        report["interference"] = {"pairs_checked": len(entry_pairs), "method": method}
        if note:
            notes.append(note)

    if "volume_change" in checks:
        # Baselines cover every body in the design (hidden too), not just the
        # filtered ones: the point is catching changes in bodies nobody targeted.
        current = snapshot_records([summaries.get(e["key"]) or measure_summary(e) for e in entries])
        vc = {}
        if opts["baseline_token"] is not None:
            baseline = get_baseline(opts["baseline_token"])
            if baseline is None:
                return None, (f"Error: unknown baseline_token {opts['baseline_token']!r} "
                              "(baselines are session-only and the last "
                              f"{BASELINE_STORE_LIMIT} are kept)")
            if baseline["document"] != document:
                notes.append(f"baseline was taken in document {baseline['document']!r}, "
                             f"active document is {document!r}")
            changes = diff_snapshots(baseline["records"], current, opts["edited_component"], units)
            problems.extend(changes)
            vc = {"baseline_token": opts["baseline_token"],
                  "baseline_age_seconds": round(time.time() - baseline["created"], 1),
                  "changed_bodies": len({c["body"] for c in changes}),
                  "edited_component": opts["edited_component"]}
            if opts["edited_component"] is not None:
                vc["outside_edited_component"] = sum(1 for c in changes if c.get("outside_edited_component"))
        if opts["snapshot"]:
            report["baseline_token"] = store_baseline(current, document)
            vc["snapshot_bodies"] = len(current)
        report["volume_change"] = vc

    report.update(summarize(problems, opts["max_details"]))
    if notes:
        report["notes"] = notes
    # Summary first in the serialized output.
    ordered = {k: report[k] for k in ("ok", "summary") if k in report}
    ordered.update({k: v for k, v in report.items() if k not in ordered})
    return ordered, None


def check_solids(arguments):
    """Run geometry sanity checks on bodies in the active design."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        report, err = run_checks(design, _document_name(app), opts)
        if err:
            return error_result(err)
        log(f"[MCP] check_solids: {report['bodies_checked']} bodies, {report['summary']['problems']} problems")
        return ok_result(report)
    except Exception as exc:
        log(f"[MCP] check_solids failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
