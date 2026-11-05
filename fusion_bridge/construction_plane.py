"""Make a plane at a stated place and report where it really is: construction_plane.

Each mode turns its arguments into a prediction: an ``origin`` and a ``normal``
the plane must match, and for ``offset`` and ``angle`` a ``mirror``, the plane the
same value gives with its sign the other way. Which way Fusion takes a face's
offset or a ``setByAngle`` is not documented. So nothing assumes it: the built
plane is measured against both, and if it matches the mirror it is deleted and
rebuilt once with the value negated. ``angle_deg`` then always means right-handed
about the axis given, and ``toward`` always wins.

The normal's own sign stays Fusion's choice. A plane matches its prediction
either way up, ``sign`` says which it got, and consumers read ``normal``.

A plane through coordinates needs sketch points: ``feature_ops.anchor_points``.
They are named, listed and hidden, and never deleted, since the plane and
everything sketched on it hang from them. A rebuild deletes the plane only.

All points in and out are world cm internally; a plane in a sub-component is
read through its occurrence. Everything above ``FusionPlanes`` takes that one
duck-typed collaborator, so fakes can drive it. A plane cannot change a body, so
one baseline is stored for ``check_solids`` and no diff is taken.
"""

import math
import re
import traceback

import adsk.core
import adsk.fusion

from .common import feature_ops as fo
from .common.api import attempt, present, safe, strict_items
from .common.args import bool_arg, error_pair, reject_unknown, require_object, text_arg, vector_arg
from .common.bodies import (
    ROOT_PATH,
    SURFACE_TYPES,
    bbox,
    find_occurrence,
    native,
    occurrence_frame,
    outward_normal,
)
from .common.entity_refs import (
    CURVE_TYPES,
    FusionScene,
    bbox_distance,
    parse_ref,
    resolve,
    resolved_row,
)
from .common.geometry import (
    CM_PER_UNIT,
    UNIT_FACTORS,
    along,
    collinear,
    cross,
    display_direction,
    display_point,
    dot,
    perpendicular_pair,
    rotate_about,
    same_plane,
    scaled,
    sub,
    to_local,
    unit,
    xyz,
)
from .common.results import error_result
from .dispatch import get_app, log

MODES = ("offset", "three_point", "angle", "point_normal")
ROOT_PLANES = {"xy": "xYConstructionPlane", "xz": "xZConstructionPlane", "yz": "yZConstructionPlane"}
ROOT_AXES = {"x": "xConstructionAxis", "y": "yConstructionAxis", "z": "zConstructionAxis"}
_PARENT_KEYS = {"from", "from_ref", "from_plane_token"}
_MODE_KEYS = {
    "offset": _PARENT_KEYS | {"offset", "offset_expression", "toward"},
    "three_point": {"points"},
    "angle": _PARENT_KEYS | {"axis", "angle_deg", "angle_expression"},
    "point_normal": {"point", "normal"},
}
_COMMON_KEYS = {"mode", "name", "component", "occurrence_path", "reuse_existing", "show_anchors", "units", "dry_run"}
_ALL_MODE_KEYS = set().union(*_MODE_KEYS.values())

# Three points a micron apart describe a plane in theory and a numerically hopeless
# one in practice, so point_normal spreads them over the part. 1 in at the least.
REACH_FLOOR_CM = 2.54
# Below this, an axis or a side is too nearly along the plane to mean anything.
SQUARE_SINE = 1e-6
_ANCHOR_PLANE = re.compile(r".*_anchors_\d+_plane")
_PLANE_TYPE = "ConstructionPlane"


# -- Argument validation --------------------------------------------------------


def _direction(value, where):
    direction = unit(vector_arg(value, where))
    if direction is None:
        raise ValueError(f"'{where}' has no direction: it is a zero vector")
    return direction


def _point(value, where, units):
    return tuple(c * CM_PER_UNIT[units] for c in vector_arg(value, where))


def _ref_of_kind(value, kind, where, units):
    ref = parse_ref({"kind": kind, **value} if isinstance(value, dict) else value, units, where)
    if ref["kind"] != kind:
        raise ValueError(f"'{where}' must have kind {kind!r}, not {ref['kind']!r}")
    return ref


