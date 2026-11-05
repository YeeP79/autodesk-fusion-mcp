"""A flat gusset between two faces, coped to what they belong to.

A face is a plane or a cylinder, so a tube or a plate. A plate outline runs past
the corner into both bodies, and the bodies cut it with their tools kept: an exact
cope with no spline sketching. Against a plane the corner lies on the plane. Against
a tube it lies on the tube's axis, so a leg is measured from the axis.

- Two planes: the corner is on the line where they meet. The gusset stands across
  that line, so ``point_a`` and ``point_b`` must lie in one plane across it.
- Two cylinders: the corner is where the axes meet, and the gusset contains both.
- A plane and a cylinder: the corner is where the axis meets the plane, and the
  leg on the plane runs toward the plane's point.

Each leg runs from the corner toward its point. A plane's outward side must face the
other leg: an outside corner is refused.

Arguments, in *units*, world coordinates:

- ``body_a``, ``body_b``: BodyRefs in one occurrence, or one body twice.
- ``point_a``, ``point_b``: on each face.
- ``leg_a``, ``leg_b``: leg lengths from the corner.
- ``thickness``: centered on the gusset's plane.
- ``overlap``: how far the outline runs back past the corner, along the bisector. Keep it
  inside the bodies: past the far side of a plate it leaves a stray piece.
- ``fillet_radius`` with ``fillet_points``: optional, world points on edges of the finished gusset.
- ``body_name``: default ``Gusset``. ``plane_name``: default ``<body_name> plane``.

Result, beyond ``steps.run_macro``'s: ``body``, ``stray_bodies`` and ``gusset``
{corner, dir_a, dir_b, normal, tip_a, tip_b}. A cut can leave pieces, as in a hollow tube's
bore. The largest is the gusset and the rest stay, listed, and make the status ``suspect``.
"""

import math

from ..common.geometry import COLLINEAR_SINE, add, along, cross, dot, scaled, sub, unit
from . import inputs
from . import steps as st

_KNOWN = {"body_a", "body_b", "point_a", "point_b", "leg_a", "leg_b", "thickness", "overlap", "fillet_radius",
          "fillet_points", "body_name", "plane_name"}
_SURFACES = ["plane", "cylinder"]


def _length(vector):
    return math.sqrt(dot(vector, vector))


def _toward(vector, corner, point, where, tolerance):
    """*vector*, signed to run from *corner* toward *point*."""
    reach = dot(sub(point, corner), vector)
    if abs(reach) <= tolerance:
        raise ValueError(f"{where} is at the corner, so it gives its leg no direction")
    return vector if reach > 0 else scaled(vector, -1.0)


def _outward(leg, normal, where):
    if dot(leg, normal) <= 0:
        raise ValueError(f"the other leg runs behind {where}: this is an outside corner")


def _two_planes(a, b, point_a, point_b, tolerance):
    line = cross(a["normal"], b["normal"])
    if _length(line) <= COLLINEAR_SINE:
        raise ValueError("the faces are parallel: they share no corner")
    axis = unit(line)
    apart = abs(dot(sub(point_a, point_b), axis))
    if apart > tolerance:
        raise ValueError(f"'point_a' and 'point_b' are {apart:.6g} apart along the corner line: "
                         "the gusset stands across it, so they must lie in one plane across it")
    base = scaled(add(scaled(cross(b["normal"], line), dot(a["normal"], a["point"])),
                      scaled(cross(line, a["normal"]), dot(b["normal"], b["point"]))), 1.0 / dot(line, line))
    corner = along(base, axis, dot(sub(point_a, base), axis))
    dir_a = _toward(cross(axis, a["normal"]), corner, point_a, "'point_a'", tolerance)
    dir_b = _toward(cross(axis, b["normal"]), corner, point_b, "'point_b'", tolerance)
    _outward(dir_b, a["normal"], "face a")
    _outward(dir_a, b["normal"], "face b")
    return corner, dir_a, dir_b


