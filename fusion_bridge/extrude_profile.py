"""Extrude a sketch profile with explicit targets: extrude_profile.

Left alone, a cut takes every body it passes through, other components' bodies
included. So cut, join and intersect need ``target_bodies``, every body in the
design is measured before and after, and a feature that reached outside its
targets is taken back (``on_collateral``).

Fusion's ``participantBodies`` is documented for cut and intersect only. A join
sets it too, and the measurement decides whether Fusion listened: when another
body was consumed or grew, or the setter refuses, the feature is undone and the
join is redone as a new body plus a Combine naming exactly the targets. Either
way the result has the same shape; ``join_route`` says which ran.

The extrude runs along the sketch's normal, so ``direction`` only picks a sense,
and that sense is asked of ``profile.plane``, never assumed. Sketches are native:
under an occurrence their planes are in component space and go through the
occurrence's transform. ``ExtrudeFeatureInput.setDistanceExtent`` is gone from
the current API, so every one-sided extent goes through ``setOneSideExtent``.

Internal lengths are cm; ``t`` is a distance along the extrude's direction from
the profile plane's origin. The swept box is axis-aligned and ignores taper, so
it can only say what is near the path; ``plan_basis`` names it when it is used.
"""

import traceback

import adsk.core
import adsk.fusion

from .common.api import attempt, items, safe, strict_items
from .common.args import bool_arg, is_finite_number, reject_unknown, require_object, text_arg
from .common.bodies import (
    ROOT_PATH,
    SURFACE_TYPES,
    bbox,
    bbox_overlap,
    find_occurrence,
    label,
    native,
    occurrence_frame,
    parse_body_ref,
    parse_body_refs,
    resolve_targets,
    vertex_points,
)
from .common.entity_refs import (
    REF_TOLERANCE_CM,
    FusionScene,
    angle_between,
    body_ref,
    entity_kind,
    in_context,
    make_ref,
    parse_ref,
    resolve,
    resolved_row,
    same_body,
    same_entity,
    unique_entities,
)
from .common.feature_ops import (
    add_feature,
    baseline,
    body_changes,
    envelope,
    feature_row,
    grew_outside,
    lost_outside,
    preflight,
    read_value,
    refused,
    snapshot,
    unchanged,
    undo,
    value_input,
    write_result,
)
from .common.geometry import (
    CM_PER_UNIT,
    along,
    direction_from_local,
    display_direction,
    display_point,
    dot,
    from_local,
    scaled,
    sub,
    to_local,
    to_units,
    unit,
    xyz,
)
from .common.results import error_result
from .dispatch import get_app, log
from .sketch_profile import choose_profile, describe_profiles

_KNOWN = {"sketch_token", "sketch_name", "component", "occurrence_path", "profile_select", "containing_point",
          "containing_points", "operation", "target_bodies", "extent", "direction", "side", "taper_deg",
          "taper_expression", "start_offset", "start_offset_expression", "body_name", "feature_name",
          "on_collateral", "units", "dry_run"}

OPERATIONS = {"new_body": "NewBodyFeatureOperation", "join": "JoinFeatureOperation",
              "cut": "CutFeatureOperation", "intersect": "IntersectFeatureOperation"}
PROFILE_SELECTS = ("only", "containing_point", "containing_points", "most_loops", "largest")
SIDES = {"positive": 1, "negative": -1}
ON_COLLATERAL = ("undo", "keep")

# What each extent type takes, beyond 'type'.
_EXTENT_KEYS = {
    "distance": {"distance", "distance_expression"},
    "symmetric": {"distance", "distance_expression", "full_length"},
    "two_sides": {"distance", "distance_expression", "side_two_distance", "side_two_distance_expression"},
    "through_all": {"both_sides"},
    "to_entity": {"to", "to_body", "to_offset", "to_offset_expression", "chained"},
}

# "More than 1 degree off the sketch normal" is a wrong answer, not a rounding error.
DIRECTION_LIMIT_DEG = 1.0
PARALLEL_FACE_DEG = 0.01
# A cut ending within this of a parallel face leaves a floor or a sliver: 0.001 in.
ABUTMENT_TOLERANCE_CM = 0.00254
# Closer than this the end plane is on the face. The floor that caused this
# check was 25 nm, 250 times wider.
TOUCHING_CM = 1e-8
DEPTH_TOLERANCE_CM = REF_TOLERANCE_CM
# Profile area comes from areaProperties, which is accurate only to a stated margin.
VOLUME_CHECK_REL = 5e-3

ABUTMENT_ADVICE = "use through_all or to_entity; abutting cuts leave a floor"
_FACE_SETS = (("start", "startFaces"), ("end", "endFaces"), ("side", "sideFaces"))


class Refused(Exception):
    """A call that goes no further. *extra* rides in the result beside ``refusals``."""

    def __init__(self, problems, **extra):
        super().__init__("; ".join(problems))
        self.problems = list(problems)
        self.extra = extra


# -- Pure geometry --------------------------------------------------------------


def extrude_sign(normal, direction, limit_deg=DIRECTION_LIMIT_DEG):
    """+1 along *normal*, -1 against it, None when *direction* is over the limit off its line."""
    off = angle_between(normal, direction, signed=False)
    if off is None or off > limit_deg:
        return None
    return 1 if dot(normal, direction) >= 0 else -1


def abutment_gap(end_t, face_t, tolerance=ABUTMENT_TOLERANCE_CM):
    """The gap between an end plane and a parallel face when it is an abutment, else None.

    On the face there is no gap, and past the tolerance nothing thin is left.
    """
    gap = abs(face_t - end_t)
    return gap if TOUCHING_CM < gap <= tolerance else None


def abutments(planes, origin, direction, end_t, box):
    """``[{"point", "gap"}]`` for the planes an end plane abuts, within *box*.

    *planes* are ``(point, normal, bbox)``. A face away from the swept box can
    share the height and still leave no floor under this profile; a face just
    past the box's end is the case being looked for, so the box is padded.
    """
    found = []
    for point, normal, face_box in planes:
        angle = angle_between(normal, direction, signed=False)
        if angle is None or angle > PARALLEL_FACE_DEG or not bbox_overlap(face_box, box, ABUTMENT_TOLERANCE_CM):
            continue
        gap = abutment_gap(end_t, dot(sub(point, origin), direction))
        if gap is not None:
            found.append({"point": point, "gap": gap})
    return found


