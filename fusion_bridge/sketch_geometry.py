"""Fusion-free geometry for the sketch_profile tool.

Everything here works on plain tuples in centimeters (Fusion's internal unit)
so it can be unit tested without Fusion. The tool validates an outline with
these functions before it creates anything in the design.

Outline model: ``points`` are the vertices of a closed loop; segment ``i``
runs from point ``i`` to point ``(i + 1) % n``. An arc replaces segment ``i``
with a three-point arc through an interior point (``mid``).
"""

import math

from .common.args import is_finite_number, is_int
from .common.geometry import CM_PER_UNIT, cross, dot, sub

# Shorter than this is a zero-length segment; closer than this is touching.
LENGTH_TOL_CM = 1e-5
# Model-space points further than this from the sketch plane are refused.
PLANE_TOL_CM = 1e-4
MAX_POINTS = 500
# Arcs are approximated by chords of at most this sweep for intersection tests.
ARC_STEP_DEG = 10.0

# Orthonormal (u, v) frames used ONLY to validate model-space input before a
# sketch exists. The real sketch mapping comes from Sketch.modelToSketchSpace;
# self-intersection is unaffected by which in-plane frame is used.
ROOT_PLANE_FRAMES = {
    "xy": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "xz": ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
    "yz": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
}


class GeometryError(ValueError):
    """Input that must be refused; ``problems`` lists every reason."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# -- Small vector helpers -----------------------------------------------------


def _norm(a):
    return math.sqrt(dot(a, a))


def _cross2(a, b):
    return a[0] * b[1] - a[1] * b[0]


def distance(a, b):
    return _norm(sub(a, b))


# -- Units and argument parsing -----------------------------------------------


def unit_factor(units):
    """Centimeters per one *units*; raises GeometryError for unknown units."""
    if units not in CM_PER_UNIT:
        raise GeometryError([f"'units' must be one of {sorted(CM_PER_UNIT)}, got {units!r}"])
    return CM_PER_UNIT[units]


def parse_point(value, dims, label, factor):
    """Validate one coordinate list of length *dims* and convert it to cm."""
    if not isinstance(value, (list, tuple)) or len(value) != dims:
        raise GeometryError([f"{label} must be a list of {dims} numbers"])
    if not all(is_finite_number(v) for v in value):
        raise GeometryError([f"{label} must contain only finite numbers"])
    return tuple(float(v) * factor for v in value)


def parse_points(points, dims, factor):
    """Convert the outline vertices to cm and drop an explicit closing point.

    Returns (points, warnings). Exact consecutive duplicates are refused later
    by validate_outline; only a last point equal to the first is removed here,
    because the loop is always closed automatically.
    """
    if not isinstance(points, list):
        raise GeometryError(["'points' must be a list of coordinate lists"])
    if len(points) > MAX_POINTS:
        raise GeometryError([f"'points' has {len(points)} entries; the limit is {MAX_POINTS}"])
    parsed = [parse_point(p, dims, f"points[{i}]", factor) for i, p in enumerate(points)]
    warnings = []
    if len(parsed) > 2 and parsed[-1] == parsed[0]:
        parsed.pop()
        warnings.append("The last point repeated the first; it was dropped because the loop closes itself.")
    if len(parsed) < 2:
        raise GeometryError(["'points' needs at least 2 distinct vertices (3 without arcs)"])
    return parsed, warnings


def parse_arcs(arcs, segment_count, dims, factor):
    """Validate the arc list; return {segment index: {"bulge": b} | {"mid": point}}."""
    if arcs is None:
        return {}
    if not isinstance(arcs, list):
        raise GeometryError(["'arcs' must be a list"])
    result, problems = {}, []
    for k, arc in enumerate(arcs):
        label = f"arcs[{k}]"
        if not isinstance(arc, dict):
            problems.append(f"{label} must be an object")
            continue
        unknown = set(arc) - {"index", "bulge", "mid_point"}
        if unknown:
            problems.append(f"{label} has unknown fields {sorted(unknown)}")
        index = arc.get("index")
        if not is_int(index) or not 0 <= index < segment_count:
            problems.append(f"{label}.index must be an integer segment index 0..{segment_count - 1}")
            continue
        if index in result:
            problems.append(f"{label}: segment {index} already has an arc")
            continue
        has_bulge, has_mid = "bulge" in arc, "mid_point" in arc
        if has_bulge == has_mid:
            problems.append(f"{label} needs exactly one of 'bulge' or 'mid_point'")
            continue
        if has_bulge:
            bulge = arc["bulge"]
            if not is_finite_number(bulge) or bulge == 0:
                problems.append(f"{label}.bulge must be a finite non-zero number (0 is a straight line)")
                continue
            result[index] = {"bulge": float(bulge)}
        else:
            try:
                result[index] = {"mid": parse_point(arc["mid_point"], dims, f"{label}.mid_point", factor)}
            except GeometryError as exc:
                problems.extend(exc.problems)
    if problems:
        raise GeometryError(problems)
    return result


# -- Arcs ---------------------------------------------------------------------


def bulge_to_mid(start, end, bulge):
    """Point halfway along an arc given its DXF-style bulge.

    bulge = tan(sweep / 4); positive sweeps counterclockwise from start to end,
    which puts the arc on the right of the start->end direction. |bulge| = 1 is
    a semicircle. The sagitta (chord midpoint to arc midpoint) is bulge * chord / 2.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    chord = math.hypot(dx, dy)
    if chord == 0:
        raise GeometryError(["an arc needs distinct start and end points"])
    sagitta = bulge * chord / 2.0
    right = (dy / chord, -dx / chord)
    mx, my = (start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0
    return (mx + right[0] * sagitta, my + right[1] * sagitta)


def circle_through(a, b, c):
    """Center and radius of the circle through three 2D points, or None if collinear."""
    d = 2.0 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if d == 0:
        return None
    a2, b2, c2 = dot(a, a), dot(b, b), dot(c, c)
    ux = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d
    uy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d
    return (ux, uy), math.hypot(a[0] - ux, a[1] - uy)


def arc_sweep(start, mid, end):
    """Signed sweep (radians, + = counterclockwise) of the arc start->mid->end."""
    circle = circle_through(start, mid, end)
    if circle is None:
        return None
    (cx, cy), _ = circle
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    am = math.atan2(mid[1] - cy, mid[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)
    ccw = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) < ccw:
        return ccw
    return ccw - 2 * math.pi


def tessellate_arc(start, mid, end, step_deg=ARC_STEP_DEG):
    """Points along the arc after *start*, ending exactly at *end*."""
    (cx, cy), radius = circle_through(start, mid, end)
    sweep = arc_sweep(start, mid, end)
    steps = max(2, int(math.ceil(abs(math.degrees(sweep)) / step_deg)))
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    pts = [(cx + radius * math.cos(a0 + sweep * k / steps), cy + radius * math.sin(a0 + sweep * k / steps))
           for k in range(1, steps)]
    pts.append(tuple(end))
    return pts


def arc_mids(points2d, arcs):
    """Resolve each arc to a 2D mid point (bulge -> mid, explicit mid unchanged)."""
    mids = {}
    n = len(points2d)
    for index, spec in arcs.items():
        start, end = points2d[index], points2d[(index + 1) % n]
        mids[index] = bulge_to_mid(start, end, spec["bulge"]) if "bulge" in spec else tuple(spec["mid"][:2])
    return mids


# -- Point / segment predicates -------------------------------------------------


def point_segment_distance(p, a, b):
    ab = sub(b, a)
    length2 = dot(ab, ab)
    if length2 == 0:
        return distance(p, a)
    t = max(0.0, min(1.0, dot(sub(p, a), ab) / length2))
    return distance(p, (a[0] + ab[0] * t, a[1] + ab[1] * t))


def segments_touch(p1, p2, q1, q2, tol=LENGTH_TOL_CM):
    """True if two 2D segments cross, touch or overlap (within *tol*)."""
    d1 = _cross2(sub(p2, p1), sub(q1, p1))
    d2 = _cross2(sub(p2, p1), sub(q2, p1))
    d3 = _cross2(sub(q2, q1), sub(p1, q1))
    d4 = _cross2(sub(q2, q1), sub(p2, q1))
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    return min(point_segment_distance(q1, p1, p2), point_segment_distance(q2, p1, p2),
               point_segment_distance(p1, q1, q2), point_segment_distance(p2, q1, q2)) <= tol


def polygon_area(points2d):
    """Signed shoelace area (+ = counterclockwise)."""
    n = len(points2d)
    return sum(_cross2(points2d[i], points2d[(i + 1) % n]) for i in range(n)) / 2.0


def point_in_loops(point, loops):
    """Even-odd containment of a 2D point in a set of closed polylines.

    Holes come out naturally: a point inside an inner loop crosses two boundaries.
    """
    x, y = point
    inside = False
    for loop in loops:
        n = len(loop)
        for i in range(n):
            (x1, y1), (x2, y2) = loop[i][:2], loop[(i + 1) % n][:2]
            if (y1 > y) != (y2 > y):
                if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
    return inside


# -- Outline validation --------------------------------------------------------


def _segment_label(i, n):
    return f"segment {i} (points {i}->{(i + 1) % n})"


def _outline(points2d, mids):
    """Closed tessellated loop: list of (start, end, original segment index)."""
    n = len(points2d)
    chords = []
    for i in range(n):
        start, end = points2d[i], points2d[(i + 1) % n]
        pts = tessellate_arc(start, mids[i], end) if i in mids else [end]
        prev = start
        for p in pts:
            chords.append((prev, p, i))
            prev = p
    return chords


def validate_outline(points2d, arcs):
    """Refuse a degenerate or self-intersecting closed outline.

    *points2d* are cm vertices in a plane; *arcs* is the parse_arcs mapping.
    Returns (mids, warnings) where mids maps arc segment -> 2D mid point.
    Raises GeometryError listing every problem found.
    """
    n = len(points2d)
    problems, warnings = [], []
    for i in range(n):
        j = (i + 1) % n
        if points2d[i] == points2d[j]:
            problems.append(f"points {i} and {j} are identical (duplicate consecutive point)")
        elif distance(points2d[i], points2d[j]) <= LENGTH_TOL_CM:
            problems.append(f"{_segment_label(i, n)} has zero length")
    if n < 3 and not arcs:
        problems.append("a closed outline of straight lines needs at least 3 points")
    if problems:
        raise GeometryError(problems)

    mids = arc_mids(points2d, arcs)
    for i, mid in mids.items():
        start, end = points2d[i], points2d[(i + 1) % n]
        if point_segment_distance(mid, start, end) <= LENGTH_TOL_CM or circle_through(start, mid, end) is None:
            problems.append(f"arc on {_segment_label(i, n)} is effectively straight (mid point on the chord)")
    if problems:
        raise GeometryError(problems)

    chords = _outline(points2d, mids)
    m = len(chords)
    found = set()

    def report(kind, a, b):
        key = (kind, min(a, b), max(a, b))
        if key not in found:
            found.add(key)
            if kind == "fold":
                problems.append(f"{_segment_label(a, n)} doubles back over {_segment_label(b, n)}")
            else:
                problems.append(f"{_segment_label(a, n)} crosses or touches {_segment_label(b, n)}")

    # Adjacent chords share a vertex; they are only wrong if one folds back over the other.
    for k in range(m):
        p, q, si = chords[k]
        _, r, sj = chords[(k + 1) % m]
        if point_segment_distance(p, q, r) <= LENGTH_TOL_CM or point_segment_distance(r, p, q) <= LENGTH_TOL_CM:
            report("fold", si, sj)
        elif si != sj and si not in mids and sj not in mids:
            # Straight-through vertex: legal, but it adds a corner nobody asked for.
            if point_segment_distance(q, p, r) <= LENGTH_TOL_CM:
                warnings.append(f"point {sj} is collinear with its neighbors; it adds a vertex but no corner")

    # Non-adjacent chords must not meet. Sweep along x to skip far-apart pairs.
    order = sorted(range(m), key=lambda k: min(chords[k][0][0], chords[k][1][0]))
    active = []
    for k in order:
        a, b, si = chords[k]
        lo_x = min(a[0], b[0])
        active = [o for o in active if max(chords[o][0][0], chords[o][1][0]) >= lo_x - LENGTH_TOL_CM]
        lo_y, hi_y = min(a[1], b[1]), max(a[1], b[1])
        for o in active:
            if abs(o - k) in (1, m - 1):
                continue
            c, d, sj = chords[o]
            if min(c[1], d[1]) > hi_y + LENGTH_TOL_CM or max(c[1], d[1]) < lo_y - LENGTH_TOL_CM:
                continue
            if segments_touch(a, b, c, d):
                report("cross", si, sj)
        active.append(k)

    if problems:
        raise GeometryError(problems)
    # Checked last: a bowtie also has zero signed area, but "crosses" is the useful report.
    perimeter = sum(distance(a, b) for a, b, _ in chords)
    if abs(polygon_area([c[0] for c in chords])) <= LENGTH_TOL_CM * perimeter:
        raise GeometryError(["the outline encloses no area (all points are collinear)"])
    return mids, warnings


# -- Model space -----------------------------------------------------------------


def newell_normal(points3d):
    """Unit normal of a (roughly) planar 3D polygon, or None if degenerate."""
    n = len(points3d)
    nx = ny = nz = 0.0
    for i in range(n):
        (x1, y1, z1), (x2, y2, z2) = points3d[i], points3d[(i + 1) % n]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length == 0:
        return None
    return (nx / length, ny / length, nz / length)


def frame_from_normal(normal):
    """Any right-handed orthonormal (u, v) with u x v = normal."""
    helper = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = cross(helper, normal)
    length = _norm(u)
    u = tuple(c / length for c in u)
    v = cross(normal, u)
    return u, v


def project_to_frame(points3d, origin, u, v):
    """2D coordinates of 3D points in the (origin, u, v) frame plus off-plane distances."""
    normal = cross(u, v)
    flat, off = [], []
    for p in points3d:
        rel = sub(p, origin)
        flat.append((dot(rel, u), dot(rel, v)))
        off.append(abs(dot(rel, normal)))
    return flat, off


def check_on_plane(off, labels, tol=PLANE_TOL_CM, factor=1.0, units="cm"):
    """Refuse points further than *tol* (cm) from the plane; report in user units."""
    problems = [f"{label} is {d / factor:.6g} {units} off the sketch plane"
                for label, d in zip(labels, off) if d > tol]
    if problems:
        raise GeometryError(problems)


def flatten_model_input(points3d, arcs, plane, factor=1.0, units="cm"):
    """Project model-space input into a 2D validation frame.

    *plane* is a root plane key, or None for an arbitrary plane (normal from
    the points themselves). Returns (points2d, arcs2d) ready for
    validate_outline. Raises GeometryError for off-plane input.
    """
    mid_items = [(i, spec["mid"]) for i, spec in sorted(arcs.items()) if "mid" in spec]
    everything = list(points3d) + [m for _, m in mid_items]
    labels = [f"points[{i}]" for i in range(len(points3d))] + [f"arc on segment {i} mid_point" for i, _ in mid_items]
    if plane in ROOT_PLANE_FRAMES:
        origin, (u, v) = (0.0, 0.0, 0.0), ROOT_PLANE_FRAMES[plane]
    else:
        normal = newell_normal(points3d)
        if normal is None:
            raise GeometryError(["the outline encloses no area (all points are collinear)"])
        origin = tuple(sum(c) / len(points3d) for c in zip(*points3d))
        u, v = frame_from_normal(normal)
    flat, off = project_to_frame(everything, origin, u, v)
    check_on_plane(off, labels, factor=factor, units=units)
    points2d = flat[:len(points3d)]
    arcs2d = {i: dict(spec) for i, spec in arcs.items()}
    for (i, _), mid in zip(mid_items, flat[len(points3d):]):
        arcs2d[i] = {"mid": mid}
    return points2d, arcs2d
