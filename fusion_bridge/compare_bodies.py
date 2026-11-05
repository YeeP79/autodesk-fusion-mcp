"""Are two bodies the same solid, and where do they differ: compare_bodies.

Read-only: every boolean runs on temporary copies, which never enter the design
or the timeline. Equal volume proves nothing (a split shaft has the volume of a
whole one), so ``same`` needs the symmetric difference and the box shift both
within tolerance.

Near-identical bodies are this tool's main job and their coincident faces are
where booleans are weakest. So the three booleans check each other (``consistent``),
a failure falls back to a containment grid, and when neither can be trusted the
verdict is ``unknown``, never ``same``.

Everything above the Fusion layer takes plain values and duck-typed ``copies``
and ``state`` collaborators, so it is testable with fakes. Internal values are
cm; arguments are converted on the way in and results on the way out.
"""

import json
import math
import time
import traceback

import adsk.core
import adsk.fusion

from .common import geometry
from .common.api import safe, strict_items
from .common.args import bool_arg, error_pair, int_arg, is_finite_number, reject_unknown, require_object
from .common.bodies import bbox, bbox_overlap, bbox_shift, body_entries, label, parse_body_ref, resolve_targets, temp_copy
from .common.geometry import UNIT_FACTORS, add, sub, to_cm, unit_labels
from .common.results import error_result, ok_result
from .dispatch import get_app, log
from .probe_geometry import MAX_SAMPLES, Budget, FusionProbe

_DIGITS = 6

_KNOWN = {"a", "b", "units", "volume_tolerance", "length_tolerance", "max_lumps", "sample_fallback", "translate_b"}

# Defaults in inches, converted when the caller works in other units.
DEFAULT_VOLUME_TOLERANCE_IN3 = 1e-4
DEFAULT_LENGTH_TOLERANCE_IN = 1e-3
DEFAULT_MAX_LUMPS = 20
MAX_LUMPS_LIMIT = 500

# Kernel volumes on a large body drift past a fixed tolerance, so the booleans'
# cross-check also allows this fraction of the larger body.
CONSISTENCY_REL = 1e-6

INTERSECTION = "IntersectionBooleanType"
DIFFERENCE = "DifferenceBooleanType"


def to_units(value_cm, units, power=1):
    return geometry.to_units(value_cm, units, power, _DIGITS)


# -- Argument validation --------------------------------------------------------


def _tolerance(arguments, key, default_in, units, power):
    if key not in arguments:
        return to_cm(default_in, "in", power)
    value = arguments[key]
    if not is_finite_number(value) or value < 0:
        raise ValueError(f"'{key}' must be a non-negative number")
    return to_cm(float(value), units, power)


def _translate(arguments, units):
    value = arguments.get("translate_b", [0, 0, 0])
    if not isinstance(value, list) or len(value) != 3 or not all(is_finite_number(c) for c in value):
        raise ValueError("'translate_b' must be [x, y, z] numbers")
    return tuple(to_cm(float(c), units) for c in value)


