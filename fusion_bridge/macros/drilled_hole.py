"""A hole drilled into a planar face, chamfered or countersunk, then optionally threaded.

Drill first, then treat the rim, then thread: a chamfer adds a cone and leaves the
cylinder whole, and drilling through first means nothing abuts.

Arguments, in *units*, world coordinates:

- ``body``: the BodyRef to drill.
- ``point``: the hole's center, on the face.
- ``diameter``.
- ``normal``: optional, the face's outward normal, when *point* is on an edge of two faces.
- ``depth``: optional. Default through everything in ``body``.
- ``chamfer``: optional size of an equal-distance chamfer on the rim. Or:
- ``countersink_diameter`` with ``countersink_angle`` (included, in degrees).
- ``thread``: optional object of apply_thread's own keys, ``family``, ``designation`` and ``modeled`` required;
  also ``thread_class``, ``right_handed``, ``full_length``, ``length``, ``offset``, ``measured_from``.

Result, beyond ``steps.run_macro``'s: ``body`` and ``hole`` {center, normal, rim_on_face, rim_down_bore, resized}.
"""

import math

from ..common.args import reject_unknown
from . import inputs
from . import steps as st

_KNOWN = {"body", "point", "diameter", "normal", "depth", "chamfer", "countersink_diameter", "countersink_angle",
          "thread"}
_THREAD_KEYS = {"family", "designation", "modeled", "thread_class", "right_handed", "full_length", "length", "offset",
                "measured_from"}


def countersink_distances(diameter, countersink_diameter, angle_deg):
    """``(on the face, down the bore)``: a cone of *angle_deg* included, as a two-distance chamfer."""
    on_face = (countersink_diameter - diameter) / 2.0
    return on_face, on_face / math.tan(math.radians(angle_deg) / 2.0)


def _thread(arguments):
    thread = arguments.get("thread")
    if thread is None:
        return None
    if not isinstance(thread, dict):
        raise ValueError("'thread' must be an object")
    reject_unknown(thread, _THREAD_KEYS, "'thread' does not take")
    for key in ("family", "designation"):
        if not isinstance(thread.get(key), str) or not thread[key].strip():
            raise ValueError(f"'thread.{key}' is required")
    if not isinstance(thread.get("modeled"), bool):
        raise ValueError("'thread.modeled' is required: true or false")
    return thread


def _rim(arguments, args):
    """``(on the face, down the bore)``, or None for a bare hole."""
    chamfer = inputs.length(arguments, "chamfer", required=False)
    sink = inputs.length(arguments, "countersink_diameter", required=False)
    angle = inputs.length(arguments, "countersink_angle", required=False)
    if chamfer is not None and sink is not None:
        raise ValueError("give one of 'chamfer' and 'countersink_diameter', not both")
    if (sink is None) != (angle is None):
        raise ValueError("'countersink_diameter' and 'countersink_angle' go together")
    if sink is None:
        return None if chamfer is None else (chamfer, chamfer)
    if sink <= args["diameter"]:
        raise ValueError("'countersink_diameter' must be above 'diameter'")
    if angle >= 180.0:
        raise ValueError("'countersink_angle' must be below 180 degrees")
    return countersink_distances(args["diameter"], sink, angle)


def parse(arguments):
    args = inputs.start(arguments, _KNOWN)
    args.update(body=inputs.body(arguments, "body"), point=inputs.point(arguments, "point"),
                normal=inputs.direction(arguments, "normal", required=False),
                diameter=inputs.length(arguments, "diameter"), depth=inputs.length(arguments, "depth", required=False),
                thread=_thread(arguments), two_distances=arguments.get("countersink_diameter") is not None)
    args["rim"] = _rim(arguments, args)
    if args["rim"] is not None and args["depth"] is not None and args["rim"][1] >= args["depth"]:
        raise ValueError(f"the rim runs {args['rim'][1]:.6g} down the bore, past 'depth'")
    return args


def plan(args):
    labels = ["thread lookup"] if args["thread"] else []
    labels += ["face", "hole sketch", "hole"]
    if args["rim"]:
        labels += ["rim", "face at rim", "rim treatment"] if args["two_distances"] else ["rim", "rim treatment"]
    return labels + (["bore", "thread"] if args["thread"] else [])


def _treat_rim(run, args, body, normal):
    edge = st.rim(run, "rim", body, args["point"], normal, args["diameter"], args["tolerance"])
    on_face, down_bore = args["rim"]
    chosen = {"edges": [edge["ref"]], "size": on_face}
    if args["two_distances"]:
        # Refound here, not kept from the start: the cut may have replaced the face. Coplanar
        # faces elsewhere on the body are why this asks at the rim, not on the plane.
        top = st.plane_face_at(run, "face at rim", body, edge["ref"]["point"], normal, args["tolerance"])
        chosen.update(size_two=down_bore, size_on_face=top["ref"])
    treated = run.tool("rim treatment", "edge_treatment", kind="chamfer", sets=[chosen])
    for row in treated.get("measured", []):
        if row.get("verdict") == "not_measured":
            run.doubt("rim treatment: built, but nothing could measure it")


def _apply_thread(run, args, body, normal):
    """The bore as it stands now: the rim treatment moved its top edge down, and the nearest bore wins."""
    drop = args["rim"][1] if args["rim"] else 0.0
    reach = math.hypot(args["diameter"] / 2.0, drop) + args["tolerance"]
    bore = run.one("bore", "face", surface_types=["cylinder"], concavity="concave", bodies=[body],
                   diameter=st.band(args["diameter"], args["tolerance"]),
                   near={"point": list(args["point"]), "within": reach},
                   axis={"direction": normal, "through": list(args["point"]), "through_tolerance": args["tolerance"]})
    thread = run.tool("thread", "apply_thread", action="apply", internal=True, face=bore["ref"], **args["thread"])
    run.report["hole"]["resized"] = thread.get("resized")


def build(run, args):
    body, center, tolerance = args["body"], args["point"], args["tolerance"]
    if args["thread"]:
        # Read-only, and a mistyped designation is better found before the hole than after.
        run.tool("thread lookup", "apply_thread", action="catalog", internal=True, family=args["thread"]["family"],
                 designation=args["thread"]["designation"])
    face = st.plane_face_at(run, "face", body, center, args["normal"], tolerance)
    normal = face["normal"]
    sketch = st.circle_on(run, "hole sketch", face["ref"]["token"], center, args["diameter"], body)
    hole = st.extrude_at(run, "hole", sketch, center, body, operation="cut", extent=st.cut_extent(args["depth"]),
                         direction=st.reverse(normal))
    body = st.body_after(hole, body)
    rim = args["rim"] or (None, None)
    run.report.update(body=body, hole={"center": list(center), "normal": normal, "rim_on_face": rim[0],
                                       "rim_down_bore": rim[1], "resized": None})
    if args["rim"]:
        _treat_rim(run, args, body, normal)
    if args["thread"]:
        _apply_thread(run, args, body, normal)


def drilled_hole(tools, /, **arguments):
    return st.run_macro("drilled_hole", tools, arguments, parse, plan, build)