def _parent(arguments, units):
    given = sorted(key for key in _PARENT_KEYS if arguments.get(key) is not None)
    if len(given) != 1:
        raise ValueError(f"{arguments['mode']} needs exactly one of {sorted(_PARENT_KEYS)}, got {given}")
    key = given[0]
    if key == "from":
        if arguments[key] not in ROOT_PLANES:
            raise ValueError(f"'from' must be one of {sorted(ROOT_PLANES)}, got {arguments[key]!r}")
        return {"root_plane": arguments[key]}
    if key == "from_ref":
        return {"ref": _ref_of_kind(arguments[key], "face", "from_ref", units)}
    return {"token": text_arg(arguments, key)}


def _point_source(value, units):
    if not isinstance(value, dict) or set(value) - {"point", "ref", "sketch_point_token"} or len(value) != 1:
        raise ValueError("each 'points' entry must have exactly one of point, ref and sketch_point_token")
    if "point" in value:
        return {"point": _point(value["point"], "points.point", units)}
    if "ref" in value:
        return {"ref": _ref_of_kind(value["ref"], "vertex", "points.ref", units)}
    return {"sketch_point_token": text_arg(value, "sketch_point_token")}


def _axis(value, units):
    if not isinstance(value, dict) or set(value) - {"axis", "edge"} or len(value) != 1:
        raise ValueError("'axis' must have exactly one of axis and edge")
    if "edge" in value:
        return {"edge": _ref_of_kind(value["edge"], "edge", "axis.edge", units)}
    if value["axis"] not in ROOT_AXES:
        raise ValueError(f"'axis.axis' must be one of {sorted(ROOT_AXES)}, got {value['axis']!r}")
    return {"root_axis": value["axis"]}


@error_pair
def validate_args(arguments):
    """Return (options, error_text). Points come back in cm, directions as unit vectors."""
    require_object(arguments)
    mode = arguments.get("mode")
    if mode not in MODES:
        raise ValueError(f"'mode' must be one of {list(MODES)}, got {mode!r}")
    reject_unknown(arguments, _COMMON_KEYS | _ALL_MODE_KEYS)
    for key in sorted(_ALL_MODE_KEYS - _MODE_KEYS[mode]):
        if key in arguments:
            raise ValueError(f"'{key}' does not apply to mode {mode}")
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    component = text_arg(arguments, "component") or ROOT_PATH
    occurrence_path = text_arg(arguments, "occurrence_path")
    if occurrence_path == ROOT_PATH:
        occurrence_path = None
    if component != ROOT_PATH and occurrence_path is None:
        raise ValueError(f"component {component!r} needs 'occurrence_path': world coordinates pass through one "
                         "instance of it, and each instance sits somewhere else")
    opts = {"mode": mode, "name": text_arg(arguments, "name", required=True).strip(), "units": units,
            "component": component, "occurrence_path": occurrence_path,
            "reuse_existing": bool_arg(arguments, "reuse_existing", True),
            "show_anchors": bool_arg(arguments, "show_anchors", False),
            "dry_run": bool_arg(arguments, "dry_run", False)}
    if mode == "offset":
        opts["parent"] = _parent(arguments, units)
        opts["value"] = fo.read_value(arguments, "offset", units)
        toward = arguments.get("toward")
        opts["toward"] = None if toward is None else _direction(toward, "toward")
    elif mode == "angle":
        opts["parent"] = _parent(arguments, units)
        opts["value"] = fo.read_value(arguments, "angle_deg", "deg", expression_key="angle_expression")
        if "axis" not in arguments:
            raise ValueError("'axis' is required")
        opts["axis"] = _axis(arguments["axis"], units)
    elif mode == "three_point":
        points = arguments.get("points")
        if not isinstance(points, list) or len(points) != 3:
            raise ValueError("'points' must be an array of exactly three entries")
        opts["points"] = [_point_source(point, units) for point in points]
    else:
        for key in ("point", "normal"):
            if key not in arguments:
                raise ValueError(f"'{key}' is required")
        opts["point"] = _point(arguments["point"], "point", units)
        opts["normal"] = _direction(arguments["normal"], "normal")
    return opts