@error_pair
def validate_args(arguments):
    """Return (options, error_text). Tolerances and the translation come back in cm."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN)
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    for key in ("a", "b"):
        if key not in arguments:
            raise ValueError(f"'{key}' is required")
    return {
        "a": parse_body_ref(arguments["a"], "'a'"),
        "b": parse_body_ref(arguments["b"], "'b'"),
        "units": units,
        "volume_tolerance": _tolerance(arguments, "volume_tolerance", DEFAULT_VOLUME_TOLERANCE_IN3, units, 3),
        "length_tolerance": _tolerance(arguments, "length_tolerance", DEFAULT_LENGTH_TOLERANCE_IN, units, 1),
        "max_lumps": int_arg(arguments, "max_lumps", DEFAULT_MAX_LUMPS, 0, MAX_LUMPS_LIMIT),
        "sample_fallback": bool_arg(arguments, "sample_fallback", True),
        "translate_b": _translate(arguments, units),
    }


def pick_pair(entries, a_ref, b_ref):
    """The two entries to compare. Returns (a, b, warnings, error_payload)."""
    picked = []
    for ref in (a_ref, b_ref):
        targets, _, problem = resolve_targets(entries, [ref])
        if problem:
            return None, None, [], problem
        picked.append(targets[0])
    a, b = picked
    if a is b:
        return None, None, [], {"error": f"'a' and 'b' are the same body: {label(a)}"}
    surfaces = [label(e) for e in picked if not e["solid"]]
    if surfaces:
        return None, None, [], {"error": f"not solid, so it has no volume to compare: {surfaces}"}
    return a, b, [f"{label(e)} is hidden; hidden bodies are often stale" for e in picked if not e["visible"]], None


# -- Verdict (no Fusion) --------------------------------------------------------


def verdict(symmetric_difference, shift, volume_tolerance, length_tolerance, uncertainty=0.0):
    """``same``, ``different`` or ``unknown``, all in cm.

    A missing measurement is ``unknown``. *uncertainty* is the sampled path's
    error bar: ``same`` needs the whole bar inside the tolerance and ``different``
    needs it outside, so a coarse grid that found nothing answers ``unknown``.
    """
    if symmetric_difference is None or shift is None:
        return "unknown"
    if shift > length_tolerance or symmetric_difference - uncertainty > volume_tolerance:
        return "different"
    return "same" if symmetric_difference + uncertainty <= volume_tolerance else "unknown"


def consistent(volume_a, volume_b, overlap, a_only, b_only, tolerance):
    """Do the booleans agree? Returns (gap_a, gap_b, ok), gaps in cm^3.

    Each body must equal its overlap plus its own difference. A kernel failing
    on coincident faces tends to return a plausible number for one of the three.
    """
    if None in (volume_a, volume_b, overlap, a_only, b_only):
        return None, None, False
    gap_a, gap_b = volume_a - overlap - a_only, volume_b - overlap - b_only
    limit = max(tolerance, CONSISTENCY_REL * max(volume_a, volume_b))
    return gap_a, gap_b, abs(gap_a) <= limit and abs(gap_b) <= limit


def capped_lumps(lumps, limit):
    """The largest *limit* lumps, biggest first."""
    return sorted(lumps, key=lambda lump: -lump["volume"])[:limit]


# -- Sampling (no Fusion) -------------------------------------------------------


def grid_dims(low, high, max_cells):
    """Cells per axis, close to cubic, at most *max_cells* in all."""
    spans = [h - l for l, h in zip(low, high)]
    filled = [s for s in spans if s > 0]
    if not filled:
        return (1, 1, 1)
    side = (math.prod(filled) / max_cells) ** (1.0 / len(filled))
    dims = [max(1, round(s / side)) for s in spans]
    while math.prod(dims) > max_cells:  # a thin axis held at 1 leaves the others too many
        dims[dims.index(max(dims))] -= 1
    return tuple(dims)


def cell_centers(low, high, dims):
    """Yield the center of every cell, x slowest."""
    sizes = [(h - l) / n for l, h, n in zip(low, high, dims)]
    for i in range(dims[0]):
        for j in range(dims[1]):
            for k in range(dims[2]):
                yield tuple(l + (index + 0.5) * size for l, index, size in zip(low, (i, j, k), sizes))


def union_box(a, b):
    return (tuple(min(x, y) for x, y in zip(a[0], b[0])), tuple(max(x, y) for x, y in zip(a[1], b[1])))


def sample_regions(state_a, state_b, box, budget, max_cells=MAX_SAMPLES):
    """Classify a grid over *box*; each ``state`` maps a point to probe_geometry's words.

    A cell on a boundary, unread, or not reached before the budget ran out is
    ambiguous or unsampled rather than a guess; ``grid_estimate`` turns those
    into uncertainty. Returns counts, the cell volume, and each difference's box.
    """
    low, high = box
    dims = grid_dims(low, high, max_cells)
    half = tuple((h - l) / n / 2.0 for l, h, n in zip(low, high, dims))
    counts = {"both": 0, "a_only": 0, "b_only": 0, "neither": 0, "ambiguous": 0}
    boxes = {"a_only": None, "b_only": None}
    done = 0
    for point in cell_centers(low, high, dims):
        if budget.exhausted(done):
            break
        done += 1
        a, b = state_a(point), state_b(point)
        if a not in ("inside", "outside") or b not in ("inside", "outside"):
            counts["ambiguous"] += 1
            continue
        region = {(True, True): "both", (True, False): "a_only", (False, True): "b_only",
                  (False, False): "neither"}[(a == "inside", b == "inside")]
        counts[region] += 1
        if region in boxes:
            cell = (sub(point, half), add(point, half))
            boxes[region] = cell if boxes[region] is None else union_box(boxes[region], cell)
    total = math.prod(dims)
    counts["unsampled"] = total - done
    return {"counts": counts, "total": total, "cell_volume": 8.0 * math.prod(half), "boxes": boxes}


def grid_estimate(counts, cell_volume):
    """Volumes from cell counts, with an error bar.

    Unread cells could each be either way. Differing cells sit mostly along a
    boundary, so they carry a square-root term, and one cell is the floor: a
    difference smaller than a cell can fall between the samples.
    """
    differing = counts["a_only"] + counts["b_only"]
    return {
        "overlap": counts["both"] * cell_volume,
        "a_only": counts["a_only"] * cell_volume,
        "b_only": counts["b_only"] * cell_volume,
        "uncertainty": cell_volume * (1.0 + math.sqrt(differing) + counts["ambiguous"] + counts["unsampled"]),
    }


# -- The two methods ------------------------------------------------------------


def _unmeasured(method):
    empty = {"volume": None, "lumps": [], "lump_count": 0}
    return {"method": method, "overlap": None, "a_only": empty, "b_only": empty, "uncertainty": 0.0,
            "gaps": (None, None), "trusted": False}


def volume_of(body):
    """A boolean result's volume; an empty result is zero, not an error."""
    if not strict_items(safe(body, "lumps")):
        return 0.0
    volume = safe(body, "volume")
    if volume is None:
        raise RuntimeError("a boolean result has no readable volume")
    return volume


