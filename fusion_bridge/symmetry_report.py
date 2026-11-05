"""How far is an assembly from a true mirror across a plane: symmetry_report.

Read-only: it measures, and every boolean runs on temporary copies that never
enter the design or the timeline.

Each visible solid is mirrored by numbers, center of mass and bounding box, and
paired with the body that best fits the mirror image. Names play no part: a body
called "Left" says nothing about where it is. Instances count per occurrence, so
the two sides of a component used on both sides are two bodies. A body straddling
the plane may pair with itself (``on_plane``).

Numbers cannot tell a drilled part from an undrilled one, so the closest pairs are
then checked by reflecting a temporary copy and comparing it by booleans, the way
``compare_bodies`` does. If the kernel refuses a reflection the report stays on
numbers and says so. A boolean that agrees decides ``true_mirror``; one that
disagrees caps a pair at ``near``, because a thin wall gives a large difference
for a small shift and the lengths say more about how far off the pair is.

Every deviation is a length. A volume difference becomes one by spreading it over
the body's size (``volume_length``): for a tube that is the difference in length.

Everything above the Fusion layer takes plain values and duck-typed collaborators,
so it is testable with fakes. Internal values are cm.
"""

import math
import time
import traceback
from collections import Counter

import adsk.core
import adsk.fusion

from . import compare_bodies as cb
from .audit_rules import Budget, fits_budget
from .common.api import attempt, safe
from .common.args import bool_arg, int_arg, is_finite_number, reject_unknown, require_object, text_arg, vector_arg
from .common.bodies import PLACEMENT_TOLERANCE_CM, SURFACE_TYPES, bbox, bbox_shift, body_entries, label
from .common.geometry import CM_PER_UNIT, UNIT_FACTORS, dot, mirror_point, sub, to_cm, unit, unit_labels, xyz
from .common.results import error_result, ok_result
from .dispatch import get_app, log

_KNOWN = {"plane", "plane_token", "point", "normal", "units", "tolerance", "include_hidden", "component_contains",
          "max_results", "time_budget_s"}

ROOT_PLANE_NORMALS = {"xy": (0.0, 0.0, 1.0), "xz": (0.0, 1.0, 0.0), "yz": (1.0, 0.0, 0.0)}

DEFAULT_TOLERANCE_IN = 0.01
DEFAULT_MAX_RESULTS = 100
MAX_RESULTS_LIMIT = 1000
DEFAULT_TIME_BUDGET_S = 90.0
MIN_TIME_BUDGET_S = 1.0
MAX_TIME_BUDGET_S = 110.0
# Pairing is fast and what it finds is the point, so it may run this far past the
# budget. MAX_TIME_BUDGET_S plus this stays under settings.MCP_MAIN_THREAD_TIMEOUT.
PAIRING_GRACE_S = 5.0

# A deviation up to this many tolerances is near; past it, different.
NEAR_FACTOR = 10.0
# Bodies further apart than this share of their volume are not two sides of one part.
MAX_VOLUME_DIFFERENCE = 0.5
# A reflected copy keeps its source's volume, up to the kernel's rounding. This
# only has to catch a body turned inside out or scaled.
REFLECTED_VOLUME_REL = 1e-4
AXIS_ALIGNED_DOT = 1.0 - 1e-12

TRUE_MIRROR, NEAR, DIFFERENT = "true_mirror", "near", "different"
POSITIVE, NEGATIVE, ON_PLANE = "positive", "negative", "on_plane"
STATUS_RANK = {DIFFERENT: 0, NEAR: 1, TRUE_MIRROR: 2}
SIDES = (POSITIVE, NEGATIVE, ON_PLANE)
BY_NUMBERS, BY_BOOLEANS = "numbers", "temporary_brep"


# -- Argument validation --------------------------------------------------------


def _plane_source(arguments, units):
    """The plane asked for: ``{"root": key}``, ``{"token": text}`` or ``{"point", "normal"}`` in cm."""
    given = [key for key in ("plane", "plane_token") if arguments.get(key) is not None]
    if arguments.get("point") is not None or arguments.get("normal") is not None:
        given.append("point and normal")
    if len(given) != 1:
        raise ValueError(f"pass exactly one of plane, plane_token, or point and normal; got {given or 'none'}")
    if given[0] == "plane":
        if arguments["plane"] not in ROOT_PLANE_NORMALS:
            raise ValueError(f"'plane' must be one of {sorted(ROOT_PLANE_NORMALS)}, got {arguments['plane']!r}")
        return {"root": arguments["plane"]}
    if given[0] == "plane_token":
        return {"token": text_arg(arguments, "plane_token")}
    if arguments.get("point") is None or arguments.get("normal") is None:
        raise ValueError("'point' and 'normal' go together: pass both")
    normal = unit(vector_arg(arguments["normal"], "normal"))
    if normal is None:
        raise ValueError("'normal' has no direction: it is a zero vector")
    return {"point": tuple(c * CM_PER_UNIT[units] for c in vector_arg(arguments["point"], "point")), "normal": normal}


