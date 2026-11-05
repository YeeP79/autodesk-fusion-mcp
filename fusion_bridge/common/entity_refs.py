"""EntityRef: name a face or edge across calls, and describe one.

An EntityRef is ``{"token", "kind", "point", "body"}``, where ``token`` or
``point`` may be omitted but not both. Wrappers do not survive between calls and
one entity's token string can differ between reads, so a ref is resolved fresh
on every call and tokens are never compared as strings; entities are compared
only with ``==``.

``findEntityByToken`` keeps the assembly context and gives world coordinates
(RC-1), and a face split by a cut comes back as several results in no order to
rely on (RC-3), which is why every ref also carries a point: ``point`` picks
among them.

All internal points are world cm; a parsed ref's point is too. Records hold
values in the caller's units, so the filters compare like with like and
``diameter: {min: 1, max: 1}`` matches a rounded 1.0. Keys starting with ``_``
hold Fusion objects or working values and never reach a report (``public``).

Nothing at module level imports ``adsk``: the filters load and run anywhere.
Only ``FusionScene`` asks Fusion anything, and ``resolve`` takes any object with
its methods, so fakes can drive it.
"""

import math
from collections import namedtuple

from .api import attempt, items, safe
from .args import is_finite_number
from .bodies import (
    ROOT_PATH,
    SURFACE_TYPES,
    assign_keys,
    bbox,
    body_entries,
    face_record,
    native,
    object_type,
    parse_body_refs,
    path_of,
    resolve_targets,
    vertex_points,
)
from .geometry import (
    CM_PER_UNIT,
    along,
    display_direction,
    display_point,
    dot,
    scaled,
    sub,
    to_units,
    unit,
    xyz,
)

KINDS = ("face", "edge", "vertex")
REF_KEYS = {"token", "kind", "point", "body"}

# How far a ref's point may sit from the entity and still name it: 0.001 in.
REF_TOLERANCE_CM = 0.00254

# adsk.core.Curve3DTypes values (Line=0 ... Polyline=7).
CURVE_TYPES = {0: "line", 1: "arc", 2: "circle", 3: "ellipse", 4: "ellipse", 5: "line", 6: "spline", 7: "spline"}
_ENTITY_KINDS = {"BRepFace": "face", "BRepEdge": "edge", "BRepVertex": "vertex"}

# adsk.fusion.PointContainment values.
_CONTAINMENT = {0: "inside", 1: "on", 2: "outside"}

_LABEL_DIGITS = 3
# Coincident ends of an edge, and how far two cylinders may differ and be one surface.
_SAME_POINT_CM = 1e-6
_SAME_SURFACE_CM = 1e-4
_PARALLEL_DOT = 1.0 - 1e-9
# A face on a plane must lie within this many degrees of it, whatever its samples say:
# a tilted disc has only two sample points, and both can sit on the plane.
ON_PLANE_NORMAL_DEG = 1.0
# Normals closer than this across an edge make it tangent.
TANGENT_DEG = 0.1
# How far from an edge a containment test steps; clears containment's own tolerance.
EDGE_STEP_CM = 1e-3
_EDGE_SAMPLES = 5


# -- Parsing a ref --------------------------------------------------------------


def parse_ref(value, units, where="ref"):
    """Check one EntityRef argument; returns it with ``point`` in cm and ``body`` a tuple.

    Raises ValueError. ``kind`` may be left out only with a token: a point alone
    gives the search nothing to look for.
    """
    if not isinstance(value, dict) or set(value) - REF_KEYS:
        raise ValueError(f"'{where}' must be an object with token, kind, point and body")
    token, kind, point, body = (value.get(key) for key in ("token", "kind", "point", "body"))
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise ValueError(f"'{where}.token' must be a non-empty string")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"'{where}.kind' must be one of {list(KINDS)}")
    if point is not None:
        if not isinstance(point, list) or len(point) != 3 or not all(is_finite_number(c) for c in point):
            raise ValueError(f"'{where}.point' must be an array of three finite numbers [x, y, z]")
        point = tuple(float(c) * CM_PER_UNIT[units] for c in point)
    if token is None and point is None:
        raise ValueError(f"'{where}' needs a token, a point, or both")
    if token is None and kind is None:
        raise ValueError(f"'{where}' with only a point needs 'kind'")
    return {"token": token, "kind": kind, "point": point,
            "body": None if body is None else parse_body_refs([body], f"{where}.body")[0]}