# -- Pure helpers ---------------------------------------------------------------


def negated(value):
    """A ``read_value`` result the other way round; one form serves numbers and expressions."""
    return {"text": f"-({value['text']})", "number": None if value["number"] is None else -value["number"]}


def reach_for(point, boxes, floor=REACH_FLOOR_CM):
    """How far apart to set anchors: the diagonal of the box nearest *point*, *floor* at least."""
    if not boxes:
        return floor
    low, high = min(boxes, key=lambda box: bbox_distance(box, point))
    return max(math.dist(low, high), floor)


def judge(plan, built):
    """``ok``, ``flip`` (it matches the mirror, so rebuild) or ``off``."""
    if same_plane(built["origin"], built["normal"], plan["origin"], plan["normal"]):
        return "ok"
    mirror = plan["mirror"]
    if mirror and same_plane(built["origin"], built["normal"], *mirror):
        return "flip"
    return "off"


def find_existing(planes, plan):
    """The first plane coincident with the prediction. A plane holding up an anchor is not one to build on."""
    return next((plane for plane in planes
                 if not _ANCHOR_PLANE.fullmatch(plane["name"] or "")
                 and same_plane(plane["origin"], plane["normal"], plan["origin"], plan["normal"])), None)


def sign_row(asked, built):
    return {"asked_normal": display_direction(asked), "built_normal": display_direction(built),
            "flipped_vs_asked": dot(asked, built) < 0}


# -- Planning: each mode's prediction -------------------------------------------


def _new_plan(kind, entities, value=None, unit_kind=None):
    return {"parent": None, "origin": None, "normal": None, "mirror": None, "to_anchor": [], "resolved": [],
            "warnings": [], "definition": {"kind": kind, "entities": entities, "value": value, "unit": unit_kind}}


def _resolve(layer, ref, role, plan):
    """The entity a ref names now; a refusal raises ValueError."""
    found = resolve(layer.scene, ref)
    if found.refusal:
        raise ValueError(f"{role}: {found.refusal['error']}")
    plan["resolved"].append(resolved_row(found, role))
    plan["warnings"] += found.warnings
    return found.entity


def _read_parent(opts, layer, plan):
    """``{"entity", "point", "normal", "row"}`` of the plane or face measured from."""
    source = opts["parent"]
    if "root_plane" in source:
        return layer.root_plane(source["root_plane"])
    if "token" in source:
        return layer.plane_by_token(source["token"])
    face = _resolve(layer, source["ref"], "from_ref", plan)
    parent = layer.face_parent(face)
    if parent is None:
        raise ValueError("'from_ref' is not a planar face")
    return parent


def plan_offset(opts, layer):
    plan = _new_plan("offset", [], opts["value"], "cm")
    parent = _read_parent(opts, layer, plan)
    side = parent["normal"]
    if opts["toward"] is not None:
        along_normal = dot(opts["toward"], side)
        if abs(along_normal) <= SQUARE_SINE:
            raise ValueError("'toward' lies in the parent plane, so it picks neither side")
        side = side if along_normal > 0 else scaled(side, -1.0)
    distance = layer.evaluate(opts["value"], "cm")
    plan["definition"]["entities"] = [parent["entity"]]
    plan.update(parent=parent["row"], origin=along(parent["point"], side, distance), normal=parent["normal"],
                mirror=(along(parent["point"], side, -distance), parent["normal"]))
    return plan