def validate_arguments(arguments):
    """Normalized options, the tolerance in cm; raises ValueError with the text to report."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN)
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    tolerance = arguments.get("tolerance")
    if tolerance is not None and (not is_finite_number(tolerance) or tolerance <= 0):
        raise ValueError("'tolerance' must be a positive number")
    budget = arguments.get("time_budget_s", DEFAULT_TIME_BUDGET_S)
    if not is_finite_number(budget) or not MIN_TIME_BUDGET_S <= budget <= MAX_TIME_BUDGET_S:
        raise ValueError(f"'time_budget_s' must be a number from {MIN_TIME_BUDGET_S:g} to {MAX_TIME_BUDGET_S:g}")
    return {
        "plane": _plane_source(arguments, units),
        "units": units,
        "tolerance": to_cm(DEFAULT_TOLERANCE_IN, "in") if tolerance is None else to_cm(float(tolerance), units),
        "include_hidden": bool_arg(arguments, "include_hidden", False),
        "component_contains": text_arg(arguments, "component_contains"),
        "max_results": int_arg(arguments, "max_results", DEFAULT_MAX_RESULTS, 1, MAX_RESULTS_LIMIT),
        "time_budget_s": float(budget),
    }


# -- The plane (no Fusion) ------------------------------------------------------


def make_plane(point, normal, source):
    """``{"point", "normal", "source", "axis_aligned"}``; *normal* becomes a unit vector."""
    direction = unit(normal)
    if direction is None:
        raise ValueError("the mirror plane has no usable normal")
    return {"point": tuple(point), "normal": direction, "source": source,
            "axis_aligned": max(abs(c) for c in direction) >= AXIS_ALIGNED_DOT}


def signed_distance(point, plane):
    return dot(sub(point, plane["point"]), plane["normal"])


def box_corners(box):
    low, high = box
    return [(x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]


def mirror_box(box, plane):
    """The box around *box*'s mirrored corners.

    Exactly the mirrored box for an axis-aligned plane. For any other plane it is
    only a looser box around the mirror image.
    """
    corners = [mirror_point(corner, plane["point"], plane["normal"]) for corner in box_corners(box)]
    return (tuple(min(c[i] for c in corners) for i in range(3)), tuple(max(c[i] for c in corners) for i in range(3)))


def reflection_rows(point, normal):
    """The 4x4 matrix of the reflection across a plane, rows of a column-vector transform."""
    n = unit(normal)
    offset = 2.0 * dot(point, n)
    rows = [[(1.0 if i == j else 0.0) - 2.0 * n[i] * n[j] for j in range(3)] + [offset * n[i]] for i in range(3)]
    return rows + [[0.0, 0.0, 0.0, 1.0]]


# -- Reading bodies (no Fusion) -------------------------------------------------


def _inside(point, box, slack=PLACEMENT_TOLERANCE_CM):
    return all(box[0][i] - slack <= point[i] <= box[1][i] + slack for i in range(3))


def world_center(center, box, to_world):
    """A center of mass in world coordinates, or None.

    A center of mass lies inside its body's box. One that does not was read in
    component space, so it goes through *to_world*, which may return None.
    """
    if center is None or _inside(center, box):
        return center
    moved = to_world(center)
    return moved if moved is not None and _inside(moved, box) else None


def wanted(entry, opts):
    """Why *entry* is left out (a ``skipped`` key), or None."""
    if not entry["solid"]:
        return "not_solid"
    if not entry["visible"] and not opts["include_hidden"]:
        return "hidden"
    text = opts["component_contains"]
    if text is not None:
        names = f"{entry['component'] or ''}\n{entry['occurrence_path'] or ''}"
        if text.lower() not in names.lower():
            return "filtered"
    return None


def measured(entry, volume, center, plane):
    """What pairing needs of one body, or None when it cannot be measured."""
    box = entry["bbox"]
    if box is None or volume is None or volume <= 0:
        return None
    middle = tuple((low + high) / 2.0 for low, high in zip(*box))
    place = middle if center is None else center
    distances = [signed_distance(corner, plane) for corner in box_corners(box)]
    return {
        **entry,
        "volume": volume,
        "center": place,
        "center_estimated": center is None,
        "size": math.dist(box[0], box[1]),
        "distance": signed_distance(place, plane),
        "reach": (min(distances), max(distances)),
        "mirror_center": mirror_point(place, plane["point"], plane["normal"]),
        "mirror_box": mirror_box(box, plane),
        "exact_box": plane["axis_aligned"],
    }


def side_of(body, tolerance):
    if abs(body["distance"]) <= tolerance:
        return ON_PLANE
    return POSITIVE if body["distance"] > 0 else NEGATIVE


# -- Pairing (no Fusion) --------------------------------------------------------


def deviation(a, b, plane):
    """How far *b* is from the mirror image of *a*, in cm; None when it cannot be its partner."""
    offset = sub(b["center"], a["mirror_center"])
    center = math.hypot(*offset)
    size = max(a["size"], b["size"])
    larger = max(a["volume"], b["volume"])
    volume_share = abs(a["volume"] - b["volume"]) / larger
    if center > size or volume_share > MAX_VOLUME_DIFFERENCE:
        return None
    box = bbox_shift(a["mirror_box"], b["bbox"]) if plane["axis_aligned"] else None
    volume_length = volume_share * size
    return {
        "center": center,
        "offset": offset,
        "box": box,
        "volume": b["volume"] - a["volume"],
        "volume_share": volume_share,
        "volume_length": volume_length,
        "worst": max(center, box or 0.0, volume_length),
        "score": center + (box or 0.0) + volume_length,
    }


def classify(worst, tolerance):
    if worst <= tolerance:
        return TRUE_MIRROR
    return NEAR if worst <= tolerance * NEAR_FACTOR else DIFFERENT


def may_be_own_mirror(body, tolerance):
    """Its center is on the plane, or it runs through it. Merely touching is one half of a split part."""
    low, high = body["reach"]
    return abs(body["distance"]) <= tolerance or (low < -tolerance and high > tolerance)


def _across(a, b, tolerance):
    """Could these two be on either side? Two bodies well to one side cannot."""
    return a["distance"] * b["distance"] <= 0 or min(abs(a["distance"]), abs(b["distance"])) <= tolerance


def candidates(bodies, plane, tolerance, stop=lambda: False):
    """Every plausible pairing as ``(score, i, j, deviation)``, and whether all were looked at.

    In each pair *i* is the body further along the normal, so the two sides read
    the same way in every row.
    """
    found = []
    for i, a in enumerate(bodies):
        if stop():
            return found, False
        if may_be_own_mirror(a, tolerance):
            own = deviation(a, a, plane)
            if own is not None:
                found.append((own["score"], i, i, own))
        for j in range(i + 1, len(bodies)):
            b = bodies[j]
            if not _across(a, b, tolerance):
                continue
            first, second = (i, j) if a["distance"] >= b["distance"] else (j, i)
            gap = deviation(bodies[first], bodies[second], plane)
            if gap is not None:
                found.append((gap["score"], first, second, gap))
    return found, True


def pair_bodies(bodies, plane, tolerance, stop=lambda: False):
    """``(pairs, unmatched bodies, finished)``: the closest pairing first, each body used once."""
    found, finished = candidates(bodies, plane, tolerance, stop)
    used, pairs = set(), []
    for _, i, j, gap in sorted(found, key=lambda row: row[:3]):
        if i in used or j in used:
            continue
        used.update((i, j))
        pairs.append({"a": bodies[i], "b": bodies[j], "on_plane": i == j, "deviation": gap,
                      "status": classify(gap["worst"], tolerance), "basis": BY_NUMBERS, "geometry": None})
    return pairs, [body for index, body in enumerate(bodies) if index not in used], finished


# -- Confirming by booleans (no Fusion) -----------------------------------------


class ReflectionFailed(Exception):
    """The kernel cannot reflect a temporary body, so no pair can be confirmed."""


def difference_limit(pair, tolerance):
    """The volume that spread over the pair's size is one *tolerance* long."""
    a, b = pair["a"], pair["b"]
    return max(a["volume"], b["volume"]) * tolerance / max(a["size"], b["size"])