# -- Resolving a ref ------------------------------------------------------------


class FusionScene:
    """What ``resolve`` and the scans ask of Fusion. Points are world cm.

    Lives for one call: the keyed body entries are read once and hold wrappers.
    """

    def __init__(self, design):
        import adsk.core
        import adsk.fusion
        self._adsk = adsk
        self._design = design
        self._root = design.rootComponent
        self._entries = None
        self._types = {"face": adsk.fusion.BRepEntityTypes.BRepFaceEntityType,
                       "edge": adsk.fusion.BRepEntityTypes.BRepEdgeEntityType,
                       "vertex": adsk.fusion.BRepEntityTypes.BRepVertexEntityType}

    def _point(self, point):
        return self._adsk.core.Point3D.create(*point)

    def entries(self):
        if self._entries is None:
            self._entries = assign_keys(list(body_entries(self._root)))
        return self._entries

    def by_token(self, token):
        """Everything the token names; a token Fusion rejects names nothing."""
        return attempt(lambda: list(self._design.findEntityByToken(token)), [])

    def at_point(self, point, kind, tolerance_cm=REF_TOLERANCE_CM):
        found = self._root.findBRepUsingPoint(self._point(point), self._types[kind], tolerance_cm, False)
        return items(found)

    def distance(self, entity, point):
        """Minimum distance in cm from *point* to *entity*; None when it cannot be measured."""
        manager = self._adsk.core.Application.get().measureManager
        return attempt(lambda: manager.measureMinimumDistance(self._point(point), entity).value)

    def contains(self, entry, point):
        """'inside', 'on', 'outside' or 'unknown' for a world point against a body."""
        state = attempt(lambda: entry["_body"].pointContainment(self._point(point)))
        return _CONTAINMENT.get(state, "unknown")


Resolution = namedtuple("Resolution", "entity kind resolved_by warnings refusal")


def _refused(code, error, **detail):
    return Resolution(None, None, None, [], {"code": code, "error": error, **detail})


def resolved_row(resolution, role=None):
    """One row of a result's ``resolved`` list."""
    row = {"kind": resolution.kind, "resolved_by": resolution.resolved_by}
    return {"role": role, **row} if role else row


def entity_kind(entity):
    return _ENTITY_KINDS.get(object_type(entity))


def same_body(body, entry):
    """Is *body* the one in *entry*? ``==`` first, then path and name unless the name repeats."""
    if body is None:
        return False
    if same_entity(body, entry["_body"]):
        return True
    named_uniquely = entry.get("key") == f"{entry['occurrence_path']}/{entry['body']}"
    return named_uniquely and path_of(body) == entry["occurrence_path"] and safe(body, "name") == entry["body"]


def _on_body(entity, entry):
    return entry is None or same_body(safe(entity, "body"), entry)


def _in_context(entity, entry):
    """A native result for a body in an occurrence, as a proxy of that occurrence.

    Native entities ignore the occurrence transform, so their geometry reads in
    component space. One that cannot be mapped stays native and fails ``_on_body``.
    """
    if entry is None or entry["occurrence_path"] == ROOT_PATH or safe(entity, "assemblyContext") is not None:
        return entity
    occurrence = safe(entry["_body"], "assemblyContext")
    mapped = attempt(lambda: entity.createForAssemblyContext(occurrence))
    return entity if mapped is None else mapped


def same_entity(a, b):
    """``==`` on two wrappers; a stale one raises, and is not the same entity."""
    try:
        return a == b
    except Exception:
        return False


def unique_entities(entities):
    kept = []
    for entity in entities:
        if not any(same_entity(entity, other) for other in kept):
            kept.append(entity)
    return kept


def _target_entry(scene, body):
    """The entry a ref's body names, or None for a ref with no body; second value a refusal."""
    if body is None:
        return None, None
    targets, _, problem = resolve_targets(scene.entries(), [body])
    if problem:
        code = "ambiguous_body" if problem["error"].startswith("ambiguous") else "body_not_found"
        return None, _refused(code, problem["error"], candidates=problem["candidates"])
    return targets[0], None


