"""Build closed sketch outlines safely and inspect sketch profiles.

Raw API sketches go wrong in quiet ways: lines drawn from separate Point3D
objects are not connected, 3-point arcs come back counterclockwise with their
ends swapped, a self-intersecting outline yields several profiles (and
``profiles.item(0)`` may be a sliver), and model coordinates do not equal sketch
coordinates on non-XY planes. ``closed_polyline`` validates the outline first
(see sketch_geometry), then draws every curve through shared SketchPoints and
reports the profiles it produced. ``profiles`` describes an existing sketch.
"""

import json
import traceback

import adsk.core
import adsk.fusion

from . import sketch_geometry as geo
from . import value_builders
from .dispatch import get_app, log

ACTIONS = ("closed_polyline", "profiles")
SPACES = ("sketch", "model")
STROKE_TOL_CM = 1e-3
SKETCH_KEY = "sketch_profile_sketch"
PROFILE_KEY = "sketch_profile_profile"
_ROOT_PLANE_ATTRS = {"xy": "xYConstructionPlane", "xz": "xZConstructionPlane", "yz": "yZConstructionPlane"}
_PLANE_TYPES = ("adsk::fusion::ConstructionPlane", "adsk::fusion::BRepFace")


def _result(payload, is_error=False):
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def _error(text):
    return _result(f"Error: {text}", True)


# -- Argument parsing (Fusion-free) ------------------------------------------------


def _optional_string(arguments, key):
    value = arguments.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise geo.GeometryError([f"'{key}' must be a non-empty string"])
    return value


def parse_request(arguments):
    """Validate tool arguments without touching Fusion.

    For closed_polyline in sketch space this runs the full outline validation;
    model-space outlines are checked against the plane here (root planes) and
    re-validated in true sketch coordinates once the sketch exists.
    """
    if not isinstance(arguments, dict):
        raise geo.GeometryError(["arguments must be an object"])
    action = arguments.get("action")
    if action not in ACTIONS:
        raise geo.GeometryError([f"'action' must be one of {list(ACTIONS)}, got {action!r}"])
    units = arguments.get("units")
    factor = geo.unit_factor(units)
    space = arguments.get("space", "sketch")
    if space not in SPACES:
        raise geo.GeometryError([f"'space' must be one of {list(SPACES)}, got {space!r}"])
    dims = 2 if space == "sketch" else 3
    request = {"action": action, "units": units, "factor": factor, "space": space,
               "component": _optional_string(arguments, "component"),
               "name": _optional_string(arguments, "name"), "warnings": []}

    if action == "profiles":
        for key in ("sketch_plane", "plane_entity_token", "points", "arcs"):
            if key in arguments:
                raise geo.GeometryError([f"'{key}' is only used by closed_polyline"])
        request["sketch_entity_token"] = _optional_string(arguments, "sketch_entity_token")
        if (request["name"] is None) == (request["sketch_entity_token"] is None):
            raise geo.GeometryError(["profiles needs exactly one of 'name' or 'sketch_entity_token'"])
        point = arguments.get("point")
        request["point"] = None if point is None else geo.parse_point(point, dims, "point", factor)
        return request

    for key in ("sketch_entity_token", "point"):
        if key in arguments:
            raise geo.GeometryError([f"'{key}' is only used by profiles"])
    plane = arguments.get("sketch_plane")
    token = _optional_string(arguments, "plane_entity_token")
    if (plane is None) == (token is None):
        raise geo.GeometryError(["closed_polyline needs exactly one of 'sketch_plane' or 'plane_entity_token'"])
    if plane is not None and plane not in _ROOT_PLANE_ATTRS:
        raise geo.GeometryError([f"'sketch_plane' must be one of {sorted(_ROOT_PLANE_ATTRS)}, got {plane!r}"])
    request.update(sketch_plane=plane, plane_entity_token=token)

    points, warnings = geo.parse_points(arguments.get("points"), dims, factor)
    arcs = geo.parse_arcs(arguments.get("arcs"), len(points), dims, factor)
    request["warnings"] += warnings
    request.update(points=points, arcs=arcs)
    if space == "sketch":
        request["mids"], outline_warnings = geo.validate_outline(points, arcs)
        request["warnings"] += outline_warnings
    else:
        flat, flat_arcs = geo.flatten_model_input(points, arcs, plane, factor, units)
        geo.validate_outline(flat, flat_arcs)  # Early refusal; re-run on real sketch coordinates.
    return request