def plan_angle(opts, layer):
    plan = _new_plan("angle", [], opts["value"], "deg")
    parent = _read_parent(opts, layer, plan)
    source = opts["axis"]
    if "root_axis" in source:
        axis = layer.root_axis(source["root_axis"])
    else:
        edge = _resolve(layer, source["edge"], "axis.edge", plan)
        axis = layer.edge_line(edge)
        if axis is None:
            raise ValueError("'axis.edge' is not a straight edge")
    direction = unit(axis["direction"])
    if direction is None or abs(dot(direction, parent["normal"])) > SQUARE_SINE:
        raise ValueError("the axis must run parallel to the plane the angle is measured from")
    angle = layer.evaluate(opts["value"], "deg")
    plan["definition"]["entities"] = [axis["entity"], parent["entity"]]
    plan.update(parent=parent["row"], origin=axis["point"], normal=rotate_about(parent["normal"], direction, angle),
                mirror=(axis["point"], rotate_about(parent["normal"], direction, -angle)),
                axis_direction=direction)
    return plan


def _anchored(plan, positions, sources):
    """Fill in a through-points plan: *sources* holds an entity, or None where an anchor goes."""
    plan["definition"]["entities"] = sources
    plan["to_anchor"] = [(i, position) for i, (position, source) in enumerate(zip(positions, sources))
                         if source is None]
    plan.update(origin=positions[0], normal=cross(sub(positions[1], positions[0]), sub(positions[2], positions[0])),
                parent={"kind": "anchors", "name": None, "token": None, "visible": None})
    return plan


def plan_three_point(opts, layer):
    plan = _new_plan("three_points", [])
    positions, sources = [], []
    for source in opts["points"]:
        if "point" in source:
            positions.append(source["point"])
            sources.append(None)
        elif "ref" in source:
            vertex = _resolve(layer, source["ref"], "points.ref", plan)
            positions.append(layer.vertex_point(vertex))
            sources.append(vertex)
        else:
            entity, position = layer.sketch_point(source["sketch_point_token"])
            positions.append(position)
            sources.append(entity)
    if collinear(*positions):
        raise ValueError("the three points are collinear or coincident, so they define no plane")
    return _anchored(plan, positions, sources)


def plan_point_normal(opts, layer):
    point, normal = opts["point"], opts["normal"]
    across, up = perpendicular_pair(normal)
    reach = reach_for(point, layer.body_boxes())
    positions = [point, along(point, across, reach), along(point, up, reach)]
    plan = _anchored(_new_plan("three_points", []), positions, [None, None, None])
    plan["normal"] = normal
    return plan


PLANNERS = {"offset": plan_offset, "three_point": plan_three_point, "angle": plan_angle,
            "point_normal": plan_point_normal}


# -- Results --------------------------------------------------------------------


def _plane_row(built, name, opts, layer):
    units = opts["units"]
    return {"token": built["token"], "name": name, "component": layer.component_name,
            "origin": display_point(built["origin"], units), "normal": display_direction(built["normal"]),
            "u": display_direction(built["u"]), "v": display_direction(built["v"])}


def _anchor_rows(plan, anchors, units):
    return [{**row, "point": display_point(position, units)}
            for row, (_, position) in zip(anchors["rows"], plan["to_anchor"])]


def _plan_row(plan, opts, layer):
    units = opts["units"]
    row = {"mode": opts["mode"], "name": opts["name"], "component": layer.component_name,
           "origin": display_point(plan["origin"], units), "normal": display_direction(plan["normal"]),
           "normal_sign": "as asked; Fusion picks the built sign",
           "anchors": [{"point": display_point(position, units), "visible": opts["show_anchors"]}
                       for _, position in plan["to_anchor"]]}
    if "axis_direction" in plan:
        row["axis_direction"] = display_direction(plan["axis_direction"])
    return row


def _common(plan):
    extra = {"parent": plan["parent"], "resolved": plan["resolved"]}
    if "axis_direction" in plan:
        extra["axis_direction"] = display_direction(plan["axis_direction"])
    return extra


def _reused(existing, plan, opts, layer, warnings):
    if existing["name"] != opts["name"]:
        warnings = warnings + [f"reused the coincident plane {existing['name']!r}; it keeps its name"]
    return fo.envelope("built", opts["units"], warnings=warnings, reused=True, rebuilt=False, anchors=[],
                       plane=_plane_row(existing, existing["name"], opts, layer),
                       sign=sign_row(plan["normal"], existing["normal"]), **_common(plan))


