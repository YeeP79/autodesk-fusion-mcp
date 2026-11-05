"""Chamfer or fillet named edges, retry the way Fusion suggests, measure: edge_treatment.

Every edge gets an anchor: its ``pointOnEdge``, read before anything is built.
A treated edge trims its neighbors (RC-4), and a trimmed edge still contains a
point on it where its midpoint has moved, so each attempt finds its edges again
by anchor, as the distance to the curve. An anchor that finds nothing is ``lost``.

Fusion's refusal asks for separate operations, so a refused feature is split and
tried again, one rung at a time: everything on a body, the sets on one plane,
each set, each chain, each edge. Sets on one plane stay together because built
apart the first trims the corners the second names. A chain is tried whole
before its edges, closed ones first, because an arc alone has two ends Fusion
cannot cap where it runs tangentially into a line it does not take. A rung that
would repeat the question just refused is skipped. Edge breaks are not a solved
problem: whatever no rung could build stays in ``refused`` with Fusion's words,
nothing substitutes another shape, and the status is ``partial``, because a
half-chamfered part is a missing feature.

A two-distance chamfer laid the wrong way builds without complaint, so it is
measured: the anchor's distance to the nearest edge of ``size_on_face`` is the
inset on that face. Swapped is deleted and rebuilt flipped; swapped again is
deleted and counts as a refusal. Nothing predicts the direction, because the
rule Build derived from block end caps does not hold for every shape.

``ChamferFeatures.createInput`` is gone from the current API, so every chamfer
goes through ``createInput2``, and an angle is the caller's to turn into two
distances. ``Job`` takes duck-typed collaborators, so fakes can drive it.
Internal lengths are cm.
"""

import math
import traceback

import adsk.core
import adsk.fusion

from .common import feature_ops as fo
from .common.api import attempt, items, safe
from .common.args import (
    bool_arg,
    error_pair,
    is_finite_number,
    reject_unknown,
    require_object,
    text_arg,
)
from .common.bodies import ROOT_PATH, SURFACE_TYPES, bbox, label, native, parse_body_ref, resolve_targets
from .common.entity_refs import (
    CURVE_TYPES,
    REF_TOLERANCE_CM,
    FusionScene,
    bbox_distance,
    body_ref,
    describe_edge,
    edge_samples,
    parse_ref,
    public,
    resolve,
    resolved_row,
    same_body,
    same_entity,
    unique_entities,
)
from .common.geometry import (
    CM_PER_UNIT,
    UNIT_FACTORS,
    cross,
    display_point,
    dot,
    scaled,
    sub,
    to_units,
    unit,
    xyz,
)
from .common.results import error_result
from .dispatch import get_app, log
from .solid_check import face_width

_KNOWN = {"kind", "strategy", "tolerance", "feature_name", "units", "dry_run", "sets"}
_SET_KNOWN = {"edges", "points", "body", "size", "size_expression", "size_two", "size_two_expression",
              "size_on_face", "tangent_chain"}

KINDS = ("chamfer", "fillet")
STRATEGIES = ("split_on_refusal", "together_only")

# Two distances closer than this share of the larger cannot be told apart on a face.
DECISIVE_FRACTION = 0.02
# More faces than this on a body usually means layered unions, where fillets fail.
HIGH_FACE_COUNT = 200
# How far apart two points must be to fix a direction, and two directions to fix a plane.
_DISTINCT_CM = 1e-6

SWAPPED = "swapped"  # a working verdict: it never reaches a result
STALE_EDGE_REFS = "EntityRefs on these edges and their neighbors may be stale; find them again"


# -- Pure geometry --------------------------------------------------------------


def point_to_segment(point, start, end):
    """Distance from *point* to the segment, measured from an end when it projects past one."""
    span = sub(end, start)
    length = dot(span, span)
    at = 0.0 if length <= 0.0 else min(1.0, max(0.0, dot(sub(point, start), span) / length))
    return math.dist(point, tuple(s + at * d for s, d in zip(start, span)))


def off_circle(point, center, normal, radius):
    """Distance from *point* to a whole circle: out of its plane and off its rim, which cannot cancel."""
    offset = sub(point, center)
    out = dot(offset, normal)
    flat = sub(offset, scaled(normal, out))
    return math.hypot(out, math.sqrt(dot(flat, flat)) - radius)


def coplanar(points, tolerance):
    """Do *points* lie on one plane? Collinear points lie on many, so they do."""
    if len(points) < 3:
        return True
    base = points[0]
    far = max(points, key=lambda p: math.dist(p, base))
    span = sub(far, base)
    if math.sqrt(dot(span, span)) <= _DISTINCT_CM:
        return True
    crossed = [cross(span, sub(p, base)) for p in points]
    widest = max(crossed, key=lambda c: dot(c, c))
    normal = unit(widest)
    if normal is None:
        return True
    return all(abs(dot(sub(p, base), normal)) <= tolerance for p in points)