# -- Fusion helpers ----------------------------------------------------------------


def _point3d(x, y, z=0.0):
    return adsk.core.Point3D.create(x, y, z)


def _xyz(point):
    return (point.x, point.y, point.z)


def _design():
    design = adsk.fusion.Design.cast(get_app().activeProduct)
    if design is None:
        raise geo.GeometryError(["the active product is not a Fusion design"])
    return design


def _component(design, name):
    if name is None or name == "root":
        return design.rootComponent
    component = design.allComponents.itemByName(name)
    if component is None:
        names = [design.allComponents.item(i).name for i in range(min(design.allComponents.count, 20))]
        raise geo.GeometryError([f"no component named {name!r}; components include {names}"])
    return component


def _plane(design, component, request):
    if request["sketch_plane"]:
        return getattr(component, _ROOT_PLANE_ATTRS[request["sketch_plane"]])
    entities = list(design.findEntityByToken(request["plane_entity_token"]) or [])
    if not entities:
        raise geo.GeometryError(["'plane_entity_token' does not match anything in the active design"])
    entity = entities[0]
    if getattr(entity, "objectType", None) not in _PLANE_TYPES:
        raise geo.GeometryError([f"'plane_entity_token' is a {getattr(entity, 'objectType', '?')}, "
                                 "not a construction plane or planar face"])
    return entity


def _sketch_coordinates(sketch, request):
    """Outline points and arc mids in sketch-space 2D cm, validated."""
    if request["space"] == "sketch":
        return request["points"], request["mids"], []
    labels, model = [], []
    for i, p in enumerate(request["points"]):
        labels.append(f"points[{i}]")
        model.append(p)
    mid_keys = [i for i, spec in sorted(request["arcs"].items()) if "mid" in spec]
    for i in mid_keys:
        labels.append(f"arc on segment {i} mid_point")
        model.append(request["arcs"][i]["mid"])
    mapped = [_xyz(sketch.modelToSketchSpace(_point3d(*p))) for p in model]
    geo.check_on_plane([abs(p[2]) for p in mapped], labels, factor=request["factor"], units=request["units"])
    n = len(request["points"])
    points2d = [p[:2] for p in mapped[:n]]
    arcs2d = {i: dict(spec) for i, spec in request["arcs"].items()}
    for i, p in zip(mid_keys, mapped[n:]):
        arcs2d[i] = {"mid": p[:2]}
    mids, warnings = geo.validate_outline(points2d, arcs2d)
    return points2d, mids, warnings


def build_loop(sketch, points2d, mids, point3d=_point3d):
    """Draw the closed loop so neighboring curves share one SketchPoint per vertex.

    Lines are drawn from the previous curve's SketchPoint, never from a fresh
    Point3D. Arcs are always created counterclockwise, so their ends are matched
    to vertices by position and merged into the shared point when needed.
    Returns (curves, vertex SketchPoints, warnings).
    """
    lines = sketch.sketchCurves.sketchLines
    arcs = sketch.sketchCurves.sketchArcs
    n = len(points2d)
    vertices = [None] * n
    curves, warnings = [], []
    for i in range(n):
        j = (i + 1) % n
        start = vertices[i] if vertices[i] is not None else point3d(*points2d[i])
        end = vertices[j] if vertices[j] is not None else point3d(*points2d[j])
        if i in mids:
            curve = arcs.addByThreePoints(start, point3d(*mids[i]), end)
        else:
            curve = lines.addByTwoPoints(start, end)
        if curve is None:
            raise RuntimeError(f"Fusion did not create segment {i}")
        ends = [curve.startSketchPoint, curve.endSketchPoint]
        if geo.distance(_xyz(ends[0].geometry)[:2], points2d[i]) > geo.distance(_xyz(ends[1].geometry)[:2], points2d[i]):
            ends.reverse()
        for k, end_point in ((i, ends[0]), (j, ends[1])):
            if vertices[k] is None:
                vertices[k] = end_point
            elif vertices[k] != end_point and not vertices[k].merge(end_point):
                sketch.geometricConstraints.addCoincident(end_point, vertices[k])
                warnings.append(f"vertex {k} could not be merged; a coincident constraint joins it instead")
        curves.append(curve)
    return curves, vertices, warnings


