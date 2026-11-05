"""A new plate from an outline, with a pilot bore and a circular bolt pattern around it.

Arguments, in *units*, world coordinates:

- ``outline``: 3 or more ``[x, y, z]`` points on the sketch plane; the loop closes itself.
- ``arcs``: optional, as sketch_profile's, with mid_point in world coordinates. Corner radii go here.
- ``plane`` (``xy``, ``xz``, ``yz``) or ``plane_token`` (a construction plane or planar face): exactly one.
- ``direction``: the way the plate grows from the plane.
- ``thickness``.
- ``pilot_center``, ``pilot_diameter``: on the sketch plane.
- ``first_bolt``, ``bolt_diameter``, ``bolt_count``: the first hole's center on the sketch plane. The
  others follow around the pilot's axis, evenly over a full turn.
- ``bolt_chamfer``: optional size of a chamfer on every bolt hole's rim, on the far side from the plane.
- ``body_name``: optional. ``occurrence_path``: optional, the instance to build in.

Result, beyond ``steps.run_macro``'s: ``body`` and ``plate`` {top_normal, pilot_center, bolt_centers}.
"""

import math

from ..common.args import is_int
from ..common.bodies import ROOT_PATH
from ..common.entity_refs import angle_between
from ..common.geometry import dot, sub
from ..extrude_profile import DIRECTION_LIMIT_DEG
from . import inputs
from . import steps as st

_KNOWN = {"outline", "arcs", "plane", "plane_token", "direction", "thickness", "pilot_center", "pilot_diameter",
          "first_bolt", "bolt_diameter", "bolt_count", "bolt_chamfer", "body_name", "occurrence_path"}
_PLANE_NORMALS = {"xy": (0.0, 0.0, 1.0), "xz": (0.0, 1.0, 0.0), "yz": (1.0, 0.0, 0.0)}


def _outline(arguments):
    points = arguments.get("outline")
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError("'outline' must be an array of at least three points")
    return [list(inputs.point({"outline": item}, "outline")) for item in points]


def _text(arguments, key):
    value = arguments.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"'{key}' must be a non-empty string")
    return value


def _plane(arguments, args):
    plane, token = arguments.get("plane"), _text(arguments, "plane_token")
    if (plane is None) == (token is None):
        raise ValueError("give exactly one of 'plane' and 'plane_token'")
    if plane is not None and (not isinstance(plane, str) or plane not in _PLANE_NORMALS):
        raise ValueError(f"'plane' must be one of {sorted(_PLANE_NORMALS)}, got {plane!r}")
    # A component's own planes are only world planes in root.
    if plane is not None and inputs.is_root(args["occurrence_path"]):
        if angle_between(args["direction"], _PLANE_NORMALS[plane], signed=False) > DIRECTION_LIMIT_DEG:
            raise ValueError(f"'direction' does not run along the normal of plane {plane}")
    return {"sketch_plane": plane} if plane is not None else {"plane_entity_token": token}


def bolt_problem(args):
    """Why the bolt circle cannot be cut, or None: pure arithmetic on the arguments."""
    reach = sub(args["first_bolt"], args["pilot_center"])
    if abs(dot(reach, args["direction"])) > args["tolerance"]:
        return "'first_bolt' and 'pilot_center' are not on one plane across 'direction'"
    radius = math.sqrt(dot(reach, reach))
    chamfer = 2.0 * (args["bolt_chamfer"] or 0.0)
    bolt = args["bolt_diameter"] + chamfer
    if radius - bolt / 2.0 <= args["pilot_diameter"] / 2.0:
        return f"the bolt holes break into the pilot: the bolt circle's radius is {radius:.6g}"
    chord = 2.0 * radius * math.sin(math.pi / args["bolt_count"])
    if chord <= bolt:
        return f"neighboring bolt holes overlap: their centers are {chord:.6g} apart"
    return None


def parse(arguments):
    args = inputs.start(arguments, _KNOWN)
    count = arguments.get("bolt_count")
    if not is_int(count) or count < 2:
        raise ValueError("'bolt_count' must be an integer of at least 2")
    args.update(outline=_outline(arguments), arcs=arguments.get("arcs"), bolt_count=count,
                direction=inputs.direction(arguments, "direction"), thickness=inputs.length(arguments, "thickness"),
                pilot_center=inputs.point(arguments, "pilot_center"),
                pilot_diameter=inputs.length(arguments, "pilot_diameter"),
                first_bolt=inputs.point(arguments, "first_bolt"),
                bolt_diameter=inputs.length(arguments, "bolt_diameter"),
                bolt_chamfer=inputs.length(arguments, "bolt_chamfer", required=False),
                body_name=_text(arguments, "body_name"), occurrence_path=_text(arguments, "occurrence_path"))
    args["target"] = _plane(arguments, args)
    problem = bolt_problem(args)
    if problem:
        raise ValueError(problem)
    return args