def resolve(scene, ref, tolerance_cm=REF_TOLERANCE_CM):
    """Find the entity a parsed ref names now; returns a ``Resolution``.

    1. The token. Several results (a split face) are told apart by ``point``,
       never by order: on none or on two of them it is ``ambiguous_split``.
    2. The point must lie on what the token found. If not, the token names
       something else now, or the point was cut away.
    3. Search at the point, within the ref's body. Nothing there, but the token
       found one entity on the right body: accept it with a warning.

    Native results are mapped into the body's occurrence first, since a native
    entity's geometry would fail step 2 and mean the wrong place afterward.
    """
    entry, refusal = _target_entry(scene, ref["body"])
    if refusal:
        return refusal
    token, kind, point = ref["token"], ref["kind"], ref["point"]

    found = [_in_context(e, entry) for e in scene.by_token(token)] if token else []
    if kind is not None:
        found = [e for e in found if entity_kind(e) == kind]
    found = unique_entities(found)

    if point is None:
        if len(found) > 1:
            return _refused("ambiguous_split", f"the token now names {len(found)} entities and the ref "
                            "has no point to tell them apart", count=len(found))
        if found and _on_body(found[0], entry):
            return Resolution(found[0], kind or entity_kind(found[0]), "token", [], None)
        return _refused("not_found", "the token names nothing on the ref's body")

    def lies_on(entity):
        distance = scene.distance(entity, point)
        return distance is not None and distance <= tolerance_cm

    on_point = [e for e in found if lies_on(e)]
    if len(found) > 1 and len(on_point) != 1:
        return _refused("ambiguous_split", f"the token now names {len(found)} entities and the point lies "
                        f"on {len(on_point)} of them", count=len(found))
    if on_point and _on_body(on_point[0], entry):
        return Resolution(on_point[0], kind or entity_kind(on_point[0]), "token", [], None)

    search_kind = kind or (entity_kind(found[0]) if found else None)
    hits = []
    if search_kind is not None:
        mapped = (_in_context(hit, entry) for hit in scene.at_point(point, search_kind, tolerance_cm))
        hits = unique_entities(hit for hit in mapped if _on_body(hit, entry))
    if len(hits) == 1:
        return Resolution(hits[0], search_kind, "point", [], None)
    if len(hits) > 1:
        return _refused("ambiguous_point", f"{len(hits)} {search_kind}s lie at the point; "
                        "name the body or use a point away from their shared boundary", count=len(hits))
    if len(found) == 1 and _on_body(found[0], entry):
        return Resolution(found[0], kind or entity_kind(found[0]), "token",
                          ["the ref's point no longer lies on the entity its token names; "
                           "the token was accepted because nothing is at the point"], None)
    return _refused("not_found", "nothing matches the ref's token or point")


# -- References and labels ------------------------------------------------------


def body_ref(entry):
    """A BodyRef; ``body_index`` only where the name repeats, since an index can shift."""
    ref = {"occurrence_path": entry["occurrence_path"], "body_name": entry["body"]}
    if entry.get("key", "").endswith(f"#{entry['index']}"):
        ref["body_index"] = entry["index"]
    return ref


def make_ref(entity, kind, units, entry=None):
    """The EntityRef of a face or edge. ``point`` is documented to lie on the entity; a centroid is not."""
    token = safe(entity, "entityToken")
    point = safe(entity, "pointOnFace" if kind == "face" else "pointOnEdge")
    ref = {"kind": kind}
    if token:
        ref["token"] = token
    if point is not None:
        ref["point"] = display_point(xyz(point), units)
    if entry is not None:
        ref["body"] = body_ref(entry)
    return ref


def _number(value):
    return f"{round(value, _LABEL_DIGITS) + 0.0:.{_LABEL_DIGITS}f}"


def _coordinate(value):
    text = f"{round(value, _LABEL_DIGITS) + 0.0:.{_LABEL_DIGITS}f}".rstrip("0").rstrip(".")
    return text or "0"


def _coordinates(point):
    return "[" + ",".join(_coordinate(c) for c in point) + "]"


def direction_label(vector, unsigned=False):
    """'+z', '-x', or rounded components. *unsigned* gives an axis one spelling for both senses."""
    rounded = [round(c, _LABEL_DIGITS) + 0.0 for c in vector]
    if unsigned and next((c for c in rounded if c), 0.0) < 0:
        rounded = [-c + 0.0 for c in rounded]
    for name, value in zip("xyz", rounded):
        if abs(value) == 1.0:
            return ("+" if value > 0 else "-") + name
    return _coordinates(rounded)