def _accuracy():
    accuracy = getattr(adsk.fusion, "CalculationAccuracy", None)
    return getattr(accuracy, "MediumCalculationAccuracy", 1)


def _stroke(curve_geometry):
    evaluator = curve_geometry.evaluator
    ok, start, end = evaluator.getParameterExtents()
    if not ok:
        return []
    ok, points = evaluator.getStrokes(start, end, STROKE_TOL_CM)
    return [_xyz(p) for p in points] if ok else []


def _loop_polylines(profile, sketch):
    """Each profile loop as a closed 2D polyline in sketch space, or None if unreliable."""
    loops = []
    for li in range(profile.profileLoops.count):
        loop = profile.profileLoops.item(li)
        pts = []
        for ci in range(loop.profileCurves.count):
            pts.extend(_stroke(loop.profileCurves.item(ci).geometry))
        loops.append(pts)
    box = profile.boundingBox
    lo, hi = _xyz(box.minPoint), _xyz(box.maxPoint)
    pad = 10 * STROKE_TOL_CM

    def inside_box(polys):
        return all(lo[a] - pad <= p[a] <= hi[a] + pad for poly in polys for p in poly for a in (0, 1))

    if not all(loops):
        return None
    if inside_box(loops):
        return [[p[:2] for p in poly] for poly in loops]
    # ProfileCurve.geometry is not documented as sketch space; if it is model space, map it.
    mapped = [[_xyz(sketch.modelToSketchSpace(_point3d(*p))) for p in poly] for poly in loops]
    return [[p[:2] for p in poly] for poly in mapped] if inside_box(mapped) else None


def describe_profiles(sketch, factor, query=None):
    """Area, centroid and loops of every profile; containment of *query* (2D sketch cm)."""
    described = []
    for i in range(sketch.profiles.count):
        profile = sketch.profiles.item(i)
        props = profile.areaProperties(_accuracy())
        loops = [profile.profileLoops.item(k) for k in range(profile.profileLoops.count)]
        info = {
            "index": i,
            "entity_token": getattr(profile, "entityToken", None),
            "area": props.area / factor ** 2,
            "centroid": [c / factor for c in _xyz(props.centroid)[:2]],
            "loop_count": len(loops),
            "inner_loop_count": sum(1 for loop in loops if not loop.isOuter),
            "curve_count": sum(loop.profileCurves.count for loop in loops),
        }
        if query is not None:
            polylines = _loop_polylines(profile, sketch)
            info["contains_point"] = None if polylines is None else geo.point_in_loops(query, polylines)
        described.append(info)
    return described


def choose_profile(described):
    """(largest index, containing index) from describe_profiles output."""
    largest = max(described, key=lambda d: d["area"])["index"] if described else None
    containing = next((d["index"] for d in described if d.get("contains_point")), None)
    return largest, containing


def _store(sketch, profile):
    value_builders.OBJECT_STORE[SKETCH_KEY] = sketch
    if profile is not None:
        value_builders.OBJECT_STORE[PROFILE_KEY] = profile
    else:
        value_builders.OBJECT_STORE.pop(PROFILE_KEY, None)


# -- Actions --------------------------------------------------------------------------