def check_reflected(copy_box, copy_volume, source):
    """Raise ReflectionFailed unless a reflected copy of *source* landed as its mirror image.

    A transform that quietly rotated or turned the body inside out would compare
    wrongly without looking wrong. An oblique plane has no exact box to expect.
    """
    if copy_volume is None or abs(copy_volume - source["volume"]) > REFLECTED_VOLUME_REL * source["volume"]:
        raise ReflectionFailed(f"the reflected copy has volume {copy_volume}, not {source['volume']}")
    if source["exact_box"]:
        shift = bbox_shift(copy_box, source["mirror_box"])
        if shift is None or shift > PLACEMENT_TOLERANCE_CM:
            raise ReflectionFailed("the reflected copy did not land on the mirrored box")


def apply_booleans(pair, parts, limit):
    """Record what the booleans found. Only trusted booleans change the status."""
    a_only, b_only = parts["a_only"]["volume"], parts["b_only"]["volume"]
    difference = a_only + b_only if parts["trusted"] else None
    verdict = cb.verdict(difference, 0.0, limit, math.inf)
    pair["geometry"] = {"verdict": verdict, "difference": difference, "a_only": a_only, "b_only": b_only,
                        "overlap": parts["overlap"], "limit": limit}
    if verdict == "same":
        pair["status"], pair["basis"] = TRUE_MIRROR, BY_BOOLEANS
    elif verdict == "different":
        pair["status"], pair["basis"] = (NEAR if pair["status"] == TRUE_MIRROR else pair["status"]), BY_BOOLEANS
    return verdict


