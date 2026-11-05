"""Build closed sketch outlines safely and inspect sketch profiles.

Raw API sketches go wrong in quiet ways: lines drawn from separate Point3D
objects are not connected, 3-point arcs come back counterclockwise with their
ends swapped, a self-intersecting outline yields several profiles (and
``profiles.item(0)`` may be a sliver), and model coordinates do not equal sketch
coordinates on non-XY planes. ``closed_polyline`` validates the outline first
(see sketch_geometry), then draws every curve through shared SketchPoints and
reports the profiles it produced. ``circles`` draws each circle as one entity,
so its extruded wall is one face and can be threaded. Both build a new sketch or
add to an existing one, so a plate outline and its holes can share a sketch.
``profiles`` describes an existing sketch.
"""

from collections import namedtuple
from contextlib import contextmanager
import traceback

import adsk.core
import adsk.fusion

from . import sketch_geometry as geo
from . import value_builders
from .common.api import attempt, safe
from .common.bodies import native
from .common.geometry import xyz
from .common.results import error_result, tool_result
from .dispatch import get_app, log

ACTIONS = ("closed_polyline", "circles", "profiles")
SPACES = ("sketch", "model")
STROKE_TOL_CM = 1e-3
SKETCH_KEY = "sketch_profile_sketch"
PROFILE_KEY = "sketch_profile_profile"
_ROOT_PLANE_ATTRS = {"xy": "xYConstructionPlane", "xz": "xZConstructionPlane", "yz": "yZConstructionPlane"}
_PLANE_TYPES = ("adsk::fusion::ConstructionPlane", "adsk::fusion::BRepFace")
# Arguments that belong to some actions only.
_BUILDERS = ("closed_polyline", "circles")
_ACTION_KEYS = {"sketch_plane": _BUILDERS, "plane_entity_token": _BUILDERS, "points": ("closed_polyline",),
                "arcs": ("closed_polyline",), "circles": ("circles",), "point": ("profiles",)}
# The sketch a build draws into. *existing* is the sketch the caller named, which
# must outlive a failed build; a new one is deleted.
Target = namedtuple("Target", "sketch component existing profiles_before")


# -- Argument parsing (Fusion-free) ------------------------------------------------


def _optional_string(arguments, key):
    value = arguments.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise geo.GeometryError([f"'{key}' must be a non-empty string"])
    return value


def _parse_target(arguments, request):
    plane = arguments.get("sketch_plane")
    token = _optional_string(arguments, "plane_entity_token")
    sketch_token = _optional_string(arguments, "sketch_entity_token")
    if sum(value is not None for value in (plane, token, sketch_token)) != 1:
        raise geo.GeometryError([f"{request['action']} needs exactly one of 'sketch_plane', "
                                 "'plane_entity_token' or 'sketch_entity_token'"])
    if plane is not None and plane not in _ROOT_PLANE_ATTRS:
        raise geo.GeometryError([f"'sketch_plane' must be one of {sorted(_ROOT_PLANE_ATTRS)}, got {plane!r}"])
    if sketch_token is not None:
        for key in ("name", "component"):
            if request[key] is not None:
                raise geo.GeometryError([f"'{key}' cannot be used with 'sketch_entity_token': "
                                         "that sketch already has its name and component"])
    request.update(sketch_plane=plane, plane_entity_token=token, sketch_entity_token=sketch_token)


def _parse_outline(arguments, request, dims):
    factor, units, plane = request["factor"], request["units"], request["sketch_plane"]
    points, warnings = geo.parse_points(arguments.get("points"), dims, factor)
    arcs = geo.parse_arcs(arguments.get("arcs"), len(points), dims, factor)
    request["warnings"] += warnings
    request.update(points=points, arcs=arcs)
    if request["space"] == "sketch":
        request["mids"], outline_warnings = geo.validate_outline(points, arcs)
        request["warnings"] += outline_warnings
    elif plane is not None or geo.newell_normal(points) is not None:
        # Early refusal; re-run on real sketch coordinates. With no root plane the
        # vertices give the normal, and a D of two points and an arc gives none.
        flat, flat_arcs = geo.flatten_model_input(points, arcs, plane, factor, units)
        geo.validate_outline(flat, flat_arcs)


def _parse_circles(arguments, request, dims):
    circles = geo.parse_circles(arguments.get("circles"), dims, request["factor"])
    request["circles"] = circles
    if request["space"] == "sketch":
        geo.validate_circles(circles)
    elif request["sketch_plane"]:
        geo.validate_circles(geo.flatten_circles(circles, request["sketch_plane"], request["factor"], request["units"]))