def _closed_polyline(request):
    design = _design()
    component = _component(design, request["component"])
    if request["name"] and component.sketches.itemByName(request["name"]) is not None:
        raise geo.GeometryError([f"a sketch named {request['name']!r} already exists in {component.name!r}"])
    plane = _plane(design, component, request)
    sketch = component.sketches.add(plane)
    if sketch is None:
        raise RuntimeError("Fusion did not create the sketch")
    try:
        points2d, mids, warnings = _sketch_coordinates(sketch, request)
        warnings = request["warnings"] + warnings
        sketch.isComputeDeferred = True
        try:
            curves, vertices, build_warnings = build_loop(sketch, points2d, mids)
        finally:
            sketch.isComputeDeferred = False
        warnings += build_warnings
        if request["name"]:
            sketch.name = request["name"]
        unconnected = [k for k, v in enumerate(vertices) if v.connectedEntities.count < 2]
        described = describe_profiles(sketch, request["factor"])
        if not described:
            raise RuntimeError(f"the outline produced no profile (unconnected vertices: {unconnected})")
    except Exception:
        sketch.deleteMe()  # Never leave a half-built sketch behind.
        raise
    if unconnected:
        warnings.append(f"vertices {unconnected} are not shared by two curves; the loop may not be closed")
    largest, _ = choose_profile(described)
    if len(described) > 1:
        warnings.append(f"the outline produced {len(described)} profiles; the largest (index {largest}) was chosen")
    profile = sketch.profiles.item(largest)
    _store(sketch, profile)
    factor = request["factor"]
    return {
        "sketch": {"name": sketch.name, "entity_token": sketch.entityToken, "component": component.name,
                   "stored_as": SKETCH_KEY},
        "profile": {"index": largest, "entity_token": getattr(profile, "entityToken", None),
                    "area": described[largest]["area"], "stored_as": PROFILE_KEY},
        "units": request["units"],
        "profile_count": len(described),
        "curve_count": len(curves),
        "arc_segments": sorted(mids),
        "sketch_points": [[c / factor for c in p] for p in points2d],
        "unconnected_vertices": unconnected,
        "profiles": described,
        "warnings": warnings,
    }


def _profiles(request):
    design = _design()
    if request["sketch_entity_token"]:
        found = [e for e in (design.findEntityByToken(request["sketch_entity_token"]) or [])
                 if getattr(e, "objectType", None) == "adsk::fusion::Sketch"]
        if not found:
            raise geo.GeometryError(["'sketch_entity_token' does not match a sketch in the active design"])
        sketch = found[0]
    else:
        component = _component(design, request["component"])
        sketch = component.sketches.itemByName(request["name"])
        if sketch is None:
            raise geo.GeometryError([f"no sketch named {request['name']!r} in {component.name!r}"])
    query, warnings = None, []
    if request["point"] is not None:
        p = request["point"]
        if request["space"] == "model":
            p = _xyz(sketch.modelToSketchSpace(_point3d(*p)))
            if abs(p[2]) > geo.PLANE_TOL_CM:
                warnings.append(f"point is {p[2] / request['factor']:.6g} {request['units']} off the sketch plane; "
                                "containment uses its projection")
        query = tuple(p[:2])
    described = describe_profiles(sketch, request["factor"], query)
    largest, containing = choose_profile(described)
    if query is not None and any(d["contains_point"] is None for d in described):
        warnings.append("containment could not be determined for some profiles (contains_point is null)")
    chosen = containing if query is not None else largest
    _store(sketch, sketch.profiles.item(chosen) if chosen is not None else None)
    return {
        "sketch": {"name": sketch.name, "entity_token": sketch.entityToken, "stored_as": SKETCH_KEY},
        "units": request["units"],
        "profile_count": len(described),
        "largest_profile": largest,
        "profile_containing": containing,
        "stored_profile": PROFILE_KEY if chosen is not None else None,
        "profiles": described,
        "warnings": warnings,
    }


def sketch_profile(arguments):
    """MCP boundary: validate, then build or inspect on Fusion's main thread."""
    try:
        request = parse_request(arguments)
    except geo.GeometryError as exc:
        return _result({"error": "invalid input; nothing was created", "problems": exc.problems}, True)
    try:
        payload = _closed_polyline(request) if request["action"] == "closed_polyline" else _profiles(request)
        log(f"[MCP] sketch_profile {request['action']}: {payload['profile_count']} profile(s)")
        return _result(payload)
    except geo.GeometryError as exc:
        note = "refused; nothing was created" if request["action"] == "closed_polyline" else "refused"
        return _result({"error": note, "problems": exc.problems}, True)
    except Exception as exc:
        log(f"[MCP] sketch_profile failed: {exc}")
        return _error(f"{exc}\n{traceback.format_exc()}")