def _place_anchors(plan, opts, layer):
    """Put anchors where the definition has gaps. Returns the anchor record, or None if none were needed."""
    if not plan["to_anchor"]:
        return None
    anchors = layer.anchors([position for _, position in plan["to_anchor"]], opts["name"], opts["show_anchors"])
    entities = plan["definition"]["entities"]
    for (index, _), point in zip(plan["to_anchor"], anchors["points"]):
        entities[index] = point
    if anchors["unused"]:
        plan["warnings"].append(f"one sketch would not hold the anchors; these anchor nothing and were left, "
                                f"hidden: {anchors['unused']}")
    if plan["parent"]["kind"] == "anchors":
        plan["parent"] = {**plan["parent"], "name": anchors["rows"][0]["sketch"], "token": anchors["rows"][0]["token"],
                          "visible": anchors["rows"][0]["visible"]}
    return anchors


def build(design, opts, plan, layer, warnings):
    """Add the plane, measure it, and rebuild it once if its sign went the other way."""
    units = opts["units"]
    records, token = fo.baseline(design)
    try:
        anchors = _place_anchors(plan, opts, layer)
    except RuntimeError as exc:
        return fo.refused(units, [f"the anchors could not be placed: {exc}"],
                          warnings=warnings + [f"any made so far stay, as {opts['name']}_anchors*"],
                          baseline_token=token, resolved=plan["resolved"])
    rows = _anchor_rows(plan, anchors, units) if anchors else []
    warnings = warnings + plan["warnings"]

    def refused_add(outcome):
        kept = ["the anchors stay, hidden: nothing is ever deleted from them"] if rows else []
        return fo.refused(units, [outcome["refusal"]], warnings=warnings + outcome["warnings"] + kept,
                          baseline_token=token, anchors=rows, **_common(plan))

    outcome = fo.add_feature(lambda: layer.add(plan["definition"], False))
    if outcome["status"] == "refused":
        return refused_add(outcome)
    warnings += outcome["warnings"]
    plane = outcome["feature"]
    built = layer.read(plane)
    state, rebuilt = judge(plan, built), False
    if state == "flip":
        undone = fo.undo(design, plane, records)
        warnings += undone["warnings"]
        if undone["undone"]:
            outcome = fo.add_feature(lambda: layer.add(plan["definition"], True))
            if outcome["status"] == "refused":
                return refused_add(outcome)
            warnings += outcome["warnings"]
            plane, rebuilt = outcome["feature"], True
            built = layer.read(plane)
            state = judge(plan, built)
        else:
            state = "off"

    name = layer.set_name(plane, opts["name"])
    if name != opts["name"]:
        warnings.append(f"the plane asked to be {opts['name']!r} is {name!r}")
    if state != "ok":
        warnings.append(f"the plane is not where it was asked: asked origin "
                        f"{display_point(plan['origin'], units)} normal {display_direction(plan['normal'])}, built "
                        f"origin {display_point(built['origin'], units)} normal {display_direction(built['normal'])}")
    row = {**fo.feature_row(plane, outcome["health"]), "component": layer.component_name}
    return fo.envelope("built" if state == "ok" else "suspect", units, feature=row, baseline_token=token,
                       warnings=warnings, reused=False, rebuilt=rebuilt, anchors=rows,
                       plane=_plane_row(built, name, opts, layer), sign=sign_row(plan["normal"], built["normal"]),
                       **_common(plan))


def run(design, document, opts, layer):
    """One plane: refuse, predict, reuse or plan or build."""
    units, dry_run = opts["units"], opts["dry_run"]
    targets = [{"occurrence_path": opts["occurrence_path"]}] if opts["occurrence_path"] else ()
    refusals, warnings = fo.preflight(design, document, targets)
    plan = None
    try:
        plan = PLANNERS[opts["mode"]](opts, layer)
    except ValueError as exc:
        refusals.append(str(exc))
    if refusals:
        return fo.refused(units, refusals, dry_run=dry_run, warnings=warnings)

    existing = find_existing(layer.planes(), plan) if opts["reuse_existing"] else None
    if existing is None and layer.has_plane_named(opts["name"]):
        return fo.refused(units, [f"a plane named {opts['name']!r} already exists in {layer.component_name!r}, "
                                  "and it is not one this call can reuse; pick another name"],
                          dry_run=dry_run, warnings=warnings, **_common(plan))
    if dry_run:
        row = _plan_row(plan, opts, layer)
        row["reused"] = existing is not None
        if existing is not None:
            row["anchors"] = []
        return fo.envelope("planned", units, dry_run=True, warnings=warnings + plan["warnings"], plan=row,
                           **_common(plan))
    if existing is not None:
        return _reused(existing, plan, opts, layer, warnings + plan["warnings"])
    return build(design, opts, plan, layer, warnings)