def span_along(points, origin, direction):
    """``(low, high)`` of *points* as distances along *direction* from *origin*."""
    ts = [dot(sub(p, origin), direction) for p in points]
    return min(ts), max(ts)


def box_corners(box):
    (x0, y0, z0), (x1, y1, z1) = box
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def sweep_span(base, forward, back, reach):
    """``(low, high)`` an extrude covers. *forward* or *back* None runs out to *reach*."""
    low = min(reach[0], base) if back is None else base - back
    high = max(reach[1], base) if forward is None else base + forward
    return low, high


def swept_box(points, direction, low, high):
    """The axis-aligned box of *points* moved along *direction* from *low* to *high*."""
    moved = [along(p, direction, t) for p in points for t in (low, high)]
    return tuple(min(p[i] for p in moved) for i in range(3)), tuple(max(p[i] for p in moved) for i in range(3))


def side_verdict(low, high, base, tolerance=DEPTH_TOLERANCE_CM):
    """'wrong_side' when what was built lies wholly behind its start plane, else 'ok'."""
    return "wrong_side" if high - base <= tolerance and low - base < -tolerance else "ok"


def volume_verdict(expected, actual):
    """'ok', 'mismatch', or 'not_applicable' with either figure unknown."""
    if expected is None or actual is None:
        return "not_applicable"
    return "ok" if abs(actual - expected) <= VOLUME_CHECK_REL * abs(expected) else "mismatch"


def profile_brief(row):
    return {"area": round(row["area"], 5), "centroid": [round(c, 5) for c in row["centroid"]],
            "loops": row["loop_count"]}


def pick_profiles(select, described, per_point=()):
    """Indices of the chosen profiles; raises ``Refused``. Never by position in the list.

    *per_point* holds, for each asked point, ``describe_profiles`` with that query.
    """
    briefs = [profile_brief(row) for row in described]
    if not described:
        raise Refused(["the sketch has no closed profiles"])
    if select == "only":
        if len(described) != 1:
            raise Refused([f"profile_select 'only' but the sketch has {len(described)} profiles; choose with "
                           "containing_point, containing_points, most_loops or largest"], profiles=briefs)
        return [described[0]["index"]]
    if select == "largest":
        return [choose_profile(described)[0]]
    if select == "most_loops":
        return [max(described, key=lambda row: (row["loop_count"], row["area"]))["index"]]
    picked = []
    for number, rows in enumerate(per_point):
        index = choose_profile(rows)[1]
        if index is None:
            why = ("could not be determined" if any(r.get("contains_point") is None for r in rows)
                   else "is in no profile")
            raise Refused([f"point {number} {why}"], profiles=briefs)
        if index not in picked:
            picked.append(index)
    return picked


# -- Argument validation --------------------------------------------------------


def _triple(value, key):
    if not isinstance(value, list) or len(value) != 3 or not all(is_finite_number(c) for c in value):
        raise ValueError(f"'{key}' must be an array of three finite numbers [x, y, z]")
    return tuple(float(c) for c in value)


def _point(value, key, units):
    return scaled(_triple(value, key), CM_PER_UNIT[units])


def _choice(arguments, key, allowed, default=None):
    value = arguments.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"'{key}' must be one of {list(allowed)}")
    return value


def _parse_profile_select(arguments, units):
    """(select, points in cm); the keys for points only go with the selects that use them."""
    select = _choice(arguments, "profile_select", PROFILE_SELECTS, "only")
    one, many = arguments.get("containing_point"), arguments.get("containing_points")
    if select != "containing_point" and one is not None:
        raise ValueError("'containing_point' needs profile_select 'containing_point'")
    if select != "containing_points" and many is not None:
        raise ValueError("'containing_points' needs profile_select 'containing_points'")
    if select == "containing_point":
        if one is None:
            raise ValueError("profile_select 'containing_point' needs 'containing_point'")
        return select, [_point(one, "containing_point", units)]
    if select == "containing_points":
        if not isinstance(many, list) or not many:
            raise ValueError("profile_select 'containing_points' needs a non-empty 'containing_points'")
        return select, [_point(p, "containing_points entry", units) for p in many]
    return select, []


def _parse_extent(value, units):
    if not isinstance(value, dict):
        raise ValueError("'extent' is required and must be an object")
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in _EXTENT_KEYS:
        raise ValueError(f"'extent.type' must be one of {list(_EXTENT_KEYS)}")
    reject_unknown({k: v for k, v in value.items() if k != "type"}, _EXTENT_KEYS[kind],
                   f"not used by a {kind} extent")
    extent = {"type": kind}
    if kind in ("distance", "symmetric", "two_sides"):
        extent["distance"] = read_value(value, "distance", units, minimum=0)
    if kind == "symmetric":
        extent["full_length"] = bool_arg(value, "full_length", True)
    if kind == "two_sides":
        extent["side_two_distance"] = read_value(value, "side_two_distance", units, minimum=0)
    if kind == "through_all":
        extent["both_sides"] = bool_arg(value, "both_sides", False)
    if kind == "to_entity":
        to, to_body = value.get("to"), value.get("to_body")
        if (to is None) == (to_body is None):
            raise ValueError("a to_entity extent needs exactly one of 'to' and 'to_body'")
        extent["to"] = None if to is None else parse_ref(to, units, "extent.to")
        if extent["to"] is not None and extent["to"]["kind"] == "edge":
            raise ValueError("'extent.to' must be a face, vertex or construction plane, not an edge")
        extent["to_body"] = None if to_body is None else parse_body_ref(to_body, "'extent.to_body'")
        extent["to_offset"] = read_value(value, "to_offset", units, required=False)
        extent["chained"] = bool_arg(value, "chained", False)
    return extent


def _one_sided(extent):
    return extent["type"] in ("distance", "to_entity") or (extent["type"] == "through_all"
                                                           and not extent["both_sides"])


