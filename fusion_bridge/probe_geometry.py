"""Read-only geometry sampling of the active design: probe_geometry.

Answers "is there material here?" questions in world coordinates: point
containment, ordered ray hits, grids of either, and solid intervals along an
axis (radial) or outward from it (axial). Nothing here changes the design.

Two API facts shape the code. ``findBRepUsingRay`` returns an entity once, for
its FIRST intersection, so one cast never shows where a ray leaves a body:
``ray_crossings`` re-casts from just past each hit to collect them all. Crossings
alone can also mislead (a tangent touch is a crossing with no material behind
it), so ``solid_intervals`` decides each span by containment at its midpoint.

Everything down to the Fusion layer takes plain tuples plus ``cast`` and
``contains`` callables, so it is testable with fakes. Internal values are cm;
arguments are converted on the way in and results on the way out.
"""

import json
import math
import time
import traceback

import adsk.core
import adsk.fusion

from .common.api import safe
from .common.args import is_finite_number, is_int
from .common.bodies import ROOT_PATH, body_entries, label, parse_body_ref, resolve_targets
from .common.geometry import CM_PER_UNIT, along, cross, dot, from_cm, scaled, unit, xyz
from .common.results import error_result, ok_result
from .dispatch import get_app, log

ACTIONS = ("point", "ray", "grid", "radial", "axial")

MAX_SAMPLES = 2000
MAX_HITS_PER_RAY = 200
# Under the 120 s main-thread limit, leaving room to enumerate bodies and reply.
TIME_BUDGET_S = 90.0
DEFAULT_AXIAL_ANGLES = (0.0, 90.0, 180.0, 270.0)

# How far past a hit the next cast starts. It must clear the ray search's own
# proximity tolerance: a surface that close behind the start would be reported
# again and use up that body's single result. 10 um is far thinner than any wall.
MARCH_STEP_CM = 1e-3
# Spans shorter than this are the same place; hits this close are one hit.
TOUCH_CM = 1e-4
# Keeps a default sampling range outside the bodies, and absorbs rounding in
# the bounding-box rejection test.
BBOX_MARGIN_CM = 1e-2

GRID_SAMPLES = ("ray", "point")
GRID_SYMBOLS = {"inside": "#", "on": "+", "unknown": "?", "outside": "."}
_STATE_RANK = {"outside": 0, "unknown": 1, "on": 2, "inside": 3}

_COMMON_FIELDS = {"action", "units", "bodies"}
_ACTION_FIELDS = {
    "point": ({"points"}, set()),
    "ray": ({"origin", "direction"}, set()),
    "grid": ({"origin", "u", "v", "rows", "columns"}, {"sample", "direction"}),
    "radial": ({"center", "axis", "radii", "angles"}, {"reference", "axial_range"}),
    "axial": ({"center", "axis", "stations"}, {"angles", "reference", "max_radius"}),
}


class ProbeError(Exception):
    """A request that cannot be sampled, reported to the caller as plain text."""


# -- Frames and spans (no Fusion) ----------------------------------------------


def perpendicular_frame(axis, reference=None):
    """Right-handed (axis, reference, binormal) of unit vectors.

    *reference* is projected perpendicular to the axis and sets angle 0; without
    one, world X is used, or world Y when the axis lies close to X. Returns None
    when the reference is parallel to the axis.
    """
    axis = unit(axis)
    if reference is None:
        reference = (0.0, 1.0, 0.0) if abs(axis[0]) > 0.9 else (1.0, 0.0, 0.0)
    ref = unit(along(reference, axis, -dot(reference, axis)))
    if ref is None:
        return None
    return axis, ref, cross(axis, ref)


def radial_direction(frame, angle_deg):
    """Direction at *angle_deg* from the reference, right-handed about the axis."""
    _, ref, binormal = frame
    a = math.radians(angle_deg)
    return tuple(math.cos(a) * r + math.sin(a) * b for r, b in zip(ref, binormal))


def grid_points(origin, u, v, rows, columns):
    """Yield (row, column, point) row by row; corners included, rows step along v."""
    for row in range(rows):
        fv = row / (rows - 1) if rows > 1 else 0.0
        for column in range(columns):
            fu = column / (columns - 1) if columns > 1 else 0.0
            yield row, column, tuple(o + a * fu + b * fv for o, a, b in zip(origin, u, v))


