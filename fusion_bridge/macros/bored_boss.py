"""A round boss on a planar face, bored and optionally chamfered at the rim.

Arguments, in *units*, world coordinates:

- ``body``: the BodyRef the boss joins.
- ``point``: the boss center, on the face.
- ``outer_diameter``, ``height``, ``bore_diameter``.
- ``normal``: optional, the face's outward normal, when *point* is on an edge of two faces.
- ``bore_depth``: optional, from the boss's end. Default through everything in ``body``.
- ``rim_chamfer``: optional size of a chamfer on the bore's rim.

Result, beyond ``steps.run_macro``'s: ``body`` and ``boss`` {center, top, normal}.
"""

from . import inputs
from . import steps as st

_KNOWN = {"body", "point", "outer_diameter", "height", "bore_diameter", "normal", "bore_depth", "rim_chamfer"}


def parse(arguments):
    args = inputs.start(arguments, _KNOWN)
    args.update(body=inputs.body(arguments, "body"), point=inputs.point(arguments, "point"),
                normal=inputs.direction(arguments, "normal", required=False),
                outer_diameter=inputs.length(arguments, "outer_diameter"), height=inputs.length(arguments, "height"),
                bore_diameter=inputs.length(arguments, "bore_diameter"),
                bore_depth=inputs.length(arguments, "bore_depth", required=False),
                rim_chamfer=inputs.length(arguments, "rim_chamfer", required=False))
    wall = (args["outer_diameter"] - args["bore_diameter"]) / 2.0
    if wall <= 0:
        raise ValueError("'bore_diameter' must be below 'outer_diameter': the bore would take the boss")
    if args["rim_chamfer"] is not None and args["rim_chamfer"] >= wall:
        raise ValueError(f"'rim_chamfer' must be below the boss's wall, {wall}")
    return args


def plan(args):
    chamfer = ["rim", "rim chamfer"] if args["rim_chamfer"] else []
    return ["face", "boss sketch", "boss", "bore sketch", "bore", *chamfer, "check"]


def build(run, args):
    body, center, tolerance = args["body"], args["point"], args["tolerance"]
    face = st.plane_face_at(run, "face", body, center, args["normal"], tolerance)
    normal = face["normal"]
    sketch = st.circle_on(run, "boss sketch", face["ref"]["token"], center, args["outer_diameter"], body)
    boss = st.extrude_at(run, "boss", sketch, center, body, operation="join",
                         extent={"type": "distance", "distance": args["height"]}, direction=normal)
    end, body = st.end_face(run, boss), st.body_after(boss, body)

    top = st.offset(center, normal, args["height"])
    run.report.update(body=body, boss={"center": list(center), "top": top, "normal": normal})
    sketch = st.circle_on(run, "bore sketch", end["token"], top, args["bore_diameter"], body)
    st.extrude_at(run, "bore", sketch, top, body, operation="cut", extent=st.cut_extent(args["bore_depth"]),
                  direction=st.reverse(normal))
    if args["rim_chamfer"]:
        edge = st.rim(run, "rim", body, top, normal, args["bore_diameter"], tolerance)
        run.tool("rim chamfer", "edge_treatment", kind="chamfer", sets=[{"edges": [edge["ref"]],
                                                                         "size": args["rim_chamfer"]}])
    run.check("check", [body], boss["baseline_token"], inputs.path_of(body))


def bored_boss(tools, /, **arguments):
    return st.run_macro("bored_boss", tools, arguments, parse, plan, build)