def _two_axes(a, b, point_a, point_b, tolerance):
    line = cross(a["axis"], b["axis"])
    if _length(line) <= COLLINEAR_SINE:
        raise ValueError("the cylinders' axes are parallel: they share no corner")
    between = sub(b["origin"], a["origin"])
    gap = abs(dot(between, unit(line)))
    if gap > tolerance:
        raise ValueError(f"the cylinders' axes miss each other by {gap:.6g}, so no flat gusset contains both")
    span = dot(line, line)
    on_a = along(a["origin"], a["axis"], dot(cross(between, b["axis"]), line) / span)
    on_b = along(b["origin"], b["axis"], dot(cross(between, a["axis"]), line) / span)
    corner = scaled(add(on_a, on_b), 0.5)
    return (corner, _toward(a["axis"], corner, point_a, "'point_a'", tolerance),
            _toward(b["axis"], corner, point_b, "'point_b'", tolerance))


def _plane_and_axis(plane, cylinder, plane_point, axis_point, tolerance):
    """``(corner, leg on the plane, leg on the axis)``."""
    rise = dot(cylinder["axis"], plane["normal"])
    if abs(rise) <= COLLINEAR_SINE:
        raise ValueError("the cylinder's axis never meets the plane")
    height = dot(sub(plane["point"], cylinder["origin"]), plane["normal"])
    corner = along(cylinder["origin"], cylinder["axis"], height / rise)
    on_axis = _toward(cylinder["axis"], corner, axis_point, "the cylinder's point", tolerance)
    reach = sub(plane_point, corner)
    flat = sub(reach, scaled(plane["normal"], dot(reach, plane["normal"])))
    if _length(flat) <= tolerance:
        raise ValueError("the plane's point is at the corner, so it gives its leg no direction")
    _outward(on_axis, plane["normal"], "the plane")
    return corner, unit(flat), on_axis


def corner_frame(face_a, face_b, point_a, point_b, tolerance):
    """``{"corner", "dir_a", "dir_b", "normal"}`` for two surfaces; raises ValueError.

    A surface is ``{"type": "plane", "point", "normal"}`` with its outward unit normal,
    or ``{"type": "cylinder", "origin", "axis"}`` with a unit axis.
    """
    kinds = (face_a["type"], face_b["type"])
    if kinds == ("plane", "plane"):
        corner, dir_a, dir_b = _two_planes(face_a, face_b, point_a, point_b, tolerance)
    elif kinds == ("cylinder", "cylinder"):
        corner, dir_a, dir_b = _two_axes(face_a, face_b, point_a, point_b, tolerance)
    elif kinds == ("plane", "cylinder"):
        corner, dir_a, dir_b = _plane_and_axis(face_a, face_b, point_a, point_b, tolerance)
    else:
        corner, dir_b, dir_a = _plane_and_axis(face_b, face_a, point_b, point_a, tolerance)
    return {"corner": corner, "dir_a": dir_a, "dir_b": dir_b, "normal": unit(cross(dir_a, dir_b))}


def plate_outline(frame, leg_a, leg_b, overlap):
    """The oversize triangle: back past the corner along the bisector, then the two leg tips."""
    corner = frame["corner"]
    back = unit(add(frame["dir_a"], frame["dir_b"]))
    return [along(corner, back, -overlap), along(corner, frame["dir_a"], leg_a), along(corner, frame["dir_b"], leg_b)]


def _surface(row):
    """What ``corner_frame`` needs of a find_entities row."""
    if row["type"] == "plane":
        return {"type": "plane", "point": tuple(row["ref"]["point"]), "normal": unit(row["normal"])}
    return {"type": "cylinder", "origin": tuple(row["axis_origin"]), "axis": unit(row["axis_direction"])}


def _fillet(arguments):
    radius = inputs.length(arguments, "fillet_radius", required=False)
    points = arguments.get("fillet_points")
    if (radius is None) != (points is None):
        raise ValueError("'fillet_radius' and 'fillet_points' go together")
    if points is None:
        return None, None
    if not isinstance(points, list) or not points:
        raise ValueError("'fillet_points' must be a non-empty array of points")
    return radius, [list(inputs.point({"fillet_points": item}, "fillet_points")) for item in points]


def _name(arguments, key, default):
    value = arguments.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string")
    return value