def plane_groups(point_lists, tolerance):
    """Indices of *point_lists* grouped by shared plane, in order, each joining the first it fits."""
    groups = []
    for index, points in enumerate(point_lists):
        for group in groups:
            if coplanar(group["points"] + points, tolerance):
                group["members"].append(index)
                group["points"] = group["points"] + points
                break
        else:
            groups.append({"members": [index], "points": list(points)})
    return [group["members"] for group in groups]


# -- Chains ---------------------------------------------------------------------


def _ends(edge):
    return attempt(lambda: (edge.startVertex, edge.endVertex), ())


def chains(edges):
    """These edges as the chains they form: lists of indices, each ordered, by first edge.

    Two edges chain when they share a vertex, compared with ``==`` because
    Fusion hands back a new wrapper on every read. An edge whose vertices will
    not answer stands alone.
    """
    ends = [_ends(edge) for edge in edges]
    owner = list(range(len(edges)))

    def root(index):
        while owner[index] != index:
            owner[index] = owner[owner[index]]
            index = owner[index]
        return index

    for index, mine in enumerate(ends):
        for other in range(index):
            if any(same_entity(vertex, theirs) for vertex in mine for theirs in ends[other]):
                owner[root(index)] = root(other)
    found = {}
    for index in range(len(edges)):
        found.setdefault(root(index), []).append(index)
    return sorted(found.values(), key=lambda chain: chain[0])


def chain_closed(edges, chain):
    """Does the chain close on itself? Every vertex must be met exactly twice."""
    seen = []
    for index in chain:
        ends = _ends(edges[index])
        if not ends:
            return False
        for vertex in ends:
            for row in seen:
                if same_entity(row[0], vertex):
                    row[1] += 1
                    break
            else:
                seen.append([vertex, 1])
    return all(count == 2 for _, count in seen)


# -- Verdicts -------------------------------------------------------------------


def decisive(size, size_two):
    """Can a face tell these two distances apart?"""
    return abs(size - size_two) >= DECISIVE_FRACTION * max(size, size_two)


def classify_inset(inset, size, size_two):
    """``size``, ``size_two`` or None: which distance *inset* is, within a quarter of their gap."""
    quarter = abs(size - size_two) / 4
    if abs(inset - size) <= quarter:
        return "size"
    if abs(inset - size_two) <= quarter:
        return "size_two"
    return None


def inset_verdict(labels, beside_swap=False):
    """One verdict from every anchor's label. Any anchor the wrong way fails the set.

    *beside_swap*: a set in the same feature laid this set's ``size_two`` on the
    face, so its boundary reads exactly as a swap would and proves nothing.
    """
    if not labels:
        return "not_measured"
    if "size_two" in labels:
        return "not_measured" if beside_swap else SWAPPED
    return "ok" if all(one == "size" for one in labels) else "mismatch"


def equal_verdict(insets, size, tolerance, beside=()):
    """Verdict of an equal chamfer from its insets on both faces.

    An inset at another set's size in the same feature may be that set's
    boundary, so it is not judged.
    """
    close = max(tolerance, DECISIVE_FRACTION * size)
    judged = [inset for inset in insets if abs(inset - size) <= close
              or not any(abs(inset - other) <= close for other in beside)]
    if not judged:
        return "not_measured"
    return "ok" if all(abs(inset - size) <= close for inset in judged) else "mismatch"


def radius_verdict(radii, size, tolerance):
    """(verdict, nearest radius) of a fillet from its cylinder and torus radii.

    A fillet along a spline is a NURBS face with no radius to read: not measured.
    """
    if not radii:
        return "not_measured", None
    nearest = min(radii, key=lambda radius: abs(radius - size))
    return ("ok" if abs(nearest - size) <= tolerance else "mismatch"), nearest


def edge_suspects(length, neighbors, widths, largest, show):
    """Why an edge may refuse, as text; *show* turns cm into a displayed number.

    A guide for reading a refusal, not a prediction: Build's probe built several
    of these cases and refused others, for causes still unknown.
    """
    why = []
    if length is not None and length < largest:
        why.append(f"edge {show(length)} long is shorter than size {show(largest)}")
    short = [other for other in neighbors if other is not None and other < largest]
    if short:
        why.append(f"neighbor edge {show(min(short))} long is shorter than size {show(largest)}")
    thin = [width for width in widths if width is not None and width < largest]
    if thin:
        why.append(f"sliver face {show(min(thin))} wide beside the edge is narrower than size {show(largest)}")
    return why


# -- The ladder -----------------------------------------------------------------


def pending(part):
    return [anchor for anchor in part["anchors"] if anchor["state"] == "pending"]


def shown_scope(scope, parts):
    """A step holding one edge is an edge attempt, whatever rung it is on."""
    return "edge" if sum(len(pending(part)) for part in parts) == 1 else scope