def region_of(body, max_lumps):
    """Where a difference is: its volume and its largest lumps."""
    lumps = [{"volume": safe(lump, "volume") or 0.0, "bbox": bbox(lump)} for lump in strict_items(safe(body, "lumps"))]
    return {"volume": volume_of(body), "lumps": capped_lumps(lumps, max_lumps), "lump_count": len(lumps)}


def boolean_parts(copies, a, b, opts):
    """``A∩B``, ``A-B`` and ``B-A`` on fresh copies, since a boolean modifies its target.

    Bodies whose boxes do not touch skip the booleans: the overlap is empty and
    each difference is the body itself. Raises RuntimeError when one fails.
    """
    may_overlap = bbox_overlap(a["bbox"], b["bbox"])

    def apply(target, tool, kind):
        body = copies.fresh(target)
        if may_overlap and not copies.combine(body, copies.fresh(tool), kind):
            raise RuntimeError(f"{kind} of {target} with {tool} failed")
        return body

    overlap = volume_of(apply("a", "b", INTERSECTION)) if may_overlap else 0.0
    a_only = region_of(apply("a", "b", DIFFERENCE), opts["max_lumps"])
    b_only = region_of(apply("b", "a", DIFFERENCE), opts["max_lumps"])
    gap_a, gap_b, ok = consistent(a["volume"], b["volume"], overlap, a_only["volume"], b_only["volume"],
                                  opts["volume_tolerance"])
    return {"method": "temporary_brep", "overlap": overlap, "a_only": a_only, "b_only": b_only, "uncertainty": 0.0,
            "gaps": (gap_a, gap_b), "trusted": ok}