def confirm_pairs(pairs, copies_for, budget, tolerance):
    """Check the closest pairs that the numbers do not already rule out.

    *copies_for* gives a pair's ``copies`` collaborator (see ``compare_bodies``);
    side ``a`` comes back reflected. One failed reflection ends the run, since no
    other pair would fare better. Returns the ``confirmation`` block.
    """
    queue = sorted((pair for pair in pairs if pair["status"] != DIFFERENT), key=lambda p: p["deviation"]["score"])
    tally = Counter()
    state = {"method": BY_BOOLEANS, "available": True, "reason": None}
    started, done = budget.elapsed(), 0
    for pair in queue:
        if budget.expired() or not fits_budget(done, budget.elapsed() - started, 1, budget.remaining()):
            break
        limit = difference_limit(pair, tolerance)
        a = {"bbox": pair["a"]["mirror_box"], "volume": pair["a"]["volume"]}
        b = {"bbox": pair["b"]["bbox"], "volume": pair["b"]["volume"]}
        try:
            parts = cb.boolean_parts(copies_for(pair), a, b, {"max_lumps": 0, "volume_tolerance": limit})
        except ReflectionFailed as exc:
            state.update(method=BY_NUMBERS, available=False, reason=str(exc))
            break
        except Exception as exc:  # a kernel failure on one pair says nothing about the next
            pair["geometry"] = {"verdict": "unknown", "error": str(exc)}
            tally["unknown"] += 1
            done += 1
            continue
        tally[apply_booleans(pair, parts, limit)] += 1
        done += 1
    return {**state, "checked": done, "same": tally["same"], "different": tally["different"],
            "unknown": tally["unknown"], "not_reached": len(queue) - done}


# -- Report ---------------------------------------------------------------------


def _ref(body, tolerance, units):
    return {"occurrence_path": body["occurrence_path"], "body_name": body["body"], "body_index": body["index"],
            "component": body["component"], "side": side_of(body, tolerance),
            "volume": cb.to_units(body["volume"], units, 3), "center": [cb.to_units(c, units) for c in body["center"]]}


def _geometry_out(found, units):
    if found is None or "error" in found:
        return found
    return {"verdict": found["verdict"], "symmetric_difference": cb.to_units(found["difference"], units, 3),
            "a_only": cb.to_units(found["a_only"], units, 3), "b_only": cb.to_units(found["b_only"], units, 3),
            "overlap": cb.to_units(found["overlap"], units, 3), "limit": cb.to_units(found["limit"], units, 3)}


def _pair_out(pair, tolerance, units):
    found = pair["deviation"]
    return {
        "status": pair["status"],
        "on_plane": pair["on_plane"],
        "basis": pair["basis"],
        "worst": cb.to_units(found["worst"], units),
        "deviation": {
            "center_of_mass": cb.to_units(found["center"], units),
            "center_offset": [cb.to_units(c, units) for c in found["offset"]],
            "bbox": cb.to_units(found["box"], units),
            "volume": cb.to_units(found["volume"], units, 3),
            "volume_percent": round(100.0 * found["volume_share"], 4),
            "volume_length": cb.to_units(found["volume_length"], units),
        },
        "geometry": _geometry_out(pair["geometry"], units),
        "a": _ref(pair["a"], tolerance, units),
        "b": _ref(pair["b"], tolerance, units),
    }