def next_rung(parts, scope, by_plane, chains_of):
    """The next narrower ``(scope, groups)``, or ``(None, [])`` at the bottom.

    A rung that would only repeat what was just refused is skipped. *by_plane*
    takes parts and returns lists of parts; *chains_of* takes a part and returns
    lists of its anchors, closed chains first.
    """
    if scope == "together":
        groups = by_plane(parts)
        if 1 < len(groups) < len(parts):
            return "plane", groups
        scope = "plane"
    if scope == "plane":
        if len(parts) > 1:
            return "set", [[part] for part in parts]
        scope = "set"
    part = parts[0]
    if scope == "set":
        found = chains_of(part)
        if len(found) > 1:
            return "loop", [[{**part, "anchors": chain}] for chain in found]
        scope = "loop"
    # A tangent-chain set reaches its whole chain from any one edge, so an edge
    # alone would only ask for the chain that was just refused.
    if scope == "loop" and len(pending(part)) > 1 and not part["set"]["tangent"]:
        return "edge", [[{**part, "anchors": [anchor]}] for anchor in pending(part)]
    return None, []


def ladder(parts, scope, try_step, split, give_up):
    """Try *parts* as one step; on refusal go down a rung, or give up at the bottom.

    ``try_step(scope, parts)`` returns the refusal text, or None when no lower
    rung is needed. ``split(parts, scope)`` is ``next_rung``. A TypeError is our
    call being wrong, so it stops everything.
    """
    refusal = try_step(scope, parts)
    if refusal is None:
        return
    lower, groups = split(parts, scope)
    if lower is None:
        give_up(scope, parts, refusal)
        return
    for group in groups:
        if any(pending(part) for part in group):
            ladder(group, lower, try_step, split, give_up)


# -- Finding edges --------------------------------------------------------------


def off_edge(edge, point):
    """Distance from *point* to an edge when Fusion cannot measure it, or None.

    A circle or arc is measured against its whole circle; anything else against
    its chord, which is wrong for a spline and only better than nothing.
    """
    geometry = safe(edge, "geometry")
    if CURVE_TYPES.get(safe(geometry, "curveType")) in ("arc", "circle"):
        found = attempt(lambda: off_circle(point, xyz(geometry.center), xyz(geometry.normal), float(geometry.radius)))
        if found is not None:
            return found
    return attempt(lambda: point_to_segment(point, xyz(edge.startVertex.geometry), xyz(edge.endVertex.geometry)))


def edge_distance(scene, edge, point, limit=None):
    """Distance in cm from *point* to the edge's curve, or None when nothing can say.

    A box already further than *limit* settles it without asking Fusion: its
    distance is a lower bound, returned as it is.
    """
    box = bbox(edge)
    if limit is not None and box is not None:
        floor = bbox_distance(box, point)
        if floor > limit:
            return floor
    measured = scene.distance(edge, point)
    return measured if measured is not None else off_edge(edge, point)


def near_edges(scene, edges, point, tolerance):
    """The distinct edges within *tolerance* of *point*, nearest first."""
    rows = []
    for edge in edges:
        away = edge_distance(scene, edge, point, tolerance)
        if away is not None and away <= tolerance:
            rows.append((away, edge))
    rows.sort(key=lambda row: row[0])
    return unique_entities(edge for _, edge in rows)


def nearest_distance(scene, edges, point):
    """Distance in cm to the nearest of *edges*, or None. Boxes rule out the far ones unmeasured."""
    boxed = sorted(((bbox_distance(box, point) if (box := bbox(edge)) else 0.0, i) for i, edge in enumerate(edges)))
    best = None
    for floor, index in boxed:
        if best is not None and floor >= best:
            break
        away = edge_distance(scene, edges[index], point)
        if away is not None and (best is None or away < best):
            best = away
    return best


def fillet_radii(feature):
    """The radii of a fillet's cylinder and torus faces, in cm."""
    radii = []
    for face in items(safe(feature, "faces")):
        geometry = safe(face, "geometry")
        kind = SURFACE_TYPES.get(safe(geometry, "surfaceType"))
        radius = safe(geometry, "radius") if kind == "cylinder" else (
            safe(geometry, "minorRadius") if kind == "torus" else None)
        if radius is not None:
            radii.append(radius)
    return radii


def _width(face):
    """A face's mean width, or None when its area cannot be read."""
    area = safe(face, "area")
    if area is None:
        return None
    return face_width(area, sum(safe(edge, "length") or 0.0 for edge in items(safe(face, "edges"))))


# -- Argument validation --------------------------------------------------------


def _parse_point(value, units, where):
    if not isinstance(value, list) or len(value) != 3 or not all(is_finite_number(c) for c in value):
        raise ValueError(f"{where} must be an array of three finite numbers [x, y, z]")
    return tuple(float(c) * CM_PER_UNIT[units] for c in value)