def sampled_parts(state_a, state_b, a, b, budget):
    """Estimate the same volumes from a containment grid. Returns (parts, grid)."""
    grid = sample_regions(state_a, state_b, union_box(a["bbox"], b["bbox"]), budget)
    estimate = grid_estimate(grid["counts"], grid["cell_volume"])

    def region(key):
        box = grid["boxes"][key]
        lumps = [] if box is None else [{"volume": estimate[key], "bbox": box}]
        return {"volume": estimate[key], "lumps": lumps, "lump_count": len(lumps)}

    parts = {"method": "sampled", "overlap": estimate["overlap"], "a_only": region("a_only"), "b_only": region("b_only"),
             "uncertainty": estimate["uncertainty"], "gaps": (None, None), "trusted": True}
    return parts, grid


def measure(entry, offset):
    """What a report says about one body, in cm; *offset* places its box where it was compared."""
    body, box = entry["_body"], entry["bbox"]
    return {
        "ref": {"occurrence_path": entry["occurrence_path"], "body_name": entry["body"], "body_index": entry["index"]},
        "volume": safe(body, "volume"),
        "area": safe(body, "area"),
        "faces": safe(safe(body, "faces"), "count"),
        "lumps": safe(safe(body, "lumps"), "count"),
        "bbox": None if box is None else (add(box[0], offset), add(box[1], offset)),
    }


# -- Report ---------------------------------------------------------------------


def _box_out(box, units):
    return None if box is None else [[to_units(c, units) for c in corner] for corner in box]


def _body_out(body, units):
    return {"ref": body["ref"], "volume": to_units(body["volume"], units, 3), "area": to_units(body["area"], units, 2),
            "faces": body["faces"], "lumps": body["lumps"], "bbox": _box_out(body["bbox"], units)}


def _region_out(region, limit, units):
    return {"volume": to_units(region["volume"], units, 3),
            "lumps": [{"volume": to_units(lump["volume"], units, 3), "bbox": _box_out(lump["bbox"], units)}
                      for lump in region["lumps"][:limit]],
            "lump_count": region["lump_count"]}


def _percent(part, whole):
    return round(100.0 * part / whole, 3) if part is not None and whole else None


def build_report(a, b, parts, opts, warnings):
    units = opts["units"]
    overlap, a_only, b_only = parts["overlap"], parts["a_only"]["volume"], parts["b_only"]["volume"]
    difference = None if a_only is None or b_only is None else a_only + b_only
    shift = bbox_shift(a["bbox"], b["bbox"])
    volume_delta = None if a["volume"] is None or b["volume"] is None else b["volume"] - a["volume"]
    gap_a, gap_b = parts["gaps"]
    return {
        "verdict": verdict(difference if parts["trusted"] else None, shift, opts["volume_tolerance"],
                           opts["length_tolerance"], parts["uncertainty"]),
        "units": unit_labels(units),
        "method": parts["method"],
        "tolerances": {"volume": to_units(opts["volume_tolerance"], units, 3),
                       "length": to_units(opts["length_tolerance"], units)},
        "translate_b": [to_units(c, units) for c in opts["translate_b"]],
        "a": _body_out(a, units),
        "b": _body_out(b, units),
        "delta": {"volume": to_units(volume_delta, units, 3), "percent": _percent(volume_delta, a["volume"]),
                  "bbox_max_shift": to_units(shift, units)},
        "overlap": {"volume": to_units(overlap, units, 3), "percent_of_a": _percent(overlap, a["volume"]),
                    "percent_of_b": _percent(overlap, b["volume"])},
        "symmetric_difference": {"volume": to_units(difference, units, 3),
                                 "a_only": _region_out(parts["a_only"], opts["max_lumps"], units),
                                 "b_only": _region_out(parts["b_only"], opts["max_lumps"], units)},
        "consistency": {"a_minus_overlap_vs_a_only": to_units(gap_a, units, 3),
                        "b_minus_overlap_vs_b_only": to_units(gap_b, units, 3),
                        "ok": parts["trusted"] if parts["method"] == "temporary_brep" else None},
        "warnings": warnings,
    }