def worst_first(pairs):
    return sorted(pairs, key=lambda p: (STATUS_RANK[p["status"]], -p["deviation"]["worst"], label(p["a"])))


def summarize(bodies, pairs, unmatched):
    counts = Counter(pair["status"] for pair in pairs)
    return {"bodies": len(bodies), "pairs": len(pairs), TRUE_MIRROR: counts[TRUE_MIRROR], NEAR: counts[NEAR],
            DIFFERENT: counts[DIFFERENT], "unmatched": len(unmatched),
            ON_PLANE: sum(1 for pair in pairs if pair["on_plane"])}


def report_warnings(plane, bodies, confirmation, finished_reading, finished_pairing):
    warnings = []
    if not finished_reading:
        warnings.append("the time budget ran out while reading bodies; unmatched bodies may have partners "
                        "that were not read")
    if not finished_pairing:
        warnings.append("the time budget ran out while pairing; some bodies were not compared")
    if not plane["axis_aligned"]:
        warnings.append("the plane is not along a world axis, so bounding boxes cannot be compared; pairs rest on "
                        "center of mass, volume and booleans")
    estimated = sum(1 for body in bodies if body["center_estimated"])
    if estimated:
        warnings.append(f"{estimated} body(ies) had no readable center of mass; the middle of the bounding box "
                        "stands in for it")
    if not confirmation["available"]:
        warnings.append("temporary bodies could not be reflected, so nothing was confirmed by booleans; results "
                        f"rest on numbers, which cannot see a small feature: {confirmation['reason']}")
    elif confirmation["not_reached"]:
        warnings.append(f"{confirmation['not_reached']} pair(s) were not confirmed in time; their status rests "
                        "on numbers")
    return warnings


def build_report(plane, bodies, pairs, unmatched, confirmation, skipped, opts, finished_reading, finished_pairing,
                 elapsed_s):
    units, tolerance, limit = opts["units"], opts["tolerance"], opts["max_results"]
    ordered = worst_first(pairs)
    sides = {side: [] for side in SIDES}
    for body in sorted(unmatched, key=lambda b: -b["volume"]):
        sides[side_of(body, tolerance)].append(body)
    return {
        "summary": summarize(bodies, pairs, unmatched),
        "complete": finished_reading and finished_pairing and not skipped["unmeasured"],
        "units": unit_labels(units),
        "plane": {"source": plane["source"], "point": [cb.to_units(c, units) for c in plane["point"]],
                  "normal": [round(c, 6) + 0.0 for c in plane["normal"]], "axis_aligned": plane["axis_aligned"]},
        "tolerance": {"true_mirror": cb.to_units(tolerance, units), "near": cb.to_units(tolerance * NEAR_FACTOR, units)},
        "confirmation": confirmation,
        "pairs": [_pair_out(pair, tolerance, units) for pair in ordered[:limit]],
        "pairs_truncated": len(ordered) > limit,
        "unmatched": {side: [_ref(body, tolerance, units) for body in rows[:limit]] for side, rows in sides.items()},
        "unmatched_truncated": any(len(rows) > limit for rows in sides.values()),
        "skipped": skipped,
        "warnings": report_warnings(plane, bodies, confirmation, finished_reading, finished_pairing),
        "elapsed_s": round(elapsed_s, 2),
    }


def run(entries, plane, opts, budget, read_measures, copies_for):
    """Read, pair, confirm and report. Returns None when no body qualified.

    *entries* yields body entries; *read_measures* maps one to ``(volume, world
    center or None)``.
    """
    bodies = []
    skipped = {"hidden": 0, "not_solid": 0, "filtered": 0, "unmeasured": 0}
    finished_reading = True
    for entry in entries:
        if budget.expired():
            finished_reading = False
            break
        reason = wanted(entry, opts)
        if reason is not None:
            skipped[reason] += 1
            continue
        body = measured(entry, *read_measures(entry), plane)
        if body is None:
            skipped["unmeasured"] += 1
            continue
        bodies.append(body)
    if not bodies:
        return None
    pairs, unmatched, finished_pairing = pair_bodies(
        bodies, plane, opts["tolerance"], lambda: budget.elapsed() > budget.seconds + PAIRING_GRACE_S)
    confirmation = confirm_pairs(pairs, copies_for, budget, opts["tolerance"])
    return build_report(plane, bodies, pairs, unmatched, confirmation, skipped, opts, finished_reading,
                        finished_pairing, budget.elapsed())