def _parse_set(value, index, kind, units):
    where = f"sets[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    if "kind" in value:
        raise ValueError(f"{where}: 'kind' is one per call, because a chamfer and a fillet cannot share a "
                         "feature; make two calls, fillets first")
    reject_unknown(value, _SET_KNOWN)
    size = fo.read_value(value, "size", units, minimum=0)
    size_two = fo.read_value(value, "size_two", units, required=False, minimum=0)
    if kind == "fillet" and size_two is not None:
        raise ValueError(f"{where}: a fillet has one radius, so it takes no 'size_two'")
    face = value.get("size_on_face")
    if size_two is not None and face is None:
        raise ValueError(f"{where}: 'size_two' needs 'size_on_face', the face 'size' is measured on")
    if size_two is None and face is not None:
        raise ValueError(f"{where}: 'size_on_face' only means something with 'size_two'")

    body = value.get("body")
    body = None if body is None else parse_body_ref(body, f"'{where}.body'")
    edges, points = value.get("edges", []), value.get("points", [])
    if not isinstance(edges, list) or not isinstance(points, list):
        raise ValueError(f"{where}: 'edges' and 'points' must be arrays")
    if not edges and not points:
        raise ValueError(f"{where} names no edges: give 'edges' or 'points'")
    if points and body is None:
        raise ValueError(f"{where}: 'points' need 'body'")

    refs = []
    for i, ref in enumerate(edges):
        parsed = parse_ref(ref, units, f"{where}.edges[{i}]")
        if parsed["kind"] not in (None, "edge"):
            raise ValueError(f"{where}.edges[{i}] is a {parsed['kind']}, not an edge")
        refs.append({**parsed, "kind": "edge", "body": parsed["body"] or body})
    on_face = None
    if face is not None:
        on_face = parse_ref(face, units, f"{where}.size_on_face")
        if on_face["kind"] not in (None, "face"):
            raise ValueError(f"{where}.size_on_face is a {on_face['kind']}, not a face")
        on_face = {**on_face, "kind": "face", "body": on_face["body"] or body}
    return {"index": index, "size": size, "size_two": size_two, "size_on_face": on_face, "refs": refs,
            "points": [_parse_point(p, units, f"{where}.points[{i}]") for i, p in enumerate(points)],
            "body": body, "tangent": bool_arg(value, "tangent_chain", False)}