def parse(arguments):
    args = inputs.start(arguments, _KNOWN)
    args.update({key: inputs.body(arguments, key) for key in ("body_a", "body_b")})
    args.update({key: inputs.point(arguments, key) for key in ("point_a", "point_b")})
    args.update({key: inputs.length(arguments, key) for key in ("leg_a", "leg_b", "thickness", "overlap")})
    if inputs.path_of(args["body_a"]) != inputs.path_of(args["body_b"]):
        raise ValueError("'body_a' and 'body_b' must share an occurrence: a Combine cannot reach across two")
    args["fillet_radius"], args["fillet_points"] = _fillet(arguments)
    args["body_name"] = _name(arguments, "body_name", "Gusset")
    args["plane_name"] = _name(arguments, "plane_name", f"{args['body_name']} plane")
    return args


def plan(args):
    fillet = ["fillet"] if args["fillet_radius"] else []
    return ["face a", "face b", "plane", "outline", "plate", "cope", *fillet, "check"]


def _largest(rows):
    return max(rows, key=lambda row: row.get("volume") or 0.0)


def _find_face(run, label, body, point, tolerance):
    return run.one(label, "face", surface_types=_SURFACES, bodies=[body],
                   near={"point": list(point), "within": tolerance})


def build(run, args):
    tolerance, body_a, body_b = args["tolerance"], args["body_a"], args["body_b"]
    face_a = _find_face(run, "face a", body_a, args["point_a"], tolerance)
    face_b = _find_face(run, "face b", body_b, args["point_b"], tolerance)
    try:
        frame = corner_frame(_surface(face_a), _surface(face_b), args["point_a"], args["point_b"], tolerance)
    except ValueError as exc:
        run.refuse("corner", str(exc))

    outline = plate_outline(frame, args["leg_a"], args["leg_b"], args["overlap"])
    run.report["gusset"] = {**{key: list(value) for key, value in frame.items()},
                            "tip_a": list(outline[1]), "tip_b": list(outline[2])}
    where = st.placement(body_a)
    plane = run.tool("plane", "construction_plane", mode="point_normal", name=args["plane_name"],
                     point=list(frame["corner"]), normal=list(frame["normal"]), **where)
    sketch = run.tool("outline", "sketch_profile", action="closed_polyline", space="world",
                      plane_entity_token=plane["plane"]["token"], points=[list(p) for p in outline], **where)
    plate = run.tool("plate", "extrude_profile", sketch_token=sketch["sketch"]["entity_token"], profile_select="only",
                     operation="new_body", extent={"type": "symmetric", "distance": args["thickness"],
                                                   "full_length": True},
                     body_name=args["body_name"], **where)
    made = [row["ref"] for row in plate.get("bodies", {}).get("created", [])
            if row["ref"]["occurrence_path"] == inputs.path_of(body_a)]
    if len(made) != 1:
        run.fail(f"expected one new body, the extrude reports {len(made)}")

    tools = [body_a] if body_a == body_b else [body_a, body_b]
    cope = run.tool("cope", "combine_bodies", target=made[0], tools=tools, operation="cut", keep_tools=True,
                    result_name=args["body_name"])
    rows = cope.get("result", {}).get("bodies", [])
    if not rows:
        run.fail("the cope left no body")
    gusset = _largest(rows)
    strays = [row["ref"] for row in rows if row is not gusset]
    run.report.update(body=gusset["ref"], stray_bodies=strays)
    if strays:
        run.doubt(f"cope: the cut left {len(strays)} stray piece(s) beside the gusset: {strays}")
    if args["fillet_radius"]:
        run.tool("fillet", "edge_treatment", kind="fillet", tolerance=tolerance,
                 sets=[{"points": args["fillet_points"], "body": gusset["ref"], "size": args["fillet_radius"]}])
    run.check("check", [gusset["ref"]], plate["baseline_token"], inputs.path_of(body_a),
              pairs=[(gusset["ref"], tool) for tool in tools])


def gusset(tools, /, **arguments):
    return st.run_macro("gusset", tools, arguments, parse, plan, build)