def _parse_side(arguments, extent):
    """(direction, side); required one-sided, optional on two_sides, refused where nothing goes one way."""
    direction, side = arguments.get("direction"), arguments.get("side")
    if direction is not None and side is not None:
        raise ValueError("give one of 'direction' and 'side', not both")
    if side is not None:
        _choice(arguments, "side", SIDES)
    if direction is not None:
        direction = _triple(direction, "direction")
        if unit(direction) is None:
            raise ValueError("'direction' has no length")
    named = direction is not None or side is not None
    if _one_sided(extent) and not named:
        raise ValueError(f"a one-sided {extent['type']} extent needs 'direction' or 'side'")
    if named and not _one_sided(extent) and extent["type"] != "two_sides":
        raise ValueError(f"a {extent['type']} extent runs both ways: drop 'direction' and 'side'")
    return direction, side


def _parse_taper(arguments, extent):
    taper = read_value(arguments, "taper_deg", "deg", expression_key="taper_expression", required=False)
    if taper is None or taper["number"] == 0:
        return None
    if extent["type"] in ("two_sides", "through_all"):
        raise ValueError(f"a taper cannot be used with a {extent['type']} extent")
    if taper["number"] is not None and abs(taper["number"]) >= 90:
        raise ValueError("'taper_deg' must be between -90 and 90")
    return taper


def _parse_targets(arguments, operation):
    targets = arguments.get("target_bodies")
    if operation == "new_body":
        if targets is not None:
            raise ValueError("a new_body extrude changes no body: drop 'target_bodies'")
        return []
    if not isinstance(targets, list) or not targets:
        raise ValueError(f"'{operation}' needs 'target_bodies': with none named, Fusion takes every body the "
                         "feature passes through, other components' bodies included. Name each body to change")
    return parse_body_refs(targets, "target_bodies")


def parse_request(arguments):
    """Validate without touching Fusion; raises ValueError."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN)
    units = _choice(arguments, "units", CM_PER_UNIT, "in")
    token, name = text_arg(arguments, "sketch_token"), text_arg(arguments, "sketch_name")
    if (token is None) == (name is None):
        raise ValueError("give exactly one of 'sketch_token' and 'sketch_name'")
    component = text_arg(arguments, "component")
    if component is not None and token is not None:
        raise ValueError("'component' only goes with 'sketch_name': a token already names its sketch")
    select, points = _parse_profile_select(arguments, units)
    operation = _choice(arguments, "operation", OPERATIONS)
    targets = _parse_targets(arguments, operation)
    extent = _parse_extent(arguments.get("extent"), units)
    direction, side = _parse_side(arguments, extent)
    body_name = text_arg(arguments, "body_name")
    if body_name is not None and operation != "new_body":
        raise ValueError("'body_name' names a new body: only a new_body extrude makes one")
    offset = read_value(arguments, "start_offset", units, required=False)
    return {"units": units, "sketch_token": token, "sketch_name": name, "component": component,
            "occurrence_path": text_arg(arguments, "occurrence_path"), "profile_select": select, "points": points,
            "operation": operation, "targets": targets, "extent": extent, "direction": direction, "side": side,
            "taper": _parse_taper(arguments, extent),
            "start_offset": None if offset is None or offset["number"] == 0 else offset,
            "body_name": body_name, "feature_name": text_arg(arguments, "feature_name"),
            "on_collateral": _choice(arguments, "on_collateral", ON_COLLATERAL, "undo"),
            "dry_run": bool_arg(arguments, "dry_run", False)}


# -- Fusion calls ---------------------------------------------------------------


class FusionApi:
    """The feature calls, apart from the logic so fakes can drive it."""

    def __init__(self, design):
        self._units = design.unitsManager

    def value(self, value, kind="cm"):
        return value_input(value, self._units, kind)

    def length_cm(self, value, units):
        """A checked ``read_value`` length in cm."""
        if value["number"] is not None:
            return value["number"] * CM_PER_UNIT[units]
        return self._units.evaluateExpression(value["text"], "cm")

    def point(self, xyz_cm):
        return adsk.core.Point3D.create(*xyz_cm)

    def _distance(self, value):
        return adsk.fusion.DistanceExtentDefinition.create(self.value(value))

    def _one_side(self, feature_input, definition, sign, taper):
        directions = adsk.fusion.ExtentDirections
        direction = directions.PositiveExtentDirection if sign > 0 else directions.NegativeExtentDirection
        if taper is None:
            return feature_input.setOneSideExtent(definition, direction)
        return feature_input.setOneSideExtent(definition, direction, self.value(taper, "deg"))

    def _symmetric(self, feature_input, extent, taper):
        distance, full = self.value(extent["distance"]), extent["full_length"]
        if taper is None:
            return feature_input.setSymmetricExtent(distance, full)
        return feature_input.setSymmetricExtent(distance, full, self.value(taper, "deg"))

    def _through_all(self, feature_input, extent, sign):
        through = adsk.fusion.ThroughAllExtentDefinition
        if extent["both_sides"]:
            return feature_input.setTwoSidesExtent(through.create(), through.create())
        definition = through.create()
        definition.isPositiveDirection = sign > 0
        return self._one_side(feature_input, definition, sign, None)

    def _to_entity(self, feature_input, extent, sign, taper, entity):
        chained, offset = extent["chained"], extent["to_offset"]
        create = adsk.fusion.ToEntityExtentDefinition.create
        definition = create(entity, chained) if offset is None else create(entity, chained, self.value(offset))
        return self._one_side(feature_input, definition, sign, taper)

    def set_extent(self, feature_input, extent, sign, taper=None, entity=None):
        """Apply *extent*; *sign* picks the side a one-sided extent runs. Returns Fusion's answer."""
        kind = extent["type"]
        if kind == "distance":
            return self._one_side(feature_input, self._distance(extent["distance"]), sign, taper)
        if kind == "symmetric":
            return self._symmetric(feature_input, extent, taper)
        if kind == "two_sides":
            sides = (self._distance(extent["distance"]), self._distance(extent["side_two_distance"]))
            return feature_input.setTwoSidesExtent(*(sides if sign > 0 else sides[::-1]))
        if kind == "through_all":
            return self._through_all(feature_input, extent, sign)
        return self._to_entity(feature_input, extent, sign, taper, entity)

    def profile_input(self, features, profiles, operation):
        source = profiles[0] if len(profiles) == 1 else self._collection(profiles)
        return features.createInput(source, getattr(adsk.fusion.FeatureOperations, OPERATIONS[operation]))

    def start_offset(self, feature_input, value):
        feature_input.startExtent = adsk.fusion.OffsetStartDefinition.create(self.value(value))

    @staticmethod
    def _collection(members):
        collection = adsk.core.ObjectCollection.create()
        for member in members:
            collection.add(member)
        return collection

    def join(self, component, target, tools):
        """Combine *tools* into *target*; returns ``add_feature``'s outcome."""
        features = component.features.combineFeatures
        combine_input = features.createInput(target, self._collection(tools))
        combine_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
        combine_input.isKeepToolBodies = False
        return add_feature(lambda: features.add(combine_input))