@error_pair
def validate_args(arguments):
    """Return (options, error_text)."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN)
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    kind = arguments.get("kind")
    if kind not in KINDS:
        raise ValueError(f"'kind' must be one of {list(KINDS)}, got {kind!r}")
    strategy = arguments.get("strategy", "split_on_refusal")
    if strategy not in STRATEGIES:
        raise ValueError(f"'strategy' must be one of {list(STRATEGIES)}, got {strategy!r}")
    tolerance = arguments.get("tolerance")
    if tolerance is not None and (not is_finite_number(tolerance) or tolerance <= 0):
        raise ValueError("'tolerance' must be a number greater than 0")
    sets = arguments.get("sets")
    if not isinstance(sets, list) or not sets:
        raise ValueError("'sets' must be a non-empty array")
    return {"kind": kind, "strategy": strategy, "units": units,
            "tolerance": REF_TOLERANCE_CM if tolerance is None else float(tolerance) * CM_PER_UNIT[units],
            "feature_name": text_arg(arguments, "feature_name", blank_ok=True) or None,
            "dry_run": bool_arg(arguments, "dry_run", False),
            "sets": [_parse_set(value, i, kind, units) for i, value in enumerate(sets)]}


# -- One call -------------------------------------------------------------------


class Job:
    """One call: choose the edges, refuse or plan, climb the ladder, report."""

    def __init__(self, design, document, session, opts):
        self.design, self.document, self.session, self.opts = design, document, session, opts
        self.units, self.tolerance = opts["units"], opts["tolerance"]
        self.anchors, self.parts = [], []
        self.refusals, self.warnings, self.resolved = [], [], []
        self.lost, self.refused_rows, self.attempts, self.measured, self.features = [], [], [], [], []
        self.points_named = self.points_matched = 0
        self.searched = {}
        self.stopped = None

    # -- Small reads

    def _show(self, value_cm):
        return to_units(value_cm, self.units)

    def _at(self, point):
        return None if point is None else display_point(point, self.units)

    def _entry(self, key):
        return next((e for e in self.session.entries() if e["key"] == key), None)

    def _owner(self, edge):
        return next((e for e in self.session.entries() if same_body(safe(edge, "body"), e)), None)

    def _lose(self, anchor, why):
        if anchor["state"] == "pending":
            anchor["state"] = "lost"
            self.lost.append({"point": self._at(anchor["point"]), "why": why})

    # -- Choosing the edges

    def _read_sizes(self):
        for job in self.opts["sets"]:
            for key in ("size", "size_two"):
                job[f"{key}_cm"] = None
                if job[key] is None:
                    continue
                try:
                    length = self.session.length_cm(job[key], self.units)
                except (ValueError, RuntimeError) as exc:
                    self.refusals.append(f"sets[{job['index']}].{key}: {exc}")
                    continue
                if length <= 0:
                    self.refusals.append(f"sets[{job['index']}].{key}: {job[key]['text']!r} is not above zero")
                job[f"{key}_cm"] = length

    def _handle(self, face, entry):
        """What finds a face again after the edit; a wrapper would not outlive it."""
        point = safe(face, "pointOnFace")
        return {"token": safe(face, "entityToken"), "kind": "face", "point": None if point is None else xyz(point),
                "body": parse_body_ref(body_ref(entry), "body")}

    def _face_now(self, handle):
        """The face now. By token first: its point may have been chamfered away, and the search
        at a point cut away could land on the new face."""
        if handle["token"]:
            found = resolve(self.session, {**handle, "point": None}, self.tolerance)
            if found.entity is not None:
                return found.entity
        if handle["point"] is None:
            return None
        return resolve(self.session, handle, self.tolerance).entity

    def _take(self, job, edge, entry, fallback, taken):
        """One chosen edge becomes one anchor, unless its set already holds it."""
        if any(same_entity(edge, other["_edge"]) for other in taken):
            return
        if any(same_entity(edge, other["_edge"]) for other in self.anchors):
            self.refusals.append(f"sets[{job['index']}] names an edge another set already names, near "
                                 f"{self._at(fallback)}: one edge takes one treatment")
            return
        point = safe(edge, "pointOnEdge")
        point = xyz(point) if point is not None else fallback
        if point is None:
            self.refusals.append(f"sets[{job['index']}]: an edge has no readable point to find it by")
            return
        anchor = {"point": point, "set": job, "key": entry["key"], "state": "pending", "_edge": edge,
                  "faces": [self._handle(face, entry) for face in unique_entities(items(safe(edge, "faces")))],
                  "samples": edge_samples(edge) or [point]}
        taken.append(anchor)

    def _take_refs(self, job, taken):
        for ref in job["refs"]:
            found = resolve(self.session, ref, self.tolerance)
            if found.refusal:
                if found.refusal["code"] == "not_found":
                    self.lost.append({"point": self._at(ref["point"]), "why": found.refusal["error"]})
                else:
                    self.refusals.append(f"sets[{job['index']}]: {found.refusal['error']}")
                continue
            self.resolved.append(resolved_row(found, "edge"))
            self.warnings += found.warnings
            entry = self._owner(found.entity)
            if entry is None:
                self.refusals.append(f"sets[{job['index']}]: an edge's body is not in the design")
                continue
            self._take(job, found.entity, entry, ref["point"], taken)

    def _take_points(self, job, taken):
        if not job["points"]:
            return
        targets, _, problem = resolve_targets(self.session.entries(), [job["body"]])
        if problem:
            self.refusals.append(f"sets[{job['index']}]: {problem['error']}")
            return
        entry = targets[0]
        edges = items(safe(entry["_body"], "edges"))
        self.searched[entry["key"]] = len(edges)
        self.points_named += len(job["points"])
        for point in job["points"]:
            hits = near_edges(self.session, edges, point, self.tolerance)
            if not hits:
                self.lost.append({"point": self._at(point), "why": "no edge within tolerance of this point"})
            elif len(hits) > 1:
                self.refusals.append(f"sets[{job['index']}]: the point {self._at(point)} is within tolerance of "
                                     f"{len(hits)} edges; name a point away from where they meet")
            else:
                self.points_matched += 1
                self._take(job, hits[0], entry, point, taken)

    def _check_face(self, job, taken):
        """``size_on_face`` must be a face of every edge of its set."""
        if job["size_on_face"] is None:
            return
        found = resolve(self.session, job["size_on_face"], self.tolerance)
        if found.refusal:
            self.refusals.append(f"sets[{job['index']}].size_on_face: {found.refusal['error']}")
            return
        self.resolved.append(resolved_row(found, "size_on_face"))
        self.warnings += found.warnings
        apart = [anchor for anchor in taken
                 if not any(same_entity(found.entity, face) for face in items(safe(anchor["_edge"], "faces")))]
        if apart:
            self.refusals.append(f"sets[{job['index']}]: size_on_face is not adjacent to the edge at "
                                 f"{self._at(apart[0]['point'])}")
            return
        if not taken:
            return
        entry = self._entry(taken[0]["key"])
        job["face"] = self._handle(found.entity, entry)
        for anchor in taken:
            others = [face for face in unique_entities(items(safe(anchor["_edge"], "faces")))
                      if not same_entity(face, found.entity)]
            anchor["other_face"] = self._handle(others[0], entry) if others else None

    def _select(self):
        for job in self.opts["sets"]:
            taken = []
            self._take_refs(job, taken)
            self._take_points(job, taken)
            self._check_face(job, taken)
            self.anchors += taken
            for key in dict.fromkeys(anchor["key"] for anchor in taken):
                self.parts.append({"set": job, "key": key, "anchors": [a for a in taken if a["key"] == key]})
        if not self.anchors and not self.refusals:
            self.refusals.append("no edges resolved")

    # -- Suspects and the plan

    def _anchor_suspects(self, anchor):
        edge, job = anchor["_edge"], anchor["set"]
        largest = max(job["size_cm"], job["size_two_cm"] or 0.0)
        ends = _ends(edge)
        neighbors = unique_entities(other for vertex in ends for other in items(safe(vertex, "edges"))
                                    if not same_entity(other, edge))
        widths = [_width(face) for face in unique_entities(items(safe(edge, "faces")))]
        why = edge_suspects(safe(edge, "length"), [safe(other, "length") for other in neighbors], widths, largest,
                            self._show)
        return [{"point": self._at(anchor["point"]), "why": text} for text in why]

    def _suspects(self):
        rows = [row for anchor in self.anchors for row in self._anchor_suspects(anchor)]
        for part in self.parts:
            entry = self._entry(part["key"])
            count = safe(safe(entry["_body"], "faces"), "count")
            if isinstance(count, int) and count > HIGH_FACE_COUNT:
                row = {"point": self._at(part["anchors"][0]["point"]),
                       "why": f"{label(entry)} has {count} faces, which usually means layered unions"}
                if row not in rows:
                    rows.append(row)
        return rows

    def _plan_part(self, part):
        job, entry = part["set"], self._entry(part["key"])
        edges = [anchor["_edge"] for anchor in part["anchors"]]
        return {"set": job["index"], "body": body_ref(entry), "component": entry["component"],
                "size": self._show(job["size_cm"]),
                "size_two": None if job["size_two_cm"] is None else self._show(job["size_two_cm"]),
                "edges": [public(describe_edge(edge, self.units, entry, samples=False)) for edge in edges],
                "chains": [{"edges": chain, "closed": chain_closed(edges, chain)} for chain in chains(edges)]}

    def _plan(self):
        return {"kind": self.opts["kind"], "strategy": self.opts["strategy"],
                "features": len({part["key"] for part in self.parts}),
                "sets": [self._plan_part(part) for part in self.parts]}

    # -- Finding edges again

    def _find(self, part):
        """Each pending anchor's edge now, as ``(anchor, edge)`` rows; what finds nothing is lost."""
        entry = self._entry(part["key"])
        edges = [] if entry is None else items(safe(entry["_body"], "edges"))
        rows = []
        for anchor in pending(part):
            hits = near_edges(self.session, edges, anchor["point"], self.tolerance)
            if hits:
                rows.append((anchor, hits[0]))
            else:
                self._lose(anchor, "gone once an earlier step built")
        return rows

    def _locate(self, parts):
        located = []
        for part in parts:
            rows = self._find(part)
            if rows:
                located.append({"set": part["set"], "anchors": [a for a, _ in rows], "edges": [e for _, e in rows]})
        return located

    def _by_plane(self, parts):
        lists = [[p for anchor in pending(part) for p in anchor["samples"]] for part in parts]
        return [[parts[i] for i in group] for group in plane_groups(lists, self.tolerance)]

    def _chains_of(self, part):
        rows = self._find(part)
        edges = [edge for _, edge in rows]
        found = sorted(chains(edges), key=lambda chain: not chain_closed(edges, chain))
        return [[rows[i][0] for i in chain] for chain in found]

    # -- Measuring

    def _insets(self, handle, points):
        """Distance from each point to the nearest edge of a face, leaving out what cannot be read."""
        face = self._face_now(handle)
        edges = [] if face is None else items(safe(face, "edges"))
        if not edges:
            return []
        found = (nearest_distance(self.session, edges, point) for point in points)
        return [inset for inset in found if inset is not None]

    def _row(self, job, method, expected, found, verdict, flipped=False):
        return {"set": job["index"], "method": method, "expected": [self._show(v) for v in expected],
                "found": [None if v is None else self._show(v) for v in found], "flipped": flipped,
                "verdict": verdict}

    def _measure_two(self, item, beside, flipped):
        job = item["set"]
        size, two = job["size_cm"], job["size_two_cm"]
        points = [anchor["point"] for anchor in item["anchors"]]
        insets = self._insets(job["face"], points)
        first = item["anchors"][0]["other_face"]
        other = self._insets(first, points[:1]) if first else []
        middle = sorted(insets)[len(insets) // 2] if insets else None
        found = [middle, other[0] if other else None]
        was_flipped = job["index"] in flipped
        if not decisive(size, two):
            return self._row(job, "inset", [size, two], found, "not_measured", was_flipped)
        gap = abs(size - two)
        shared = any(abs(size_of - two) <= gap / 4 for size_of in beside)
        verdict = inset_verdict([classify_inset(inset, size, two) for inset in insets], shared)
        if verdict == "ok" and was_flipped:
            verdict = "flipped_fixed"
        return self._row(job, "inset", [size, two], found, verdict, was_flipped)

    def _measure_equal(self, item, beside):
        job = item["set"]
        size = job["size_cm"]
        insets = [inset for anchor in item["anchors"] for handle in anchor["faces"]
                  for inset in self._insets(handle, [anchor["point"]])]
        found = [min(insets), max(insets)] if insets else [None, None]
        return self._row(job, "inset", [size, size], found, equal_verdict(insets, size, self.tolerance, beside))

    def _measure(self, feature, located, flipped):
        rows = []
        for item in located:
            job = item["set"]
            beside = [size for other in located if other["set"] is not job
                      for size in (other["set"]["size_cm"], other["set"]["size_two_cm"]) if size is not None]
            if self.opts["kind"] == "fillet":
                verdict, nearest = radius_verdict(fillet_radii(feature), job["size_cm"], self.tolerance)
                rows.append(self._row(job, "radius", [job["size_cm"]], [nearest], verdict))
            elif job["size_two_cm"] is not None:
                rows.append(self._measure_two(item, beside, flipped))
            else:
                rows.append(self._measure_equal(item, beside))
        return rows

    # -- Building

    def _build(self, parts):
        """One feature over *parts*, flipped once if measured swapped.

        Returns ``{"outcome": built|refused|lost, ...}``. A refusal here is a
        refusal to the ladder, swapped-both-ways included: on a loop whose edges
        do not all run the same way, a lower rung can still get them right.
        """
        key = parts[0]["key"]
        flipped = set()
        before = fo.snapshot(self.design) if any(p["set"]["size_two"] for p in parts) else None
        while True:
            located = self._locate(parts)
            if not located:
                return {"outcome": "lost"}
            entry = self._entry(key)
            made = fo.add_feature(lambda: self.session.add(entry, located, self.opts["kind"], flipped))
            self.warnings += made["warnings"]
            self.session.refresh()
            if made["status"] == "refused":
                return {"outcome": "refused", "refusal": made["refusal"], "located": located}
            rows = self._measure(made["feature"], located, flipped)
            swapped = {row["set"] for row in rows if row["verdict"] == SWAPPED}
            if not swapped:
                return {"outcome": "built", "made": made, "located": located, "rows": rows}
            taken = fo.undo(self.design, made["feature"], before)
            self.warnings += taken["warnings"]
            self.session.refresh()
            if flipped or not taken["undone"]:
                return {"outcome": "refused", "located": located,
                        "refusal": f"sets {sorted(swapped)} measured swapped both ways, so the feature was deleted"}
            flipped |= swapped

    def _name(self, feature):
        name = self.opts["feature_name"]
        if not name:
            return
        wanted = name if not self.features else f"{name} {len(self.features) + 1}"
        try:
            feature.name = wanted
        except RuntimeError as exc:
            self.warnings.append(f"could not name a feature {wanted!r}: {exc}")

    def _step(self, scope, parts):
        """One rung's attempt; returns the refusal, or None when no lower rung is needed."""
        shown = shown_scope(scope, parts)
        count = sum(len(pending(part)) for part in parts)
        result = self._build(parts)
        row = {"scope": shown, "edges": count, "outcome": result["outcome"]}
        self.attempts.append(row)
        if result["outcome"] == "refused":
            row["refusal"] = result["refusal"]
            return result["refusal"]
        if result["outcome"] == "built":
            made = result["made"]
            self._name(made["feature"])
            self.features.append(fo.feature_row(made["feature"], made["health"]))
            row["feature"] = self.features[-1]["name"]
            self.measured += result["rows"]
            for item in result["located"]:
                for anchor in item["anchors"]:
                    anchor["state"] = "built"
        return None

    def _give_up(self, scope, parts, refusal):
        shown = shown_scope(scope, parts)
        for part in parts:
            for anchor in pending(part):
                anchor["state"] = "refused"
                self.refused_rows.append({"point": self._at(anchor["point"]), "attempt": shown, "refusal": refusal})

    def _split(self, parts, scope):
        if self.opts["strategy"] == "together_only":
            return None, []
        return next_rung(parts, scope, self._by_plane, self._chains_of)

    def _climb(self):
        bodies = {}
        for part in self.parts:
            bodies.setdefault(part["key"], []).append(part)
        try:
            for parts in bodies.values():
                ladder(parts, "together", self._step, self._split, self._give_up)
        except TypeError as exc:
            self.stopped = f"our call to Fusion is wrong, so nothing more was tried: {exc}"
            log(f"[MCP] edge_treatment stopped: {traceback.format_exc()}", adsk.core.LogLevels.ErrorLogLevel)
            self._give_up("stopped", self.parts, self.stopped)

    # -- The result

    def _counts(self):
        return {"requested": len(self.anchors), "built": sum(a["state"] == "built" for a in self.anchors),
                "points_named": self.points_named, "points_matched": self.points_matched,
                "searched": sum(self.searched.values()), "lost": self.lost, "refused": self.refused_rows}

    def _payload(self, status, **extra):
        payload = fo.envelope(status, self.units, dry_run=self.opts["dry_run"], resolved=self.resolved,
                              warnings=self.warnings, edges=self._counts(), **extra)
        del payload["feature"]
        payload["features"] = self.features
        return payload

    def _problems(self, before, after):
        keys = list(dict.fromkeys(part["key"] for part in self.parts))
        built = list(dict.fromkeys(a["key"] for a in self.anchors if a["state"] == "built"))
        problems = [f"set {row['set']}: expected {row['expected']}, found {row['found']}"
                    for row in self.measured if row["verdict"] == "mismatch"]
        same = fo.unchanged(before, after, built)
        if same:
            problems.append(f"built, yet these bodies did not change: {same}")
        outside = fo.lost_outside(before, after, keys)
        if outside:
            problems.append(f"bodies nobody named lost volume or went: {outside}")
        return problems

    def _finish(self, before, token, suspects):
        after = fo.snapshot(self.design)
        keys = list(dict.fromkeys(part["key"] for part in self.parts))
        changes = fo.body_changes(before, after, keys, self.units)
        others = [row["ref"] for row in changes["touched"] if row["role"] == "other_instance"]
        if others:
            self.warnings.append(f"other instances of this component changed with it: {others}")
        extra = {"bodies": changes, "baseline_token": token, "attempts": self.attempts, "measured": self.measured,
                 "suspects": suspects}
        counts = self._counts()
        if not counts["built"]:
            refusals = list(dict.fromkeys(row["refusal"] for row in self.refused_rows))
            return self._payload("refused", refusals=refusals or ["every edge was lost"], **extra)
        problems = self._problems(before, after)
        self.warnings = problems + self.warnings + [STALE_EDGE_REFS]
        if self.stopped:
            self.warnings.insert(0, self.stopped)
        short = counts["built"] < counts["requested"] or self.lost
        return self._payload("partial" if short else "suspect" if problems else "built", **extra)

    def run(self):
        self._read_sizes()
        self._select()
        blocked, notes = fo.preflight(self.design, self.document,
                                      [self._entry(key) for key in dict.fromkeys(p["key"] for p in self.parts)])
        self.refusals += blocked
        self.warnings += notes
        if self.refusals:
            return self._payload("refused", refusals=self.refusals)

        for key in dict.fromkeys(part["key"] for part in self.parts):
            entry = self._entry(key)
            if not entry["visible"]:
                self.warnings.append(f"{label(entry)} is hidden; hidden bodies are often stale")
        suspects = self._suspects()
        if self.opts["dry_run"]:
            return self._payload("planned", plan=self._plan(), suspects=suspects)

        before, token = fo.baseline(self.design)
        self._climb()
        return self._finish(before, token, suspects)


# -- Fusion layer ---------------------------------------------------------------


def _collection(entry, edges):
    """What the feature takes: a sub-component's features work on its native edges."""
    members = adsk.core.ObjectCollection.create()
    for edge in edges:
        members.add(edge if entry["occurrence_path"] == ROOT_PATH else native(edge))
    return members


class FusionEdges(FusionScene):
    """``FusionScene`` plus the sizes and the add. Lives for one call."""

    def __init__(self, design):
        super().__init__(design)
        self._units = design.unitsManager

    def refresh(self):
        """Wrappers from before an edit may be dead, so the next read takes new ones."""
        self._entries = None

    def _value(self, value):
        return fo.value_input(value, self._units)

    def length_cm(self, value, units):
        """A ``read_value`` length in cm. Raises ValueError for an expression Fusion cannot read."""
        if value["number"] is not None:
            return value["number"] * CM_PER_UNIT[units]
        self._value(value)
        return self._units.evaluateExpression(value["text"], "cm")

    def _add_chamfer(self, features, entry, located, flipped):
        entity = features.createInput2()
        for item in located:
            job, edges = item["set"], _collection(entry, item["edges"])
            size = self._value(job["size"])
            if job["size_two"] is None:
                added = entity.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edges, size, job["tangent"])
            else:
                # Five arguments: the flip sits between the distances and the chain flag.
                added = entity.chamferEdgeSets.addTwoDistancesChamferEdgeSet(
                    edges, size, self._value(job["size_two"]), job["index"] in flipped, job["tangent"])
            if not added:
                raise RuntimeError(f"Fusion would not take the edges of set {job['index']}")
        return features.add(entity)

    def _add_fillet(self, features, entry, located):
        entity = features.createInput()
        for item in located:
            job = item["set"]
            added = entity.edgeSetInputs.addConstantRadiusEdgeSet(_collection(entry, item["edges"]),
                                                                  self._value(job["size"]), job["tangent"])
            if added is None:
                raise RuntimeError(f"Fusion would not take the edges of set {job['index']}")
        return features.add(entity)

    def add(self, entry, located, kind, flipped):
        """One feature in the component that owns the body. *located* is one body's edge sets."""
        features = entry["_body"].parentComponent.features
        if kind == "fillet":
            return self._add_fillet(features.filletFeatures, entry, located)
        return self._add_chamfer(features.chamferFeatures, entry, located, flipped)


def edge_treatment(arguments):
    """Chamfer or fillet edges of the active design."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            refusals, _ = fo.preflight(None)
            return fo.write_result(fo.refused(opts["units"], refusals, dry_run=opts["dry_run"]))
        payload = Job(design, safe(app, "activeDocument"), FusionEdges(design), opts).run()
        log(f"[MCP] edge_treatment {opts['kind']}: {payload['status']}")
        return fo.write_result(payload)
    except Exception as exc:
        log(f"[MCP] edge_treatment failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