def bbox_corners(box):
    lo, hi = box
    return [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]


def in_bbox(box, p, margin=BBOX_MARGIN_CM):
    lo, hi = box
    return all(a - margin <= c <= b + margin for a, b, c in zip(lo, hi, p))


def axial_extent(bboxes, center, axis):
    """(min, max) of the boxes measured along *axis* from *center*; None if no boxes."""
    heights = [dot(tuple(c - o for c, o in zip(corner, center)), axis)
               for box in bboxes for corner in bbox_corners(box)]
    return (min(heights), max(heights)) if heights else None


def radial_extent(bboxes, center, axis):
    """Largest distance from the axis line to a box corner; None if no boxes."""
    reach = None
    for box in bboxes:
        for corner in bbox_corners(box):
            offset = tuple(c - o for c, o in zip(corner, center))
            perpendicular = along(offset, axis, -dot(offset, axis))
            distance = math.sqrt(dot(perpendicular, perpendicular))
            reach = distance if reach is None else max(reach, distance)
    return reach


def merge_intervals(intervals, touch=TOUCH_CM):
    """Sort [start, end] spans and join those that overlap or touch."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + touch:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


# -- Unit conversion and formatting (no Fusion) --------------------------------


def point_out(p, units):
    return [from_cm(c, units) for c in p]


def intervals_out(intervals, units):
    return [[from_cm(a, units), from_cm(b, units)] for a, b in intervals]


def direction_out(v):
    return [round(c, 6) + 0.0 for c in v]


def frame_out(center, frame, units):
    axis, reference, binormal = frame
    return {"center": point_out(center, units), "axis": direction_out(axis),
            "reference": direction_out(reference), "binormal": direction_out(binormal)}


# -- Argument validation (no Fusion) -------------------------------------------


def _vec3(arguments, key):
    value = arguments[key]
    if not isinstance(value, list) or len(value) != 3 or not all(is_finite_number(c) for c in value):
        return None, f"Error: '{key}' must be an array of three finite numbers [x, y, z]"
    return tuple(float(c) for c in value), None


def _direction(arguments, key):
    value, err = _vec3(arguments, key)
    if err:
        return None, err
    value = unit(value)
    if value is None:
        return None, f"Error: '{key}' must not be a zero vector"
    return value, None


def _numbers(arguments, key, minimum=None):
    value = arguments[key]
    if not isinstance(value, list) or not value or not all(is_finite_number(c) for c in value):
        return None, f"Error: '{key}' must be a non-empty array of finite numbers"
    if minimum is not None and any(c < minimum for c in value):
        return None, f"Error: every value in '{key}' must be at least {minimum}"
    return [float(c) for c in value], None


def _count(arguments, key):
    value = arguments[key]
    if not is_int(value) or not 1 <= value <= MAX_SAMPLES:
        return None, f"Error: '{key}' must be an integer from 1 to {MAX_SAMPLES}"
    return value, None


def _bodies_arg(arguments):
    """Requested targets as [(occurrence_path, body_name, body_index)], or None for the default."""
    if "bodies" not in arguments:
        return None, None
    value = arguments["bodies"]
    if not isinstance(value, list) or not value:
        return None, "Error: 'bodies' must be a non-empty array; omit it for all visible solids"
    wanted = []
    for item in value:
        try:
            ref = parse_body_ref(item, "each 'bodies' entry")
        except ValueError as exc:
            return None, f"Error: {exc}"
        if ref not in wanted:
            wanted.append(ref)
    return wanted, None


def _validate_point(arguments, cm):
    value = arguments["points"]
    if not isinstance(value, list) or not value:
        return None, "Error: 'points' must be a non-empty array of [x, y, z] points"
    points = []
    for item in value:
        if not isinstance(item, list) or len(item) != 3 or not all(is_finite_number(c) for c in item):
            return None, "Error: each entry of 'points' must be an array of three finite numbers [x, y, z]"
        points.append(scaled(item, cm))
    return {"points": points}, None


def _validate_ray(arguments, cm):
    origin, err = _vec3(arguments, "origin")
    if err:
        return None, err
    direction, err = _direction(arguments, "direction")
    if err:
        return None, err
    return {"origin": scaled(origin, cm), "direction": direction}, None


def _validate_grid(arguments, cm):
    opts = {}
    for key in ("origin", "u", "v"):
        value, err = _vec3(arguments, key)
        if err:
            return None, err
        opts[key] = scaled(value, cm)
    for key in ("rows", "columns"):
        opts[key], err = _count(arguments, key)
        if err:
            return None, err
    sample = arguments.get("sample", "ray")
    if sample not in GRID_SAMPLES:
        return None, f"Error: 'sample' must be one of {list(GRID_SAMPLES)}, got {sample!r}"
    opts["sample"] = sample
    if sample == "point":
        if "direction" in arguments:
            return None, "Error: 'direction' only applies to grid sample 'ray'"
        return opts, None
    if "direction" in arguments:
        opts["direction"], err = _direction(arguments, "direction")
        if err:
            return None, err
    else:
        opts["direction"] = unit(cross(opts["u"], opts["v"]))
        if opts["direction"] is None:
            return None, "Error: 'u' and 'v' are parallel or zero; pass 'direction' for the rays"
    return opts, None


def _validate_frame(arguments, cm):
    """Return ({center, frame}, error_text) for radial and axial."""
    center, err = _vec3(arguments, "center")
    if err:
        return None, err
    axis, err = _direction(arguments, "axis")
    if err:
        return None, err
    reference = None
    if "reference" in arguments:
        reference, err = _direction(arguments, "reference")
        if err:
            return None, err
    frame = perpendicular_frame(axis, reference)
    if frame is None:
        return None, "Error: 'reference' must not be parallel to 'axis'"
    return {"center": scaled(center, cm), "frame": frame}, None


def _validate_radial(arguments, cm):
    opts, err = _validate_frame(arguments, cm)
    if err:
        return None, err
    radii, err = _numbers(arguments, "radii", minimum=0)
    if err:
        return None, err
    opts["radii"] = scaled(radii, cm)
    opts["angles"], err = _numbers(arguments, "angles")
    if err:
        return None, err
    opts["axial_range"] = None
    if "axial_range" in arguments:
        span, err = _numbers(arguments, "axial_range")
        if err:
            return None, err
        if len(span) != 2 or span[0] >= span[1]:
            return None, "Error: 'axial_range' must be [start, end] with start below end"
        opts["axial_range"] = scaled(span, cm)
    return opts, None


def _validate_axial(arguments, cm):
    opts, err = _validate_frame(arguments, cm)
    if err:
        return None, err
    stations, err = _numbers(arguments, "stations")
    if err:
        return None, err
    opts["stations"] = scaled(stations, cm)
    opts["angles"] = list(DEFAULT_AXIAL_ANGLES)
    if "angles" in arguments:
        opts["angles"], err = _numbers(arguments, "angles")
        if err:
            return None, err
    opts["max_radius"] = None
    if "max_radius" in arguments:
        value = arguments["max_radius"]
        if not is_finite_number(value) or value <= 0:
            return None, "Error: 'max_radius' must be a positive number"
        opts["max_radius"] = value * cm
    return opts, None


_VALIDATORS = {"point": _validate_point, "ray": _validate_ray, "grid": _validate_grid,
               "radial": _validate_radial, "axial": _validate_axial}


def validate_args(arguments):
    """Return (options in cm, error_text)."""
    if not isinstance(arguments, dict):
        return None, "Error: arguments must be an object"
    units = arguments.get("units")
    if not isinstance(units, str) or units not in CM_PER_UNIT:
        return None, f"Error: 'units' is required and must be one of {sorted(CM_PER_UNIT)}, got {units!r}"
    action = arguments.get("action")
    if action not in ACTIONS:
        return None, f"Error: 'action' must be one of {list(ACTIONS)}, got {action!r}"
    required, optional = _ACTION_FIELDS[action]
    missing = sorted(required - set(arguments))
    if missing:
        return None, f"Error: action '{action}' requires {missing}"
    extra = sorted(set(arguments) - required - optional - _COMMON_FIELDS)
    if extra:
        return None, f"Error: action '{action}' does not take {extra}"
    bodies, err = _bodies_arg(arguments)
    if err:
        return None, err
    opts, err = _VALIDATORS[action](arguments, CM_PER_UNIT[units])
    if err:
        return None, err
    opts.update(action=action, units=units, bodies=bodies)
    return opts, None


# -- Choosing targets (no Fusion) ----------------------------------------------


def match_hit(targets, path, name, point_state):
    """Index of the target a ray hit belongs to, or None.

    Occurrence bodies should come back as proxies, identified by path and name.
    A hit with no assembly context reads as a root body; such a hit may still be
    a native body seen through an occurrence, so a same-named target only claims
    it when the hit point lies on that target. *point_state(index)* tells.
    """
    exact = [i for i, t in enumerate(targets) if t["occurrence_path"] == path and t["body"] == name]
    if len(exact) == 1:
        return exact[0]
    candidates = exact
    if not candidates and path == ROOT_PATH:
        candidates = [i for i, t in enumerate(targets) if t["body"] == name]
    return next((i for i in candidates if point_state(i) == "on"), None)


# -- Sampling (no Fusion; cast and contains are injected) ----------------------


def ray_crossings(cast, origin, direction, t_start=0.0, t_end=math.inf, max_hits=MAX_HITS_PER_RAY):
    """Every crossing of the targets along a ray: ([(t, target_index)], capped).

    *cast(origin, direction)* returns [(distance, target_index)] with each target
    at most once, for its first intersection. So the ray is re-cast from just past
    the nearest hit until nothing is left. All hits of a cast are kept because
    bodies in contact are hit at the same distance, and stepping past one must
    not lose the other. A body's later crossings can appear in a later cast only.
    """
    crossings = []
    cursor = t_start
    for _ in range(2 * max_hits):
        hits = [(cursor + t, index) for t, index in cast(along(origin, direction, cursor), direction)]
        hits = [h for h in hits if h[0] <= t_end]
        if not hits:
            return sorted(crossings), False
        for t, index in hits:
            if not any(i == index and abs(t - seen) <= TOUCH_CM for seen, i in crossings):
                crossings.append((t, index))
        if len(crossings) >= max_hits:
            return sorted(crossings)[:max_hits], True
        cursor = min(t for t, _ in hits) + MARCH_STEP_CM
    return sorted(crossings), True


def solid_intervals(cast, contains, origin, direction, t_start, t_end, target_count):
    """Material along a ray between t_start and t_end.

    Returns ({target_index: [[start, end], ...]}, flags). Only bodies with
    material appear. Flags: 'coincident' (the ray runs along a surface; such a
    span counts as material), 'unknown' (Fusion could not classify a span, which
    then does not count), 'hit_cap' (too many crossings; the far end is unreliable).
    """
    crossings, capped = ray_crossings(cast, origin, direction, t_start, t_end)
    flags = {"hit_cap"} if capped else set()
    stops = sorted({t_start, t_end, *(t for t, _ in crossings if t_start < t < t_end)})
    spans = {}
    for a, b in zip(stops, stops[1:]):
        if b - a <= TOUCH_CM:
            continue
        middle = along(origin, direction, (a + b) / 2)
        for index in range(target_count):
            state = contains(index, middle)
            if state in ("inside", "on"):
                spans.setdefault(index, []).append((a, b))
            if state == "on":
                flags.add("coincident")
            elif state == "unknown":
                flags.add("unknown")
    return {index: merge_intervals(found) for index, found in spans.items()}, flags


class Budget:
    """Stops a sampling loop at the sample cap or the time budget."""

    def __init__(self, max_samples=MAX_SAMPLES, seconds=TIME_BUDGET_S, clock=time.monotonic):
        self.max_samples = max_samples
        self._clock = clock
        self._deadline = clock() + seconds
        self.reason = None

    def exhausted(self, done):
        if done >= self.max_samples:
            self.reason = "sample_cap"
        elif self._clock() >= self._deadline:
            self.reason = "time_budget"
        return self.reason is not None


def _progress(requested, done, budget):
    result = {"samples_requested": requested, "samples_done": done, "truncated": done < requested}
    if done < requested:
        result["truncation_reason"] = budget.reason
    return result


def run_point(opts, targets, probe, budget):
    units, rows = opts["units"], []
    for p in opts["points"]:
        if budget.exhausted(len(rows)):
            break
        row = {"point": point_out(p, units)}
        for index, target in enumerate(targets):
            state = probe.contains(index, p)
            if state != "outside":
                row.setdefault(state, []).append(label(target))
        rows.append(row)
    return {"results": rows, "legend": "a body not listed for a point is outside it",
            **_progress(len(opts["points"]), len(rows), budget)}


def run_ray(opts, targets, probe, budget):
    units, origin, direction = opts["units"], opts["origin"], opts["direction"]
    crossings, capped = ray_crossings(probe.cast, origin, direction)
    hits = [{"distance": from_cm(t, units), "point": point_out(along(origin, direction, t), units),
             "body": targets[index]["body"], "occurrence_path": targets[index]["occurrence_path"]}
            for t, index in crossings]
    result = {"origin": point_out(origin, units), "direction": direction_out(direction),
              "hit_count": len(hits), "hits": hits, "truncated": capped}
    if capped:
        result["truncation_reason"] = "hit_cap"
    return result


def run_grid(opts, targets, probe, budget):
    units, rows, columns = opts["units"], opts["rows"], opts["columns"]
    by_ray = opts["sample"] == "ray"
    done, matrix, owners = 0, [], []
    for row, _, p in grid_points(opts["origin"], opts["u"], opts["v"], rows, columns):
        if budget.exhausted(done):
            break
        if row == len(matrix):
            matrix.append([] if by_ray else "")
            owners.append([])
        if by_ray:
            hits = probe.cast(p, opts["direction"])
            first = min(hits, default=None)
            matrix[row].append(None if first is None else from_cm(first[0], units))
            owners[row].append(None if first is None else first[1])
        else:
            states = [probe.contains(index, p) for index in range(len(targets))]
            matrix[row] += GRID_SYMBOLS[max(states, key=_STATE_RANK.__getitem__)]
        done += 1
    result = {"sample": opts["sample"], "origin": point_out(opts["origin"], units),
              "u": point_out(opts["u"], units), "v": point_out(opts["v"], units),
              "rows": rows, "columns": columns,
              "layout": "row index steps along v, column index along u; both include the far edge",
              **_progress(rows * columns, done, budget)}
    if by_ray:
        result.update({"direction": direction_out(opts["direction"]), "first_hit_distance": matrix,
                       "first_hit_body": owners,
                       "legend": "null = no hit; first_hit_body indexes 'bodies'"})
    else:
        result.update({"containment": matrix,
                       "legend": "over all target bodies: # inside, + on a surface, ? unknown, . outside"})
    return result


def _interval_row(found, flags, targets, units):
    row = {"intervals": intervals_out(merge_intervals(s for spans in found.values() for s in spans), units)}
    if len(found) > 1:
        row["by_body"] = {label(targets[i]): intervals_out(spans, units) for i, spans in sorted(found.items())}
    if flags:
        row["flags"] = sorted(flags)
    return row


def run_radial(opts, targets, probe, budget):
    units, center, frame = opts["units"], opts["center"], opts["frame"]
    axis = frame[0]
    span = opts["axial_range"]
    if span is None:
        extent = axial_extent([t["bbox"] for t in targets if t["bbox"]], center, axis)
        if extent is None:
            raise ProbeError("no target has a bounding box; pass 'axial_range'")
        span = (extent[0] - BBOX_MARGIN_CM, extent[1] + BBOX_MARGIN_CM)
    rows = []
    samples = [(r, a) for r in opts["radii"] for a in opts["angles"]]
    for radius, angle in samples:
        if budget.exhausted(len(rows)):
            break
        origin = along(center, radial_direction(frame, angle), radius)
        found, flags = solid_intervals(probe.cast, probe.contains, origin, axis, span[0], span[1], len(targets))
        rows.append({"radius": from_cm(radius, units), "angle": angle, **_interval_row(found, flags, targets, units)})
    return {"frame": frame_out(center, frame, units), "axial_range": [from_cm(c, units) for c in span],
            "results": rows, "legend": "intervals are [start, end] heights along axis from center",
            **_progress(len(samples), len(rows), budget)}


def run_axial(opts, targets, probe, budget):
    units, center, frame = opts["units"], opts["center"], opts["frame"]
    axis = frame[0]
    reach = opts["max_radius"]
    if reach is None:
        extent = radial_extent([t["bbox"] for t in targets if t["bbox"]], center, axis)
        if extent is None:
            raise ProbeError("no target has a bounding box; pass 'max_radius'")
        reach = extent + BBOX_MARGIN_CM
    rows = []
    samples = [(s, a) for s in opts["stations"] for a in opts["angles"]]
    for station, angle in samples:
        if budget.exhausted(len(rows)):
            break
        origin = along(center, axis, station)
        outward = radial_direction(frame, angle)
        found, flags = solid_intervals(probe.cast, probe.contains, origin, outward, 0.0, reach, len(targets))
        rows.append({"station": from_cm(station, units), "angle": angle, **_interval_row(found, flags, targets, units)})
    return {"frame": frame_out(center, frame, units), "max_radius": from_cm(reach, units),
            "results": rows, "legend": "intervals are [inner, outer] radii from the axis at that station and angle",
            **_progress(len(samples), len(rows), budget)}


_RUNNERS = {"point": run_point, "ray": run_ray, "grid": run_grid, "radial": run_radial, "axial": run_axial}


def sample(opts, targets, probe, budget):
    """Run the requested action and add what every result carries."""
    payload = {"action": opts["action"], "units": opts["units"], "bodies": [label(t) for t in targets]}
    payload.update(_RUNNERS[opts["action"]](opts, targets, probe, budget))
    return payload


# -- Fusion layer --------------------------------------------------------------


class FusionProbe:
    """The only code that asks Fusion about geometry; all coordinates are world cm."""

    def __init__(self, root, targets):
        self._root = root
        self._targets = targets
        # The live-proven call sees visible bodies; a hidden target needs the wider search.
        self._visible_only = all(t["visible"] for t in targets)
        states = adsk.fusion.PointContainment
        self._states = {states.PointInsidePointContainment: "inside",
                        states.PointOnPointContainment: "on",
                        states.PointOutsidePointContainment: "outside"}
        self.other_bodies = set()

    def contains(self, index, p):
        target = self._targets[index]
        if target["bbox"] and not in_bbox(target["bbox"], p):
            return "outside"
        return self._states.get(target["_body"].pointContainment(adsk.core.Point3D.create(*p)), "unknown")

    def cast(self, origin, direction):
        """[(distance, target_index)] for this cast, nearest first; see ray_crossings."""
        hit_points = adsk.core.ObjectCollection.create()
        found = self._root.findBRepUsingRay(
            adsk.core.Point3D.create(*origin), adsk.core.Vector3D.create(*direction),
            adsk.fusion.BRepEntityTypes.BRepBodyEntityType, -1, self._visible_only, hit_points)
        count = found.count if found is not None else 0
        if hit_points.count != count:
            raise RuntimeError(f"findBRepUsingRay returned {count} bodies but {hit_points.count} hit points")
        hits = []
        for i in range(count):
            body, p = found.item(i), xyz(hit_points.item(i))
            context = safe(body, "assemblyContext")
            path = safe(context, "fullPathName") if context is not None else ROOT_PATH
            name = safe(body, "name")
            index = match_hit(self._targets, path, name, lambda t: self.contains(t, p))
            if index is None:
                self.other_bodies.add(f"{path}/{name}")
            else:
                hits.append((dot(tuple(a - b for a, b in zip(p, origin)), direction), index))
        return sorted(hits)


def probe_geometry(arguments):
    """Sample the active design's bodies: containment, ray hits, grids, intervals."""
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
        targets, warnings, problem = resolve_targets(list(body_entries(root)), opts["bodies"])
        if problem:
            return error_result(json.dumps(problem, indent=2))
        probe = FusionProbe(root, targets)
        payload = sample(opts, targets, probe, budget)
        if probe.other_bodies:
            payload["other_bodies_hit"] = sorted(probe.other_bodies)[:20]
        if warnings:
            payload["warnings"] = warnings
        payload["elapsed_seconds"] = round(time.monotonic() - started, 2)
        # Compact on purpose: indenting a grid puts every number on its own line.
        return ok_result(payload, indent=None)
    except ProbeError as exc:
        return error_result(f"Error: {exc}")
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] probe_geometry failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")