def _middle(box):
    return [(a + b) / 2 for a, b in zip(*box)] if box else None


def face_label(rec):
    """A label from rounded geometry alone, so two runs agree."""
    kind, at = rec["type"], _middle(rec["bbox"])
    parts = [kind]
    if kind == "plane" and rec.get("normal"):
        parts += [f"n={direction_label(rec['normal'])}", f"area={_number(rec['area'])}"]
    elif kind in ("cylinder", "sphere"):
        parts.append(f"d={_number(rec['diameter'])}")
    elif kind == "torus":
        parts += [f"R={_number(rec['major_radius'])}", f"r={_number(rec['minor_radius'])}"]
    elif kind == "cone":
        parts.append(f"half_angle={_number(rec['half_angle_deg'])}")
    else:
        parts.append(f"area={_number(rec['area'])}")
    if rec.get("axis_direction"):
        parts.append(f"axis={direction_label(rec['axis_direction'], unsigned=True)}")
    if rec.get("length_along_axis") is not None:
        parts.append(f"len={_number(rec['length_along_axis'])}")
    if at:
        parts.append(f"at {_coordinates(at)}")
    return " ".join(parts)


def edge_label(rec):
    kind = rec["type"]
    parts = [kind]
    if kind in ("circle", "arc"):
        parts.append(f"d={_number(rec['diameter'])}")
        if kind == "arc":
            parts.append(f"sweep={_number(rec['sweep_deg'])}")
    else:
        parts.append(f"len={_number(rec['length'])}")
    if rec.get("direction"):
        parts.append(f"dir={direction_label(rec['direction'], unsigned=True)}")
    at = rec.get("center") or _middle(rec.get("bbox"))
    if at:
        parts.append(f"at {_coordinates(at)}")
    return " ".join(parts)


# -- Describing -----------------------------------------------------------------


def same_cylinder(a, b, tolerance_cm=_SAME_SURFACE_CM):
    """Are two ``(origin, axis, radius)`` cylinders one surface? Axis sense does not matter."""
    axis_a, axis_b = unit(a[1]), unit(b[1])
    if axis_a is None or axis_b is None or abs(a[2] - b[2]) > tolerance_cm:
        return False
    if abs(dot(axis_a, axis_b)) < _PARALLEL_DOT:
        return False
    return perpendicular_distance(a[0], axis_a, b[0]) <= tolerance_cm


def perpendicular_distance(origin, axis, point):
    """Distance from *point* to the line through *origin* along unit *axis*."""
    rel = sub(point, origin)
    off = sub(rel, scaled(axis, dot(rel, axis)))
    return math.sqrt(dot(off, off))


def _cylinder_of(face):
    """``(origin, axis, radius)`` of a cylinder face; None for anything else or a failed read."""
    try:
        g = face.geometry
        if SURFACE_TYPES.get(g.surfaceType) != "cylinder":
            return None
        return xyz(g.origin), xyz(g.axis), g.radius
    except Exception:
        return None


def split_siblings(face):
    """How many other faces share an edge with this cylinder and lie on its surface."""
    mine = _cylinder_of(face)
    if mine is None:
        return 0
    neighbors = unique_entities(other for edge in items(safe(face, "edges")) for other in items(safe(edge, "faces")))
    siblings = 0
    for other in neighbors:
        theirs = _cylinder_of(other)
        if not same_entity(other, face) and theirs is not None and same_cylinder(mine, theirs):
            siblings += 1
    return siblings


def describe_face(face, units, entry=None):
    """A report record for one face, in world coordinates and *units*."""
    kind = SURFACE_TYPES.get(safe(safe(face, "geometry"), "surfaceType"), "unknown")
    found = face_record(face, kind, units)
    box = bbox(face)
    rec = {"ref": make_ref(face, "face", units, entry), "type": kind, "area": found["area"],
           "bbox": None if box is None else [display_point(c, units) for c in box],
           "distance": None, "created_by": None}
    for key, value in found.items():
        if key not in ("area", "point", "is_param_reversed", "concavity_basis", "_temp_id"):
            rec[key] = value
    if kind == "sphere":
        rec["diameter"] = to_units(2 * face.geometry.radius, units)
    if kind == "cylinder":
        rec["split_siblings"] = split_siblings(face)
    rec["label"] = face_label(rec)
    rec["_entity"] = face
    rec["_temp_id"] = found["_temp_id"]
    point = rec["ref"].get("point")
    rec["_samples"] = [display_point(p, units) for p in vertex_points(face)] + ([point] if point else [])
    return rec