def compare(entry_a, entry_b, opts, copies, states, budget):
    """Measure both bodies and try booleans, then the grid. Returns the report.

    *states* is ``(state_a, state_b)``; state_b takes points in b's compared
    position. Nothing here raises for a geometry failure: that is an ``unknown``.
    """
    a = measure(entry_a, (0.0, 0.0, 0.0))
    b = measure(entry_b, opts["translate_b"])
    warnings = []
    try:
        parts = boolean_parts(copies, a, b, opts)
        problem = None if parts["trusted"] else "the booleans disagree with the body volumes"
    except RuntimeError as exc:
        parts, problem = _unmeasured("temporary_brep"), f"a boolean failed: {exc}"
    if problem is None:
        return build_report(a, b, parts, opts, warnings)
    warnings.append(problem)
    if not opts["sample_fallback"]:
        return build_report(a, b, parts, opts, warnings)
    if a["bbox"] is None or b["bbox"] is None:
        warnings.append("no bounding box to sample over")
        return build_report(a, b, _unmeasured("sampled"), opts, warnings)
    try:
        parts, grid = sampled_parts(states[0], states[1], a, b, budget)
    except RuntimeError as exc:
        warnings.append(f"sampling failed: {exc}")
        return build_report(a, b, _unmeasured("sampled"), opts, warnings)
    units = opts["units"]
    sampled = grid["total"] - grid["counts"]["unsampled"]
    warnings.append(f"sampled {sampled} of {grid['total']} cells of {to_units(grid['cell_volume'], units, 3)} "
                    f"{units}^3; volumes are estimates, +/- {to_units(parts['uncertainty'], units, 3)} {units}^3, "
                    "and a difference smaller than a cell can be missed")
    return build_report(a, b, parts, opts, warnings)


# -- Fusion layer ---------------------------------------------------------------


def _translation(vector):
    matrix = adsk.core.Matrix3D.create()
    matrix.translation = adsk.core.Vector3D.create(*vector)
    return matrix


class TemporaryCopies:
    """Fresh world-space copies of the two bodies, b at its compared position."""

    def __init__(self, manager, body_a, body_b, translate):
        self._manager = manager
        self._bodies = {"a": body_a, "b": body_b}
        self._translate = translate

    def fresh(self, side):
        copy = temp_copy(self._manager, self._bodies[side])
        if side == "b" and any(self._translate) and not self._manager.transform(copy, _translation(self._translate)):
            raise RuntimeError("translate_b could not be applied to the copy")
        return copy

    def combine(self, target, tool, kind):
        return self._manager.booleanOperation(target, tool, getattr(adsk.fusion.BooleanTypes, kind))


def compare_bodies(arguments):
    """Compare two bodies of the active design without changing it."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        started = time.monotonic()
        budget = Budget()
        design = adsk.fusion.Design.cast(get_app().activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        root = design.rootComponent
        entry_a, entry_b, hidden, problem = pick_pair(list(body_entries(root)), opts["a"], opts["b"])
        if problem:
            return error_result(json.dumps(problem, indent=2))
        copies = TemporaryCopies(adsk.fusion.TemporaryBRepManager.get(), entry_a["_body"], entry_b["_body"],
                                 opts["translate_b"])
        probe = FusionProbe(root, [entry_a, entry_b])
        states = (lambda p: probe.contains(0, p), lambda p: probe.contains(1, sub(p, opts["translate_b"])))
        report = compare(entry_a, entry_b, opts, copies, states, budget)
        report["warnings"] = hidden + report["warnings"]
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        log(f"[MCP] compare_bodies: {report['verdict']} by {report['method']} in {report['elapsed_seconds']} s")
        return ok_result(report)
    except Exception as exc:
        log(f"[MCP] compare_bodies failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