# -- Fusion layer ---------------------------------------------------------------


def _owner(entity):
    """The component an entity belongs to, wherever that entity keeps it."""
    context = safe(entity, "assemblyContext")
    if context is not None:
        return safe(context, "component")
    return present(safe(entity, "body"), "parentComponent",
                   present(safe(entity, "parentSketch"), "parentComponent", safe(entity, "component")))


def prepare_entities(entities, component, occurrence):
    """Entities for a definition in *component*, and the creation occurrence if it needs one.

    In root, proxies work as they are. In a sub-component its own geometry goes in
    natively, and geometry from any other component needs the occurrence to be
    placed by.
    """
    if occurrence is None:
        return entities, None
    owners = [_owner(entity) for entity in entities]
    if all(owner is None or owner == component for owner in owners):
        return [native(entity) for entity in entities], None
    return entities, occurrence


class FusionPlanes:
    """What planning and building ask of Fusion, for one component. Lives for one call.

    Raises ValueError when the occurrence or component cannot be found.
    """

    def __init__(self, design, opts):
        self._design = design
        self.scene = FusionScene(design)
        self._units = design.unitsManager
        self._occurrence = None
        self._component = design.rootComponent
        self.component_name = ROOT_PATH
        if opts["occurrence_path"]:
            self._occurrence = find_occurrence(design.rootComponent, opts["occurrence_path"])
            if self._occurrence is None:
                raise ValueError(f"no occurrence at {opts['occurrence_path']!r}")
            self._component = self._occurrence.component
            self.component_name = safe(self._component, "name")
            if opts["component"] not in (ROOT_PATH, self.component_name):
                raise ValueError(f"occurrence {opts['occurrence_path']!r} is of {self.component_name!r}, "
                                 f"not {opts['component']!r}")

    def evaluate(self, value, kind):
        """A value in cm or degrees. Fusion's internal angle unit is the radian."""
        fo.value_input(value, self._units, kind)
        result = self._units.evaluateExpression(value["text"], kind)
        return math.degrees(result) if kind == "deg" else result

    def _view(self, entity):
        """*entity* as seen from world: a native one goes through this call's occurrence."""
        if self._occurrence is None or safe(entity, "assemblyContext") is not None:
            return entity
        return entity.createForAssemblyContext(self._occurrence)

    def read(self, plane):
        view = self._view(plane)
        geometry = view.geometry
        return {"origin": xyz(geometry.origin), "normal": xyz(geometry.normal), "u": xyz(geometry.uDirection),
                "v": xyz(geometry.vDirection), "token": safe(view, "entityToken"), "name": safe(plane, "name")}

    def _plane_parent(self, plane, kind, name):
        seen = self.read(plane)
        return {"entity": plane, "point": seen["origin"], "normal": seen["normal"],
                "row": {"kind": kind, "name": name, "token": seen["token"], "visible": safe(plane, "isVisible")}}

    def root_plane(self, key):
        return self._plane_parent(getattr(self._component, ROOT_PLANES[key]), "root_plane", key.upper())

    def plane_by_token(self, token):
        found = attempt(lambda: list(self._design.findEntityByToken(token)), [])
        planes = [entity for entity in found if str(safe(entity, "objectType", "")).endswith(_PLANE_TYPE)]
        if len(planes) != 1:
            raise ValueError("'from_plane_token' does not name one construction plane")
        return self._plane_parent(planes[0], "plane", safe(planes[0], "name"))

    def face_parent(self, face):
        if SURFACE_TYPES.get(safe(safe(face, "geometry"), "surfaceType")) != "plane":
            return None
        point, normal = outward_normal(face)
        if normal is None:
            return None
        body = safe(face, "body")
        return {"entity": face, "point": point, "normal": normal,
                "row": {"kind": "face", "name": safe(body, "name"), "token": safe(face, "entityToken"),
                        "visible": safe(body, "isVisible")}}

    def root_axis(self, key):
        axis = getattr(self._component, ROOT_AXES[key])
        line = self._view(axis).geometry
        return {"entity": axis, "point": xyz(line.origin), "direction": xyz(line.direction)}

    def edge_line(self, edge):
        """An edge's start and direction, start vertex to end vertex; None for a curve."""
        if CURVE_TYPES.get(safe(safe(edge, "geometry"), "curveType")) != "line":
            return None
        start, end = xyz(edge.startVertex.geometry), xyz(edge.endVertex.geometry)
        return {"entity": edge, "point": start, "direction": sub(end, start)}

    def vertex_point(self, vertex):
        return xyz(vertex.geometry)

    def sketch_point(self, token):
        found = attempt(lambda: list(self._design.findEntityByToken(token)), [])
        points = [entity for entity in found if str(safe(entity, "objectType", "")).endswith("SketchPoint")]
        if len(points) != 1:
            raise ValueError("'sketch_point_token' does not name one sketch point")
        point = points[0]
        if self._occurrence is None and safe(point, "assemblyContext") is None:
            owner = _owner(point)
            if owner is not None and owner != self._design.rootComponent:
                raise ValueError("that sketch point is in a sub-component: pass 'occurrence_path' for its instance")
        return point, xyz(self._view(point).worldGeometry)

    def body_boxes(self):
        boxes = (bbox(e["_body"]) for e in self.scene.entries() if e["visible"] and e["solid"])
        return [box for box in boxes if box is not None]

    def planes(self):
        """What can be read: a plane that will not answer is not one to reuse."""
        seen = (attempt(lambda p=plane: self.read(p)) for plane in strict_items(self._component.constructionPlanes))
        return [plane for plane in seen if plane is not None]

    def has_plane_named(self, name):
        return self._component.constructionPlanes.itemByName(name) is not None

    def anchors(self, world_points, name, show):
        frame = occurrence_frame(self._occurrence)
        local = [point if frame is None else to_local(point, frame) for point in world_points]
        return fo.anchor_points(self._component, local, name, show)

    def add(self, definition, flipped):
        entities, creation = prepare_entities(definition["entities"], self._component, self._occurrence)
        planes = self._component.constructionPlanes
        entry = planes.createInput() if creation is None else planes.createInput(creation)
        kind = definition["kind"]
        if kind == "three_points":
            accepted = entry.setByThreePoints(*entities)
        else:
            value = negated(definition["value"]) if flipped else definition["value"]
            given = fo.value_input(value, self._units, definition["unit"])
            accepted = (entry.setByOffset(entities[0], given) if kind == "offset"
                        else entry.setByAngle(entities[0], given, entities[1]))
        if accepted is False:
            raise RuntimeError(f"Fusion would not accept this {kind} definition")
        return planes.add(entry)

    def set_name(self, plane, name):
        plane.name = name
        return safe(plane, "name")


def construction_plane(arguments):
    """Make a construction plane in the active design."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            refusals, _ = fo.preflight(None)
            return fo.write_result(fo.refused(opts["units"], refusals, dry_run=opts["dry_run"]))
        try:
            layer = FusionPlanes(design, opts)
        except ValueError as exc:
            return fo.write_result(fo.refused(opts["units"], [str(exc)], dry_run=opts["dry_run"]))
        payload = run(design, safe(app, "activeDocument"), opts, layer)
        log(f"[MCP] construction_plane {opts['mode']}: {payload['status']}")
        return fo.write_result(payload)
    except Exception as exc:
        log(f"[MCP] construction_plane failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