def _vertex_point(edge, name):
    return attempt(lambda: xyz(getattr(edge, name).geometry))


def edge_samples(edge):
    """World points spread along an edge, ends included; empty if the evaluator refuses."""
    def read():
        evaluator = edge.evaluator
        ok, low, high = evaluator.getParameterExtents()
        if not ok:
            return []
        points = []
        for i in range(_EDGE_SAMPLES):
            ok, p = evaluator.getPointAtParameter(low + (high - low) * i / (_EDGE_SAMPLES - 1))
            if ok:
                points.append(xyz(p))
        return points

    return attempt(read, [])


def adjacent_refs(edge, units, entry=None):
    """EntityRefs of the faces meeting at an edge; a seam meets one face twice."""
    return [make_ref(face, "face", units, entry) for face in unique_entities(items(safe(edge, "faces")))]


def describe_edge(edge, units, entry=None, adjacent=True, samples=True):
    """A report record for one edge. A scan turns off *adjacent* and *samples* unless they are needed."""
    g = safe(edge, "geometry")
    kind = CURVE_TYPES.get(safe(g, "curveType"), "unknown")
    length_cm = safe(edge, "length", 0.0) or 0.0
    start, end = _vertex_point(edge, "startVertex"), _vertex_point(edge, "endVertex")
    closed = kind == "circle" or (start is not None and end is not None
                                  and math.dist(start, end) <= _SAME_POINT_CM)
    box = bbox(edge)
    rec = {"ref": make_ref(edge, "edge", units, entry), "type": kind, "length": to_units(length_cm, units),
           "closed": closed, "distance": None,
           "bbox": None if box is None else [display_point(c, units) for c in box]}
    if kind in ("circle", "arc", "ellipse"):
        rec["center"] = display_point(xyz(g.center), units)
        rec["normal"] = display_direction(xyz(g.normal))
    if kind in ("circle", "arc"):
        rec["radius"] = to_units(g.radius, units)
        rec["diameter"] = to_units(2 * g.radius, units)
        rec["sweep_deg"] = 360.0 if kind == "circle" else round(math.degrees(length_cm / g.radius), 1)
    if kind == "line" and start is not None and end is not None:
        rec["direction"] = display_direction(sub(end, start))
    if start is not None:
        rec["start"] = display_point(start, units)
    if end is not None:
        rec["end"] = display_point(end, units)
    if adjacent:
        rec["faces"] = adjacent_refs(edge, units, entry)
    rec["label"] = edge_label(rec)
    rec["_entity"] = edge
    rec["_temp_id"] = safe(native(edge), "tempId")
    if samples:
        points = edge_samples(edge) or [p for p in (start, end) if p is not None]
        rec["_samples"] = [display_point(p, units) for p in points] + (
            [rec["ref"]["point"]] if "point" in rec["ref"] else [])
    return rec


def public(record):
    """A record without its working keys."""
    return {key: value for key, value in record.items() if not key.startswith("_")}


# -- Filters (pure) -------------------------------------------------------------


def in_range(value, bounds):
    """Inclusive ``(low, high)`` test, either bound None. No value, or NaN, never matches."""
    if not is_finite_number(value):
        return False
    low, high = bounds
    return (low is None or value >= low) and (high is None or value <= high)


def angle_between(a, b, signed=True):
    """Degrees between two vectors; unsigned treats a vector and its reverse as equal."""
    a, b = unit(a), unit(b)
    if a is None or b is None:
        return None
    cosine = dot(a, b)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine if signed else abs(cosine)))))


def aligned(vector, wanted):
    """*wanted* holds ``direction``, ``tolerance_deg``, ``signed``."""
    if not vector:
        return False
    angle = angle_between(vector, wanted["direction"], wanted["signed"])
    return angle is not None and angle <= wanted["tolerance_deg"]


def in_box(box, wanted):
    """Is the whole of *box* inside ``wanted = (min, max)``, boundaries included?"""
    if not box:
        return False
    return all(wanted[0][i] <= box[0][i] and box[1][i] <= wanted[1][i] for i in range(3))