def _planes(body):
    """``(point, normal, bbox)`` of a body's flat faces, in world coordinates through its proxy."""
    planes = []
    for face in items(safe(body, "faces")):
        geometry, point = safe(face, "geometry"), safe(face, "pointOnFace")
        if SURFACE_TYPES.get(safe(geometry, "surfaceType")) == "plane" and point is not None:
            planes.append((xyz(point), xyz(geometry.normal), bbox(face)))
    return planes


def _ref_text(ref):
    return f"{ref['occurrence_path']}/{ref['body_name']}"


def _face_points(face):
    """Points on a face: its vertices, of which a full circle may have none, and one inside it."""
    inside = safe(face, "pointOnFace")
    return vertex_points(face) + ([xyz(inside)] if inside is not None else [])


# -- One call -------------------------------------------------------------------


class Job:
    """One extrude from arguments to envelope. *scene_factory* builds a ``FusionScene`` for a design."""

    def __init__(self, design, api, scene_factory, request):
        self.design, self.api, self.scene_factory, self.request = design, api, scene_factory, request
        self.units, self.factor = request["units"], CM_PER_UNIT[request["units"]]
        self.scene = scene_factory(design)
        self.operation, self.extent = request["operation"], request["extent"]
        self.warnings, self.resolved = [], []
        self.targets = []
        self.before, self.token = None, None
        self.stale = False
        self.rebuilt = False
        self.route = "participants" if self.operation == "join" else None

    # -- Opening: everything that can refuse before a feature is attempted -------

    def run(self):
        try:
            self._open()
            plan = self._plan()
        except Refused as exc:
            return refused(self.units, exc.problems, dry_run=self.request["dry_run"], resolved=self.resolved,
                           warnings=self.warnings, **exc.extra)
        if self.request["dry_run"]:
            return envelope("planned", self.units, dry_run=True, resolved=self.resolved, warnings=self.warnings,
                            profile=self._profile_row(), extrude=self._extrude_row(), plan=plan)
        return self._execute()

    def _open(self):
        problems, extra = [], {}
        targets = self._read_targets(problems, extra)
        found = self._read_sketch(problems)
        refusals, warnings = preflight(self.design, safe(self.design, "parentDocument"), targets)
        self.warnings += warnings
        if found is not None:
            problems += self._place(found, targets)
        if refusals or problems:
            raise Refused(refusals + problems, **extra)
        self.targets = targets

    def _read_targets(self, problems, extra):
        if not self.request["targets"]:
            return []
        targets, _, problem = resolve_targets(self.scene.entries(), self.request["targets"])
        if problem:
            problems.append(problem["error"])
            extra["candidates"] = problem.get("candidates", [])
            return []
        for target in targets:
            if not target["solid"]:
                problems.append(f"{label(target)} is a surface body: an extrude can only change a solid")
        return targets

    def _named_component(self, name):
        root = self.design.rootComponent
        if name is None or name == ROOT_PATH:
            return root
        return self.design.allComponents.itemByName(name)

    def _read_sketch(self, problems):
        """The sketch as found, proxy or native; None with a problem added."""
        token, name = self.request["sketch_token"], self.request["sketch_name"]
        if token is not None:
            found = [e for e in attempt(lambda: list(self.design.findEntityByToken(token)), [])
                     if safe(e, "objectType") == "adsk::fusion::Sketch"]
            if not found:
                problems.append("'sketch_token' does not match a sketch in the active design")
            return found[0] if found else None
        path = self.request["occurrence_path"]
        occurrence = None if path in (None, ROOT_PATH) else find_occurrence(self.design.rootComponent, path)
        if path not in (None, ROOT_PATH) and occurrence is None:
            problems.append(f"no occurrence at {path!r}")
            return None
        named = self.request["component"]
        component = safe(occurrence, "component") if occurrence is not None and named is None \
            else self._named_component(named)
        if component is None:
            problems.append(f"no component named {named!r}")
            return None
        sketch = component.sketches.itemByName(name)
        if sketch is None:
            problems.append(f"no sketch named {name!r} in {safe(component, 'name')!r}")
        return sketch

    def _instance(self, found, targets):
        """The occurrence a sub-component's sketch is placed by; second value a problem."""
        root, path = self.design.rootComponent, self.request["occurrence_path"]
        if path not in (None, ROOT_PATH):
            named = find_occurrence(root, path)
            if named is None:
                return None, f"no occurrence at {path!r}"
            if safe(named, "component") != self.component:
                return None, f"occurrence {path!r} is not an instance of {safe(self.component, 'name')!r}"
            return named, None
        context = safe(found, "assemblyContext")
        if context is not None:
            return context, None
        if targets:
            return find_occurrence(root, targets[0]["occurrence_path"]), None
        instances = [o for o in strict_items(safe(root, "allOccurrences")) if safe(o, "component") == self.component]
        if len(instances) == 1:
            return instances[0], None
        return None, (f"{safe(self.component, 'name')!r} has {len(instances)} instances: "
                      "pass 'occurrence_path' to say which one world coordinates go through")

    def _place(self, found, targets):
        """Set the sketch, its component and its occurrence; returns the problems."""
        self.sketch = native(found)
        self.component = self.sketch.parentComponent
        self.in_sub = self.component != self.design.rootComponent
        self.occurrence, self.frame = None, None
        if not self.in_sub:
            if self.request["occurrence_path"] not in (None, ROOT_PATH):
                return ["'occurrence_path' places a sub-component's sketch; this sketch is in root"]
            return []

        self.occurrence, problem = self._instance(found, targets)
        if problem:
            return [problem]
        self.frame = occurrence_frame(self.occurrence)
        problems = []
        if safe(self.occurrence, "isReferencedComponent"):
            problems.append(f"the sketch is in linked occurrence {safe(self.occurrence, 'fullPathName')!r}; "
                            "edit its source design instead")
        name, path = safe(self.component, "name"), safe(self.occurrence, "fullPathName")
        strangers = [label(t) for t in targets if t["component"] != name]
        if strangers:
            problems.append(f"the feature lives in {name!r}, the sketch's component, and cannot reach "
                            f"{strangers}; sketch in root to cut across components")
        elsewhere = [label(t) for t in targets if t["component"] == name and t["occurrence_path"] != path]
        if elsewhere:
            problems.append(f"name the targets through {path!r}, the instance the sketch is placed by: {elsewhere}")
        return problems

    # -- Planning ----------------------------------------------------------------

    def _world_point(self, point):
        return point if self.frame is None else from_local(point, self.frame)

    def _world_direction(self, vector):
        return vector if self.frame is None else direction_from_local(vector, self.frame)

    def _sketch_query(self, point):
        """A world cm point as 2D sketch cm."""
        local = point if self.frame is None else to_local(point, self.frame)
        mapped = xyz(self.sketch.modelToSketchSpace(self.api.point(local)))
        if abs(mapped[2]) > REF_TOLERANCE_CM:
            self._warn_once(f"point is {mapped[2] / self.factor:.6g} {self.units} off the sketch plane; "
                            "containment uses its projection")
        return mapped[:2]

    def _warn_once(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    def _select(self):
        """The chosen profiles, picked afresh: a profile wrapper does not outlive a delete."""
        self.rows = describe_profiles(self.sketch, self.factor)
        per_point = [describe_profiles(self.sketch, self.factor, self._sketch_query(p))
                     for p in self.request["points"]]
        self.chosen = pick_profiles(self.request["profile_select"], self.rows, per_point)
        return [self.sketch.profiles.item(i) for i in self.chosen]

    def _length(self, value, name, positive=True):
        try:
            self.api.value(value)
        except ValueError as exc:
            raise Refused([f"'{name}': {exc}"]) from exc
        length = self.api.length_cm(value, self.units)
        if positive and length <= 0:
            raise Refused([f"'{name}' evaluates to {length:.6g} cm; it must be positive. "
                           "'direction' or 'side' says which way"])
        return length

    def _read_lengths(self):
        """How far the extrude runs each way along its direction; None runs until something stops it."""
        kind, extent = self.extent["type"], self.extent
        self.forward, self.back = None, 0.0
        if kind == "distance":
            self.forward = self._length(extent["distance"], "distance")
        elif kind == "symmetric":
            distance = self._length(extent["distance"], "distance")
            self.forward = self.back = distance / 2 if extent["full_length"] else distance
        elif kind == "two_sides":
            self.forward = self._length(extent["distance"], "distance")
            self.back = self._length(extent["side_two_distance"], "side_two_distance")
        elif kind == "through_all" and extent["both_sides"]:
            self.back = None
        elif kind == "to_entity" and extent["to_offset"] is not None:
            self._length(extent["to_offset"], "to_offset", positive=False)
        offset = self.request["start_offset"]
        self.offset = 0.0 if offset is None else self._length(offset, "start_offset", positive=False)
        taper = self.request["taper"]
        if taper is not None:
            try:
                self.api.value(taper, "deg")
            except ValueError as exc:
                raise Refused([f"taper: {exc}"]) from exc

    def _resolve_to(self):
        """The entity a to_entity extent stops at; refuses rather than falling back to a distance."""
        self.to_entity = None
        if self.extent["type"] != "to_entity":
            return
        if self.extent["to_body"] is not None:
            targets, _, problem = resolve_targets(self.scene.entries(), [self.extent["to_body"]])
            if problem:
                raise Refused([f"extent.to_body: {problem['error']}"], candidates=problem.get("candidates", []))
            self.to_entity = targets[0]["_body"]
            return
        found = resolve(self.scene, self.extent["to"])
        if found.refusal:
            raise Refused([f"extent.to: {found.refusal['error']}"], refusal=found.refusal)
        if entity_kind(found.entity) == "edge":
            raise Refused(["extent.to names an edge: an extrude stops at a face, vertex or construction plane"])
        self.to_entity = found.entity
        self.warnings += [w for w in found.warnings if w not in self.warnings]
        row = resolved_row(found, "to")
        if row not in self.resolved:
            self.resolved.append(row)

    def _read_plane(self, profiles):
        plane = profiles[0].plane
        self.origin = self._world_point(xyz(plane.origin))
        self.normal = unit(self._world_direction(xyz(plane.normal)))
        direction, side = self.request["direction"], self.request["side"]
        self.sign = 1
        if side is not None:
            self.sign = SIDES[side]
        elif direction is not None:
            self.sign = extrude_sign(self.normal, direction)
            if self.sign is None:
                off = angle_between(self.normal, direction, signed=False)
                raise Refused([f"'direction' is {off:.3f} deg off the sketch normal "
                               f"{display_direction(self.normal)}, over the {DIRECTION_LIMIT_DEG} deg limit: "
                               "an extrude runs along the normal"])

    def _direction(self):
        return scaled(self.normal, self.sign)

    def _base(self):
        return self.sign * self.offset

    def _profile_points(self, profiles):
        """The world corners of each profile's box, which Fusion gives in sketch space."""
        corners = []
        for profile in profiles:
            box = profile.boundingBox
            low, high = xyz(box.minPoint), xyz(box.maxPoint)
            for x, y in ((low[0], low[1]), (low[0], high[1]), (high[0], low[1]), (high[0], high[1])):
                corners.append(self._world_point(xyz(self.sketch.sketchToModelSpace(
                    self.api.point((x, y, low[2]))))))
        return corners

    def _swept(self, profiles):
        direction = self._direction()
        boxes = [e["bbox"] for e in self.scene.entries() if e["bbox"] is not None]
        corners = [c for box in boxes for c in box_corners(box)]
        reach = span_along(corners, self.origin, direction) if corners else (0.0, 0.0)
        low, high = sweep_span(self._base(), self.forward, self.back, reach)
        return swept_box(self._profile_points(profiles), direction, low, high)

    def _abuts(self):
        """Only a blind cut ends on a plane of its own choosing."""
        return self.operation == "cut" and self.extent["type"] == "distance"

    def _check_abutment(self, box):
        """Refuse a blind cut ending a sliver from a parallel face of a target."""
        if not self._abuts():
            return
        end = self._base() + self.forward
        hits = [hit for target in self.targets
                for hit in abutments(_planes(target["_body"]), self.origin, self._direction(), end, box)]
        if hits:
            rows = [{"point": display_point(h["point"], self.units), "gap": to_units(h["gap"], self.units)}
                    for h in hits]
            raise Refused([f"the cut ends {rows[0]['gap']} {self.units} from a parallel face of its target: "
                           f"{ABUTMENT_ADVICE}"], abutment=rows)

    def _input(self, operation, sign, profiles):
        """A filled-in input, without participants; raises ``Refused``."""
        features = self.component.features.extrudeFeatures
        try:
            feature_input = self.api.profile_input(features, profiles, operation)
            done = self.api.set_extent(feature_input, self.extent, sign, self.request["taper"], self.to_entity)
            if done is False:
                raise Refused([f"Fusion would not set the {self.extent['type']} extent"])
            if self.request["start_offset"] is not None:
                self.api.start_offset(feature_input, self.request["start_offset"])
            if self.occurrence is not None:
                feature_input.creationOccurrence = self.occurrence
        except RuntimeError as exc:
            raise Refused([str(exc)]) from exc
        return feature_input, features

    def _participants(self):
        """Bodies as the feature's component sees them: root reaches into proxies, a sub-component is native."""
        bodies = [t["_body"] for t in self.targets]
        return [native(b) for b in bodies] if self.in_sub else bodies

    def _set_participants(self, feature_input):
        """Name the targets; Fusion refusing on a cut is the end of it, never an implicit cut."""
        try:
            feature_input.participantBodies = self._participants()
        except RuntimeError as exc:
            if self.operation != "join":
                raise Refused([f"Fusion would not take the target bodies: {exc}"]) from exc
            self.route = "new_body_combine"
            self.warnings.append(f"Fusion would not take participantBodies on a join ({exc}); "
                                 "joining by a new body and a Combine")

    def _in_path(self, feature_input, box):
        """(entries the extrude reaches, basis). The getter first; it may give nothing (EP-3)."""
        listed = attempt(lambda: list(feature_input.participantBodies or []), [])
        entries = self.scene.entries()
        if listed:
            return [e for e in entries if any(self._is_body(b, e) for b in listed)], "participants"
        return [e for e in entries if e["solid"] and bbox_overlap(e["bbox"], box)], "bbox"

    @staticmethod
    def _is_body(body, entry):
        return same_body(body, entry) or same_entity(native(entry["_body"]), body)

    def _not_targeted(self, hits):
        """Hits that are neither a target nor another instance of one."""
        natives = [native(t["_body"]) for t in self.targets]
        return [e for e in hits if not any(same_entity(native(e["_body"]), n) for n in natives)]

    def _plan(self):
        profiles = self._select()
        self._read_lengths()
        self._resolve_to()
        self._read_plane(profiles)
        box = self._swept(profiles)
        self._check_abutment(box)
        feature_input, _ = self._input(self.operation, self.sign, profiles)
        hits, basis = self._in_path(feature_input, box)
        if self.operation != "new_body":
            self._set_participants(feature_input)
        self.in_path = [body_ref(e) for e in self._not_targeted(hits)]
        reached = [e["key"] for e in hits]
        missed = [label(t) for t in self.targets if t["key"] not in reached]
        if missed:
            self.warnings.append(f"these targets do not look to be in the extrude's path: {missed}")
        if self.in_path and self.operation != "new_body":
            self.warnings.append("the extrude passes through bodies not named as targets, left alone: "
                                 f"{[_ref_text(r) for r in self.in_path]}")
        return {"operation": self.operation, "would_intersect": [body_ref(e) for e in hits],
                "in_path_not_targeted": self.in_path, "expected_volume": self._expected_volume(),
                "side": self._side_used(), "abutment": "clear" if self._abuts() else "not_applicable",
                "plan_basis": basis, "join_route": self.route}

    # -- Report pieces -----------------------------------------------------------

    def _chosen_rows(self):
        return [row for row in self.rows if row["index"] in self.chosen]

    def _profile_row(self):
        rows = self._chosen_rows()
        return {"count": len(rows), "area": round(sum(r["area"] for r in rows), 5),
                "loop_count": sum(r["loop_count"] for r in rows),
                "plane_normal_world": display_direction(self.normal)}

    def _requested_cm(self):
        return None if self.forward is None or self.back is None else self.forward + self.back

    def _expected_volume(self):
        """Area times depth. A taper makes this no longer the volume, so there is none to give."""
        requested = self._requested_cm()
        if requested is None or self.request["taper"] is not None:
            return None
        return round(sum(r["area"] for r in self._chosen_rows()) * requested / self.factor, 5)

    def _side_used(self):
        if self.extent["type"] == "symmetric":
            return "symmetric"
        if self.extent["type"] == "through_all" and self.extent["both_sides"]:
            return "both"
        return "positive" if self.sign > 0 else "negative"

    def _extrude_row(self, measure=None, volume_check="not_applicable"):
        measured = None if measure is None else measure["measured"]
        requested = self._requested_cm()
        return {"side_used": self._side_used(), "direction_world": display_direction(self._direction()),
                "requested_depth": to_units(requested, self.units),
                "measured_depth": to_units(measured, self.units),
                "expected_volume": self._expected_volume(), "volume_check": volume_check,
                "rebuilt_other_side": self.rebuilt}

    # -- Reading what a feature made ---------------------------------------------

    def _owner(self, body, entries):
        """The entry *body* belongs to. A native body sits in every instance, so the named one comes first."""
        matches = [e for e in entries if self._is_body(body, e)]
        wanted = {t["occurrence_path"] for t in self.targets}
        if self.occurrence is not None:
            wanted.add(safe(self.occurrence, "fullPathName"))
        return next((e for e in matches if e["occurrence_path"] in wanted), matches[0] if matches else None)

    def _placed(self, faces, entries):
        """``(face in world coordinates, its entry)`` for each face."""
        placed = []
        for face in faces:
            entry = self._owner(safe(face, "body"), entries)
            placed.append((in_context(face, entry), entry))
        return placed

    def _measure(self, feature, operation, sign):
        """``{"measured", "side"}``: a new body by its span, anything else by its end cap."""
        none = {"measured": None, "side": "ok"}
        if operation == "new_body":
            faces = [f for b in items(safe(feature, "bodies")) for f in items(safe(b, "faces"))]
        elif self.back == 0:
            faces = items(safe(feature, "endFaces"))
        else:
            return none
        placed = self._placed(faces, self.scene_factory(self.design).entries())
        points = [p for face, _ in placed for p in _face_points(face)]
        if not points:
            return none
        base = sign * self.offset
        low, high = span_along(points, self.origin, scaled(self.normal, sign))
        measured = high - low if operation == "new_body" else abs(high - base)
        return {"measured": measured, "side": side_verdict(low, high, base) if self.back == 0 else "ok"}

    def _face_refs(self, feature):
        entries = self.scene_factory(self.design).entries()
        return {key: [make_ref(face, "face", self.units, entry)
                      for face, entry in self._placed(items(safe(feature, attr)), entries)]
                for key, attr in _FACE_SETS}

    def _face_points_by_set(self, feature):
        entries = self.scene_factory(self.design).entries()
        points = {}
        for key, attr in _FACE_SETS:
            placed = self._placed(items(safe(feature, attr)), entries)
            points[key] = [xyz(face.pointOnFace) for face, _ in placed if safe(face, "pointOnFace") is not None]
        return points

    def _refs_at(self, points):
        """Faces of the targets still at *points*, after a Combine replaced the extrude's own."""
        scene = self.scene_factory(self.design)
        entries = scene.entries()
        keys = {t["key"] for t in self.targets}
        refs = {}
        for key, spots in points.items():
            hits = unique_entities(hit for spot in spots for hit in scene.at_point(spot, "face"))
            refs[key] = [make_ref(face, "face", self.units, entry) for face, entry in self._placed(hits, entries)
                         if entry is not None and entry["key"] in keys]
        return refs

    # -- Building ----------------------------------------------------------------

    def _refresh(self):
        """Wrappers do not outlive a delete, so targets and refs are looked up again."""
        self.scene = self.scene_factory(self.design)
        if self.request["targets"]:
            targets, _, problem = resolve_targets(self.scene.entries(), self.request["targets"])
            if problem:
                raise Refused([problem["error"]])
            self.targets = targets
        self._resolve_to()

    def _add(self, operation, sign):
        """One attempt; ``add_feature``'s outcome, with a refusal on the way to it counted as refused."""
        try:
            if self.stale:
                self._refresh()
            feature_input, features = self._input(operation, sign, self._select())
            if operation != "new_body":
                self._set_participants(feature_input)
                if operation == "join" and self.route != "participants":
                    raise Refused(["Fusion stopped taking the target bodies"])  # An unnamed join is the bug.
        except Refused as exc:
            return {"feature": None, "status": "refused", "refusal": str(exc), "health": None, "warnings": []}
        return add_feature(lambda: features.add(feature_input))

    def _build(self, operation):
        """Add, measure, and rebuild once on the other side if it came out wrong."""
        built = self._add(operation, self.sign)
        if built["status"] != "built":
            return built
        built["measure"] = self._measure(built["feature"], operation, self.sign)
        if built["measure"]["side"] != "wrong_side":
            return built

        undone = undo(self.design, built["feature"], self.before)
        self.stale = True
        if not undone["undone"]:
            self.warnings += undone["warnings"]
            return {**built, "feature": None, "status": "refused",
                    "refusal": "the extrude came out on the wrong side and could not be taken back"}
        was = self._side_used()
        self.sign = -self.sign
        self.rebuilt = True
        self.warnings.append(f"the {was} side built behind the start plane; deleted and rebuilt on the "
                             f"{self._side_used()} side")
        again = self._add(operation, self.sign)
        if again["status"] == "built":
            again["measure"] = self._measure(again["feature"], operation, self.sign)
        return again

    def _join_by_combine(self):
        """New body plus a Combine that names exactly the targets, for a join Fusion will not limit."""
        self.route = "new_body_combine"
        strangers = [label(t) for t in self.targets
                     if t["occurrence_path"] != ROOT_PATH and not self.in_sub]
        if strangers:
            return {"feature": None, "status": "refused", "health": None, "warnings": [],
                    "refusal": f"a Combine cannot cross components, so these cannot be joined from root: {strangers}"}
        built = self._build("new_body")
        if built["status"] != "built":
            return built
        bodies = list(items(safe(built["feature"], "bodies")))
        built["points"] = self._face_points_by_set(built["feature"])
        if self.stale:
            self._refresh()
        target, *others = self._participants()
        joined = self.api.join(self.component, target, bodies + others)
        if joined["status"] != "built":
            taken = undo(self.design, built["feature"], self.before)
            self.stale = True
            self.warnings += joined["warnings"] + taken["warnings"]
            return {**joined, "refusal": f"the Combine failed: {joined['refusal']}"}
        built["combine"] = joined["feature"]
        built["warnings"] = built["warnings"] + joined["warnings"]
        return built

    def _execute(self):
        self.before, self.token = baseline(self.design)
        built = {}
        try:
            built = self._join_by_combine() if self.route == "new_body_combine" else self._build(self.operation)
            return self._conclude(built)
        except Refused as exc:
            return refused(self.units, exc.problems, baseline_token=self.token, warnings=self.warnings, **exc.extra)
        except Exception as exc:
            if built.get("feature") is None:
                raise
            log(f"[MCP] extrude_profile post-check failed: {exc}")
            return envelope("suspect", self.units, feature=feature_row(built["feature"], built["health"]),
                            baseline_token=self.token, resolved=self.resolved,
                            warnings=self.warnings + [f"the feature was added but its check failed: {exc}"])

    def _apply_names(self, built):
        feature, request = built["feature"], self.request
        try:
            if request["feature_name"]:
                feature.name = request["feature_name"]
            if request["body_name"]:
                for number, body in enumerate(items(safe(feature, "bodies"))):
                    body.name = request["body_name"] if number == 0 else f"{request['body_name']} {number + 1}"
        except RuntimeError as exc:
            self.warnings.append(f"a name could not be set: {exc}")

    def _take_back(self, built):
        combine = built.get("combine")
        if combine is not None and attempt(combine.deleteMe, False) is False:
            return {"undone": False, "warnings": ["the Combine could not be deleted"]}
        return undo(self.design, built["feature"], self.before)

    def _undone(self, built, row, bodies, collateral):
        """A feature that reached outside its targets: take it back, and redo a join another way."""
        names = ", ".join(collateral)
        taken = self._take_back(built)
        self.stale = True
        self.warnings += taken["warnings"]
        if not taken["undone"]:
            return refused(self.units, [f"the extrude changed bodies outside its targets ({names}) and "
                                        "could not be fully taken back"],
                           feature=row, bodies=bodies, baseline_token=self.token, resolved=self.resolved,
                           warnings=self.warnings, collateral=collateral)
        if self.operation == "join" and self.route == "participants":
            self.warnings.append(f"Fusion ignored participantBodies on this join ({names} changed); "
                                 "undone, and joining by a new body and a Combine")
            return self._conclude(self._join_by_combine())
        self.warnings.append(f"undone: the extrude changed bodies outside its targets: {names}")
        return envelope("undone", self.units, feature=row, bodies=bodies, baseline_token=self.token,
                        resolved=self.resolved, warnings=self.warnings, collateral=collateral,
                        profile=self._profile_row(), extrude=self._extrude_row(built.get("measure")))

    def _created_volume(self, bodies):
        """What a new body came to, in this instance only: each instance lists its own copy."""
        wanted = ROOT_PATH if self.occurrence is None else safe(self.occurrence, "fullPathName")
        rows = [row for row in bodies["created"] if row["ref"]["occurrence_path"] == wanted]
        return sum(row["volume"] for row in rows) if rows else None

    def _doubts(self, built, bodies, kept, not_changed, volume_check):
        """Why a feature that stands should not be trusted."""
        doubts = []
        if kept:
            doubts.append(f"bodies outside the targets changed and were kept: {kept}")
        rows = [_ref_text(r["ref"]) for r in bodies["outside_target"]]
        if rows and not kept:
            doubts.append(f"bodies outside the targets changed: {rows}")
        if not_changed:
            doubts.append(f"targets unchanged: {not_changed}")
        measure = built.get("measure") or {"measured": None, "side": "ok"}
        if measure["side"] == "wrong_side":
            doubts.append("the extrude is still behind its start plane after a rebuild")
        requested = self._requested_cm()
        if (requested is not None and measure["measured"] is not None
                and abs(measure["measured"] - requested) > DEPTH_TOLERANCE_CM):
            doubts.append(f"measured depth {to_units(measure['measured'], self.units)} {self.units}, "
                          f"asked for {to_units(requested, self.units)}")
        if volume_check == "mismatch":
            doubts.append(f"volume differs from the expected {self._expected_volume()}")
        return doubts

    def _conclude(self, built):
        if built["status"] != "built":
            return refused(self.units, [built["refusal"]], baseline_token=self.token, resolved=self.resolved,
                           warnings=self.warnings + built["warnings"], profile=self._profile_row(),
                           extrude=self._extrude_row(), join_route=self.route)
        self.warnings += built["warnings"]
        self._apply_names(built)
        feature = built["feature"]
        row = feature_row(feature, built["health"])

        before, after = self.before, snapshot(self.design)
        keys = [t["key"] for t in self.targets]
        bodies = body_changes(before, after, keys, self.units)
        collateral = sorted(set(lost_outside(before, after, keys)) | set(grew_outside(before, after, keys)))
        if collateral and self.request["on_collateral"] == "undo":
            return self._undone(built, row, bodies, collateral)

        landed = safe(safe(feature, "parentComponent"), "name")
        if landed != safe(self.component, "name"):
            self.warnings.append(f"the feature landed in {landed!r}, not the sketch's component "
                                 f"{safe(self.component, 'name')!r}")
        instances = [r["ref"]["occurrence_path"] for r in bodies["touched"] if r["role"] == "other_instance"]
        if instances:
            self.warnings.append(f"other instances of the component changed too: {sorted(set(instances))}")

        not_changed = unchanged(before, after, keys)
        volume_check = "not_applicable"
        if self.operation == "new_body":
            volume_check = volume_verdict(self._expected_volume(), self._created_volume(bodies))
        doubts = self._doubts(built, bodies, collateral, not_changed, volume_check)
        self.warnings += doubts
        faces = self._refs_at(built["points"]) if built.get("combine") is not None else self._face_refs(feature)
        extra = {"join_route": self.route} if self.operation == "join" else {}
        if built.get("combine") is not None:
            extra["combine_feature"] = feature_row(built["combine"])
        return envelope("suspect" if doubts else "built", self.units, feature=row, bodies=bodies,
                        baseline_token=self.token, resolved=self.resolved, warnings=self.warnings,
                        profile=self._profile_row(), extrude=self._extrude_row(built.get("measure"), volume_check),
                        faces=faces, targets_unchanged=not_changed, in_path_not_targeted=self.in_path, **extra)


# -- Handler --------------------------------------------------------------------


def _report_units(arguments):
    units = arguments.get("units") if isinstance(arguments, dict) else None
    return units if isinstance(units, str) and units in CM_PER_UNIT else "in"


def extrude_profile(arguments):
    """Extrude a sketch profile in the active design and report what every body did."""
    units = _report_units(arguments)
    try:
        request = parse_request(arguments)
    except ValueError as exc:
        return write_result(refused(units, [str(exc)]))
    try:
        design = adsk.fusion.Design.cast(get_app().activeProduct)
        if design is None:
            return write_result(refused(units, ["The active product is not a Fusion design."],
                                        dry_run=request["dry_run"]))
        payload = Job(design, FusionApi(design), FusionScene, request).run()
        log(f"[MCP] extrude_profile {request['operation']}: {payload['status']}")
        return write_result(payload)
    except Exception as exc:
        log(f"[MCP] extrude_profile failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