def plan(args):
    chamfer = ["bolt rims", "bolt chamfer"] if args["bolt_chamfer"] else []
    return ["outline", "pilot sketch", "plate", "seed sketch", "seed hole", "pilot bore", "bolt pattern", *chamfer]


def _new_body(run, plate, path):
    wanted = path or ROOT_PATH
    rows = [row for row in plate.get("bodies", {}).get("created", []) if row["ref"]["occurrence_path"] == wanted]
    if len(rows) != 1:
        run.fail(f"expected one new body in {wanted!r}, the extrude reports {len(rows)}")
    return rows[0]["ref"]


def _seed_delta(run, cut):
    rows = [row for row in cut.get("bodies", {}).get("touched", []) if row.get("role") == "target"]
    if not rows or not rows[0].get("delta"):
        run.fail("the seed hole removed nothing")
    return rows[0]["delta"]


def build(run, args):
    tolerance, thickness, path = args["tolerance"], args["thickness"], args["occurrence_path"]
    where = {} if inputs.is_root(path) else {"occurrence_path": path}
    arcs = {"arcs": args["arcs"]} if args["arcs"] is not None else {}
    outline = run.tool("outline", "sketch_profile", action="closed_polyline", space="world", points=args["outline"],
                       **args["target"], **arcs, **where)
    token = outline["sketch"]["entity_token"]
    pilot = run.tool("pilot sketch", "sketch_profile", action="circles", sketch_entity_token=token, space="world",
                     circles=[{"center": list(args["pilot_center"]), "diameter": args["pilot_diameter"]}], **where)
    st.require_whole(run, pilot)
    if max((row.get("loop_count", 0) for row in pilot.get("profiles", [])), default=0) < 2:
        run.fail("the pilot is not inside the outline: no profile has it as a hole")

    named = {"body_name": args["body_name"]} if args["body_name"] else {}
    plate = run.tool("plate", "extrude_profile", sketch_token=token, profile_select="most_loops",
                     operation="new_body", extent={"type": "distance", "distance": thickness},
                     direction=list(args["direction"]), **named, **where)
    body, end = _new_body(run, plate, path), st.end_face(run, plate)
    normal = plate["extrude"]["direction_world"]  # What was built, a degree off 'direction' at most.

    seed = st.offset(args["first_bolt"], normal, thickness)
    center = st.offset(args["pilot_center"], normal, thickness)
    run.report.update(body=body, plate={"top_normal": normal, "pilot_center": center, "bolt_centers": None})
    sketch = st.circle_on(run, "seed sketch", end["token"], seed, args["bolt_diameter"], body)
    cut = st.extrude_at(run, "seed hole", sketch, seed, body, operation="cut", extent=st.cut_extent(None),
                        direction=st.reverse(normal))
    delta = _seed_delta(run, cut)
    bore = run.one("pilot bore", "face", surface_types=["cylinder"], concavity="concave",
                   diameter=st.band(args["pilot_diameter"], tolerance), bodies=[body],
                   axis={"direction": normal, "through": center, "through_tolerance": tolerance})
    pattern = run.tool("bolt pattern", "pattern_features", type="circular", axis={"face": bore["ref"]},
                       entities=[{"feature": cut["feature"]["name"]}], count=args["bolt_count"], total_angle_deg=360,
                       seed_point=seed, seed_volume_delta=delta)
    run.report["plate"]["bolt_centers"] = pattern.get("positions")
    if args["bolt_chamfer"]:
        rims = run.find("bolt rims", "edge", args["bolt_count"], curve_types=["circle"], bodies=[body],
                        diameter=st.band(args["bolt_diameter"], tolerance),
                        on_plane={"point": center, "normal": normal, "tolerance": tolerance})
        run.tool("bolt chamfer", "edge_treatment", kind="chamfer",
                 sets=[{"edges": [row["ref"] for row in rims], "size": args["bolt_chamfer"]}])


def flanged_plate(tools, /, **arguments):
    return st.run_macro("flanged_plate", tools, arguments, parse, plan, build)