def on_plane(samples, normal, wanted):
    """Do *samples* lie on the plane, and any stated *normal* run with it?

    *wanted* holds ``point``, unit ``normal`` and ``tolerance``.
    """
    if not samples:
        return False
    if normal:
        angle = angle_between(normal, wanted["normal"], signed=False)
        if angle is None or angle > ON_PLANE_NORMAL_DEG:
            return False
    return all(abs(dot(sub(p, wanted["point"]), wanted["normal"])) <= wanted["tolerance"] for p in samples)


def bbox_distance(box, point):
    """Distance from *point* to a ``(min, max)`` box; 0 inside it."""
    gaps = (max(lo - c, 0.0, c - hi) for lo, hi, c in zip(box[0], box[1], point))
    return math.sqrt(sum(g * g for g in gaps))


def _matches_axis(direction, origin, wanted):
    if not aligned(direction, wanted):
        return False
    through = wanted.get("through")
    if through is None:
        return True
    axis = unit(direction)
    return (origin is not None
            and perpendicular_distance(origin, axis, through) <= wanted["through_tolerance"])


def _matches_common(rec, filters, axis_direction, axis_origin, length):
    if "normal" in filters and not aligned(rec.get("normal"), filters["normal"]):
        return False
    if "axis" in filters and not _matches_axis(axis_direction, axis_origin, filters["axis"]):
        return False
    for key, value in (("diameter", rec.get("diameter")), ("length", length), ("sweep_deg", rec.get("sweep_deg"))):
        if key in filters and not in_range(value, filters[key]):
            return False
    if "in_box" in filters and not in_box(rec.get("bbox"), filters["in_box"]):
        return False
    if "on_plane" in filters and not on_plane(rec.get("_samples"), rec.get("normal"), filters["on_plane"]):
        return False
    return True


def matches_face(rec, filters):
    """Every filter but ``near`` and ``edge_convexity``, which need Fusion and run last.

    A filter on a value this type does not have fails: a plane has no diameter.
    """
    if "surface_types" in filters and rec["type"] not in filters["surface_types"]:
        return False
    if "area" in filters and not in_range(rec.get("area"), filters["area"]):
        return False
    if "concavity" in filters and rec.get("concavity") != filters["concavity"]:
        return False
    return _matches_common(rec, filters, rec.get("axis_direction"), rec.get("axis_origin") or rec.get("center"),
                           rec.get("length_along_axis"))


def matches_edge(rec, filters):
    """As ``matches_face``. An edge's axis is a circle's normal or a line's direction."""
    if "curve_types" in filters and rec["type"] not in filters["curve_types"]:
        return False
    return _matches_common(rec, filters, rec.get("normal") or rec.get("direction"),
                           rec.get("center") or rec.get("start"), rec.get("length"))


# -- Edge convexity -------------------------------------------------------------


def classify_edge(point, normal_a, normal_b, contains, step=EDGE_STEP_CM):
    """'convex', 'concave', 'tangent', or None when the test cannot tell.

    The step runs along the *difference* of the outward normals. In the plane
    across the edge that direction is square to their bisector, so it stays out
    of the solid wedge at a convex edge and out of the empty wedge at a concave
    one, at any angle. The sum cannot tell them apart: it points into empty
    space at both. Both senses must agree, which catches a curved face that
    turns the step around.

    *contains* takes a point and returns 'inside', 'outside' or anything else.
    """
    a, b = unit(normal_a), unit(normal_b)
    if a is None or b is None:
        return None
    if angle_between(a, b) <= TANGENT_DEG:
        return "tangent"
    side = unit(sub(a, b))
    states = {contains(along(point, side, step)), contains(along(point, side, -step))}
    if states == {"inside"}:
        return "concave"
    if states == {"outside"}:
        return "convex"
    return None


def _normal_at(face, point):
    """Outward normal at a point; the face evaluator honors the face's orientation."""
    try:
        ok, normal = face.evaluator.getNormalAtPoint(point)
        return xyz(normal) if ok else None
    except Exception:
        return None


def read_edge_convexity(edge, entry, scene):
    """Classify one edge of a solid; ``entry`` is its body."""
    point = safe(edge, "pointOnEdge")
    faces = unique_entities(items(safe(edge, "faces")))
    if point is None or not faces:
        return None
    if len(faces) == 1:
        return "tangent"  # a seam: a face meeting itself

    normals = [_normal_at(face, point) for face in faces[:2]]
    if None in normals:
        return None
    return classify_edge(xyz(point), normals[0], normals[1], lambda p: scene.contains(entry, p))