# -- Fusion layer ---------------------------------------------------------------


def resolve_plane(source, design):
    """The mirror plane in world cm. Raises ValueError when a token names no usable plane."""
    if "root" in source:
        return make_plane((0.0, 0.0, 0.0), ROOT_PLANE_NORMALS[source["root"]], source["root"])
    if "point" in source:
        return make_plane(source["point"], source["normal"], "point_normal")
    found = attempt(lambda: list(design.findEntityByToken(source["token"])), []) or []
    if len(found) != 1:
        raise ValueError(f"'plane_token' names {len(found)} entities, not one plane")
    entity = found[0]
    surface = safe(entity, "geometry")
    if SURFACE_TYPES.get(safe(surface, "surfaceType")) != "plane":
        raise ValueError("'plane_token' does not name a construction plane or a planar face")
    owner = safe(entity, "component") or safe(safe(entity, "body"), "parentComponent")
    if safe(entity, "assemblyContext") is None and owner is not None and owner != design.rootComponent:
        raise ValueError("'plane_token' names a plane inside a component with no occurrence, so it has no one "
                         "place in the assembly; use the token of the plane as seen through an occurrence")
    return make_plane(xyz(surface.origin), xyz(surface.normal), safe(entity, "name") or "plane_token")


def read_center(body):
    """Center of mass as read. ``physicalProperties`` works at low accuracy, too coarse for a tolerance."""
    found = attempt(lambda: body.getPhysicalProperties(adsk.fusion.CalculationAccuracy.HighCalculationAccuracy))
    found = found or safe(body, "physicalProperties")
    point = safe(found, "centerOfMass")
    return None if point is None else xyz(point)


def _occurrence_mapper(body):
    """A function taking a component-space point to world, through *body*'s occurrence."""
    def to_world(center):
        occurrence = safe(body, "assemblyContext")
        if occurrence is None:
            return None
        point = adsk.core.Point3D.create(*center)
        return xyz(point) if point.transformBy(occurrence.transform2) else None

    return to_world


def read_measures(entry):
    body = entry["_body"]
    if entry["bbox"] is None:
        return safe(body, "volume"), None
    return safe(body, "volume"), world_center(read_center(body), entry["bbox"], _occurrence_mapper(body))


def reflection_matrix(plane):
    matrix = adsk.core.Matrix3D.create()
    for row, cells in enumerate(reflection_rows(plane["point"], plane["normal"])):
        for column, value in enumerate(cells):
            matrix.setCell(row, column, value)
    return matrix


class MirroredCopies(cb.TemporaryCopies):
    """``compare_bodies``' fresh copies, with side ``a`` reflected across the plane."""

    def __init__(self, manager, pair, matrix):
        super().__init__(manager, pair["a"]["_body"], pair["b"]["_body"], (0.0, 0.0, 0.0))
        self._matrix = matrix
        self._source = pair["a"]

    def fresh(self, side):
        copy = super().fresh(side)
        if side != "a":
            return copy
        try:
            moved = self._manager.transform(copy, self._matrix)
        except Exception as exc:
            raise ReflectionFailed(f"the transform raised: {exc}") from exc
        if not moved:
            raise ReflectionFailed("TemporaryBRepManager.transform refused the reflection")
        check_reflected(bbox(copy), safe(copy, "volume"), self._source)
        return copy


def symmetry_report(arguments):
    """Measure how far the active design is from a true mirror across a plane."""
    try:
        opts = validate_arguments(arguments)
    except ValueError as exc:
        return error_result(f"Error: {exc}")
    try:
        started = time.monotonic()
        design = adsk.fusion.Design.cast(get_app().activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")
        try:
            plane = resolve_plane(opts["plane"], design)
        except ValueError as exc:
            return error_result(f"Error: {exc}")
        manager = adsk.fusion.TemporaryBRepManager.get()
        matrix = reflection_matrix(plane)
        report = run(body_entries(design.rootComponent), plane, opts, Budget(opts["time_budget_s"]), read_measures,
                     lambda pair: MirroredCopies(manager, pair, matrix))
        if report is None:
            return error_result("Error: no solid body to compare; check component_contains and include_hidden")
        log(f"[MCP] symmetry_report: {report['summary']} in {round(time.monotonic() - started, 2)} s")
        return ok_result(report)
    except Exception as exc:
        log(f"[MCP] symmetry_report failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