def parse_request(arguments):
    """Validate tool arguments without touching Fusion.

    In sketch space this runs the full outline or circle validation; model-space
    input is checked against the plane here (root planes) and re-validated in
    true sketch coordinates once the sketch exists.
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
    for key, users in _ACTION_KEYS.items():
        if key in arguments and action not in users:
            raise geo.GeometryError([f"'{key}' is only used by {' and '.join(users)}"])

    if action == "profiles":
        request["sketch_entity_token"] = _optional_string(arguments, "sketch_entity_token")
        if (request["name"] is None) == (request["sketch_entity_token"] is None):
            raise geo.GeometryError(["profiles needs exactly one of 'name' or 'sketch_entity_token'"])
        point = arguments.get("point")
        request["point"] = None if point is None else geo.parse_point(point, dims, "point", factor)
        return request

    _parse_target(arguments, request)
    if action == "circles":
        _parse_circles(arguments, request, dims)
    else:
        _parse_outline(arguments, request, dims)
    return request


# -- Fusion helpers ----------------------------------------------------------------


def _point3d(x, y, z=0.0):
    return adsk.core.Point3D.create(x, y, z)


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
    mapped = [xyz(sketch.modelToSketchSpace(_point3d(*p))) for p in model]
    geo.check_on_plane([abs(p[2]) for p in mapped], labels, factor=request["factor"], units=request["units"])
    n = len(request["points"])
    points2d = [p[:2] for p in mapped[:n]]
    arcs2d = {i: dict(spec) for i, spec in request["arcs"].items()}
    for i, p in zip(mid_keys, mapped[n:]):
        arcs2d[i] = {"mid": p[:2]}
    mids, warnings = geo.validate_outline(points2d, arcs2d)
    return points2d, mids, warnings


def _circle_coordinates(sketch, request):
    """The circles with sketch-space 2D centers in cm, validated."""
    circles = request["circles"]
    if request["space"] != "sketch":
        mapped = [xyz(sketch.modelToSketchSpace(_point3d(*c["center"]))) for c in circles]
        # Sketch z is the distance off the plane: refused here, then dropped, so a
        # curve is never drawn out of its own sketch.
        geo.check_on_plane([abs(p[2]) for p in mapped], [f"circles[{i}].center" for i in range(len(mapped))],
                           factor=request["factor"], units=request["units"])
        circles = [{"center": p[:2], "radius": c["radius"]} for p, c in zip(mapped, circles)]
    geo.validate_circles(circles)
    return circles


def build_loop(sketch, points2d, mids, point3d=_point3d, drawn=None):
    """Draw the closed loop so neighboring curves share one SketchPoint per vertex.

    Lines are drawn from the previous curve's SketchPoint, never from a fresh
    Point3D. Arcs are always created counterclockwise, so their ends are matched
    to vertices by position and merged into the shared point when needed.
    Returns (curves, vertex SketchPoints, warnings); *drawn*, when given, is the
    curves list, so a caller still has them if a later segment fails.
    """
    lines = sketch.sketchCurves.sketchLines
    arcs = sketch.sketchCurves.sketchArcs
    n = len(points2d)
    vertices = [None] * n
    curves = drawn if drawn is not None else []
    warnings = []
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
        curves.append(curve)
        ends = [curve.startSketchPoint, curve.endSketchPoint]
        if geo.distance(xyz(ends[0].geometry)[:2], points2d[i]) > geo.distance(xyz(ends[1].geometry)[:2], points2d[i]):
            ends.reverse()
        for k, end_point in ((i, ends[0]), (j, ends[1])):
            if vertices[k] is None:
                vertices[k] = end_point
            elif vertices[k] != end_point and not vertices[k].merge(end_point):
                sketch.geometricConstraints.addCoincident(end_point, vertices[k])
                warnings.append(f"vertex {k} could not be merged; a coincident constraint joins it instead")
    return curves, vertices, warnings


def build_circles(sketch, circles, point3d=_point3d, drawn=None):
    """Draw each circle as one entity at sketch z 0; returns the SketchCircles."""
    add = sketch.sketchCurves.sketchCircles.addByCenterRadius
    curves = drawn if drawn is not None else []
    for i, circle in enumerate(circles):
        curve = add(point3d(*circle["center"]), circle["radius"])
        if curve is None:
            raise RuntimeError(f"Fusion did not create circle {i}")
        curves.append(curve)
    return curves


def erase_curves(curves):
    """Delete curves this call drew, then any of their points left unconnected.

    Returns how many curves could not be deleted. Drawing always starts from
    fresh points, so an unconnected point here is never the caller's.
    """
    points = [point for curve in curves
              for point in (safe(curve, name) for name in ("startSketchPoint", "endSketchPoint", "centerSketchPoint"))
              if point is not None]
    left = sum(1 for curve in reversed(curves) if not attempt(curve.deleteMe, False))
    for point in points:
        if attempt(lambda: point.isValid and point.connectedEntities.count == 0, False):
            attempt(point.deleteMe)
    return left


def _accuracy():
    accuracy = getattr(adsk.fusion, "CalculationAccuracy", None)
    return getattr(accuracy, "MediumCalculationAccuracy", 1)


def _stroke(curve_geometry):
    evaluator = curve_geometry.evaluator
    ok, start, end = evaluator.getParameterExtents()
    if not ok:
        return []
    ok, points = evaluator.getStrokes(start, end, STROKE_TOL_CM)
    return [xyz(p) for p in points] if ok else []


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
    lo, hi = xyz(box.minPoint), xyz(box.maxPoint)
    pad = 10 * STROKE_TOL_CM

    def inside_box(polys):
        return all(lo[a] - pad <= p[a] <= hi[a] + pad for poly in polys for p in poly for a in (0, 1))

    if not all(loops):
        return None
    if inside_box(loops):
        return [[p[:2] for p in poly] for poly in loops]
    # ProfileCurve.geometry is not documented as sketch space; if it is model space, map it.
    mapped = [[xyz(sketch.modelToSketchSpace(_point3d(*p))) for p in poly] for poly in loops]
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
            "centroid": [c / factor for c in xyz(props.centroid)[:2]],
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


def _circle_curve_type():
    types = getattr(adsk.core, "Curve3DTypes", None)
    return getattr(types, "Circle3DCurveType", 2)


def circle_states(sketch, circles):
    """Per circle: True if a profile uses it whole, False if cut, None if in no profile.

    Curves that cross a circle leave it one sketch entity, but its profile
    curves become arcs and its extruded wall comes out in pieces.
    """
    pieces = []
    for i in range(sketch.profiles.count):
        loops = sketch.profiles.item(i).profileLoops
        for k in range(loops.count):
            curves = loops.item(k).profileCurves
            for c in range(curves.count):
                curve = curves.item(c)
                pieces.append((curve.sketchEntity, curve.geometryType == _circle_curve_type()))
    states = []
    for circle in circles:
        whole = [is_circle for entity, is_circle in pieces if entity == circle]
        states.append(all(whole) if whole else None)
    return states


def _store(sketch, profile):
    value_builders.OBJECT_STORE[SKETCH_KEY] = sketch
    if profile is not None:
        value_builders.OBJECT_STORE[PROFILE_KEY] = profile
    else:
        value_builders.OBJECT_STORE.pop(PROFILE_KEY, None)


# -- Actions --------------------------------------------------------------------------


def _sketch_by_token(design, token):
    found = [e for e in (design.findEntityByToken(token) or [])
             if getattr(e, "objectType", None) == "adsk::fusion::Sketch"]
    if not found:
        raise geo.GeometryError(["'sketch_entity_token' does not match a sketch in the active design"])
    return found[0]


def _new_sketch(design, component, request):
    if request["name"] and component.sketches.itemByName(request["name"]) is not None:
        raise geo.GeometryError([f"a sketch named {request['name']!r} already exists in {component.name!r}"])
    sketch = component.sketches.add(_plane(design, component, request))
    if sketch is None:
        raise RuntimeError("Fusion did not create the sketch")
    return sketch


def _open_target(design, request):
    if request["sketch_entity_token"]:
        sketch = native(_sketch_by_token(design, request["sketch_entity_token"]))  # Geometry is drawn on the native.
        return Target(sketch, sketch.parentComponent, True, sketch.profiles.count)
    component = _component(design, request["component"])
    return Target(_new_sketch(design, component, request), component, False, 0)


@contextmanager
def _undo_on_failure(target):
    """Yield the list of drawn curves; on failure leave the design as it was found."""
    drawn = []
    try:
        yield drawn
    except Exception as exc:
        if not target.existing:
            target.sketch.deleteMe()  # Never leave a half-built sketch behind.
            raise
        left = erase_curves(drawn)
        if left:  # Not a refusal: something was left behind.
            raise RuntimeError(f"{exc}; {left} drawn curve(s) could not be removed from "
                               f"{target.sketch.name!r}") from exc
        raise


@contextmanager
def _deferred_compute(target):
    """Defer the sketch compute, but never on an existing sketch.

    Fusion documents that deferring while editing a sketch features depend on
    can produce a bad model in those features.
    """
    if target.existing:
        yield
        return
    target.sketch.isComputeDeferred = True
    try:
        yield
    finally:
        target.sketch.isComputeDeferred = False


def _common_result(target, described, request):
    """Store the largest profile and return the keys every build reports."""
    sketch = target.sketch
    largest, _ = choose_profile(described)
    profile = sketch.profiles.item(largest)
    _store(sketch, profile)
    return {
        "sketch": {"name": sketch.name, "entity_token": sketch.entityToken, "component": target.component.name,
                   "stored_as": SKETCH_KEY},
        "profile": {"index": largest, "entity_token": getattr(profile, "entityToken", None),
                    "area": described[largest]["area"], "stored_as": PROFILE_KEY},
        "units": request["units"],
        "profile_count": len(described),
        "added_to_existing": target.existing,
        "profile_count_before": target.profiles_before,
    }


def _no_new_region_warnings(target, described):
    if target.existing and len(described) <= target.profiles_before:
        return [f"the sketch had {target.profiles_before} profile(s) and now has {len(described)}; "
                "the new curves did not add a region"]
    return []


def _closed_polyline(request):
    design = _design()
    target = _open_target(design, request)
    sketch = target.sketch
    with _undo_on_failure(target) as drawn:
        points2d, mids, warnings = _sketch_coordinates(sketch, request)
        warnings = request["warnings"] + warnings
        with _deferred_compute(target):
            curves, vertices, build_warnings = build_loop(sketch, points2d, mids, drawn=drawn)
        warnings += build_warnings
        if request["name"]:
            sketch.name = request["name"]
        unconnected = [k for k, v in enumerate(vertices) if v.connectedEntities.count < 2]
        described = describe_profiles(sketch, request["factor"])
        if not described:
            raise RuntimeError(f"the outline produced no profile (unconnected vertices: {unconnected})")
    if unconnected:
        warnings.append(f"vertices {unconnected} are not shared by two curves; the loop may not be closed")
    result = _common_result(target, described, request)
    if len(described) > 1 and not target.existing:
        warnings.append(f"the outline produced {len(described)} profiles; "
                        f"the largest (index {result['profile']['index']}) was chosen")
    warnings += _no_new_region_warnings(target, described)
    factor = request["factor"]
    return {
        **result,
        "curve_count": len(curves),
        "arc_segments": sorted(mids),
        "sketch_points": [[c / factor for c in p] for p in points2d],
        "unconnected_vertices": unconnected,
        "profiles": described,
        "warnings": warnings,
    }


def _circle_warnings(states):
    warnings = []
    for i, whole in enumerate(states):
        if whole is False:
            warnings.append(f"circle {i} is cut by other sketch curves; its wall would come out as several "
                            "faces, which cannot be threaded")
        elif whole is None:
            warnings.append(f"circle {i} bounds no profile")
    return warnings


def _circles(request):
    design = _design()
    target = _open_target(design, request)
    sketch = target.sketch
    with _undo_on_failure(target) as drawn:
        circles = _circle_coordinates(sketch, request)
        with _deferred_compute(target):
            curves = build_circles(sketch, circles, drawn=drawn)
        if request["name"]:
            sketch.name = request["name"]
        described = describe_profiles(sketch, request["factor"])
        if not described:
            raise RuntimeError("the circles produced no profile")
    states = circle_states(sketch, curves)
    factor = request["factor"]
    return {
        **_common_result(target, described, request),
        "curve_count": len(curves),
        "circles": [{"index": i, "center": [c / factor for c in circle["center"]],
                     "diameter": 2.0 * circle["radius"] / factor, "entity_token": safe(curve, "entityToken"),
                     "whole": state}
                    for i, (circle, curve, state) in enumerate(zip(circles, curves, states))],
        "profiles": described,
        "warnings": _circle_warnings(states) + _no_new_region_warnings(target, described),
    }


def _profiles(request):
    design = _design()
    if request["sketch_entity_token"]:
        sketch = _sketch_by_token(design, request["sketch_entity_token"])
    else:
        component = _component(design, request["component"])
        sketch = component.sketches.itemByName(request["name"])
        if sketch is None:
            raise geo.GeometryError([f"no sketch named {request['name']!r} in {component.name!r}"])
    query, warnings = None, []
    if request["point"] is not None:
        p = request["point"]
        if request["space"] == "model":
            p = xyz(sketch.modelToSketchSpace(_point3d(*p)))
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


_ACTION_HANDLERS = {"closed_polyline": _closed_polyline, "circles": _circles, "profiles": _profiles}


def sketch_profile(arguments):
    """MCP boundary: validate, then build or inspect on Fusion's main thread."""
    try:
        request = parse_request(arguments)
    except geo.GeometryError as exc:
        return tool_result({"error": "invalid input; nothing was created", "problems": exc.problems}, True)
    try:
        payload = _ACTION_HANDLERS[request["action"]](request)
        log(f"[MCP] sketch_profile {request['action']}: {payload['profile_count']} profile(s)")
        return tool_result(payload)
    except geo.GeometryError as exc:
        note = "refused; nothing was created" if request["action"] in _BUILDERS else "refused"
        return tool_result({"error": note, "problems": exc.problems}, True)
    except Exception as exc:
        log(f"[MCP] sketch_profile failed: {exc}")
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
