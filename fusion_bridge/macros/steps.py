"""One macro run: each primitive called in turn, recorded, and stopped at the first that is not ok.

``Run.tool`` returns a payload only when it is ok. Otherwise it raises ``Stop``,
so a macro reads as a straight line and cannot walk past a refusal. Nothing is
undone on the way out: one failed step must not cost the steps before it, so a
macro keeps what it built and ``built`` lists it.

``status`` follows section 1 of the design, read across the whole run:

- ``built``: every step ran and was ok. The only status with ``ok`` true.
- ``refused``: the arguments or a step stopped it and nothing was left behind.
- ``partial``: it stopped with something left behind, or the last step was partial.
- ``suspect``: every step ran, but a check disagrees, so read ``warnings``.
- ``failed``: a step or the macro raised, so what changed is unknown past ``built``.
"""

import traceback

import adsk.core

from ..common.geometry import along, unit_labels
from ..dispatch import log
from . import inputs

# What each tool's step row keeps. The whole payload is the tool's to give; a row is evidence.
_KEYS = {
    "find_entities": ("matched", "returned", "truncated", "scan_capped"),
    "sketch_profile": ("sketch", "profile_count", "circles"),
    "extrude_profile": ("feature", "bodies", "extrude", "join_route", "targets_unchanged", "in_path_not_targeted",
                        "collateral"),
    "edge_treatment": ("features", "bodies", "edges", "measured", "suspects"),
    "pattern_features": ("feature", "bodies", "pattern", "positions", "positions_basis", "collateral"),
    "combine_bodies": ("feature", "bodies", "result", "stale_refs", "collateral"),
    "construction_plane": ("feature", "plane", "sign", "reused", "anchors"),
    "apply_thread": ("feature", "bodies", "thread", "face", "volume_delta", "resized", "classes"),
    "check_solids": ("summary", "problems"),
}
_WHY_KEYS = ("refusals", "problems", "error")
# A step in one of these stands in the design.
_STANDING = ("built", "suspect", "partial")
_MAX_RESULTS = 1000


def _nothing_built():
    return {"sketches": [], "planes": [], "anchors": [], "features": [], "bodies": []}


class Stop(Exception):
    """The run ends here; ``Run.stopped`` says why."""


def why(payload):
    """A step's own words for not being ok."""
    for key in _WHY_KEYS:
        value = payload.get(key)
        if value:
            return "; ".join(str(item) for item in value) if isinstance(value, list) else str(value)
    return "; ".join(str(item) for item in payload.get("warnings") or []) or f"status {payload.get('status')}"


def _row(label, name, payload):
    row = {"step": label, "name": name, "status": payload.get("status"), "ok": bool(payload.get("ok")),
           "result": {key: payload[key] for key in _KEYS.get(name, ()) if key in payload}}
    if name == "find_entities":
        row["result"]["labels"] = [item.get("label") for item in payload.get("entities", [])]
    if payload.get("warnings"):
        row["warnings"] = list(payload["warnings"])
    if not row["ok"] and name != "check_solids":
        row["why"] = why(payload)
    return row


class Run:
    def __init__(self, name, tools, units, plan):
        self.name, self.units = name, units
        self.steps, self.doubts, self.report = [], [], {}
        self.built = _nothing_built()
        self.stopped, self.crash, self.baseline_token = None, None, None
        self._tools, self._plan = tools, plan

    # -- Calling ---------------------------------------------------------------

    def _call(self, label, name, kwargs):
        payload = getattr(self._tools, name)(units=self.units, **kwargs)
        self.steps.append(_row(label, name, payload))
        self._collect(name, payload)
        return payload

    def tool(self, label, name, /, **kwargs):
        """One primitive; its payload when ok, else ``Stop``."""
        payload = self._call(label, name, kwargs)
        if not payload.get("ok"):
            self._halt(label, name, payload.get("status"), why(payload))
        return payload

    def find(self, label, entity, count, /, **filters):
        """Exactly *count* entities from ``find_entities``, or ``Stop``.

        A macro acting on the wrong number of faces builds the wrong part without
        looking wrong. ``max_results`` is the tool's largest, so ``matched`` is a
        count and not a page.
        """
        payload = self.tool(label, "find_entities", entity=entity, max_results=_MAX_RESULTS, **filters)
        if payload.get("scan_capped") or payload.get("truncated"):
            self.fail(f"the search did not finish, so its {payload.get('matched')} match(es) may not be all of them")
        if payload.get("matched") != count:
            found = [item.get("label") for item in payload.get("entities", [])][:5]
            self.fail(f"expected {count} match(es), found {payload.get('matched')}: {found}")
        return payload["entities"]

    def one(self, label, entity, /, **filters):
        return self.find(label, entity, 1, **filters)[0]

    def check(self, label, bodies, baseline_token, edited, pairs=None):
        """``check_solids`` over *bodies*. A finished check that found problems is a doubt, not a stop."""
        kwargs = {"bodies": [_check_ref(ref) for ref in bodies],
                  "checks": ["validity", "slivers", "lumps", "volume_change"], "baseline_token": baseline_token,
                  "edited_component": edited}
        if pairs:
            kwargs["checks"] = kwargs["checks"] + ["interference"]
            kwargs["interference_pairs"] = [[_check_ref(a), _check_ref(b)] for a, b in pairs]
        payload = self._call(label, "check_solids", kwargs)
        if payload.get("status") != "done":
            self._halt(label, "check_solids", payload.get("status"), why(payload))
        if not payload.get("ok"):
            self.doubt(f"{label}: check_solids found problems: {payload.get('summary')}")
        return payload

    # -- Ending ----------------------------------------------------------------

    def doubt(self, text):
        """Something a finished run must not read as success."""
        self.doubts.append(text)

    def fail(self, text):
        """The last step answered, and its answer is one the macro cannot go on from."""
        row = self.steps[-1]
        row.update(ok=False, why=text)
        self._halt(row["step"], row["name"], row["status"], text)

    def refuse(self, label, text):
        """The macro's own arithmetic refuses, with no tool involved."""
        self._halt(label, None, "refused", text)

    def _halt(self, label, name, status, text):
        self.stopped = {"step": label, "name": name, "status": status, "why": text}
        raise Stop()

    def crashed(self, exc):
        log(f"[MCP] macros.{self.name} failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        self.crash = {"error": f"Error: {exc}", "traceback": traceback.format_exc()}

    # -- Reading what stands ---------------------------------------------------

    def _collect(self, name, payload):
        status, built = payload.get("status"), self.built
        sketch = payload.get("sketch") if name == "sketch_profile" else None
        if sketch and payload.get("ok") and not payload.get("added_to_existing"):
            built["sketches"].append({"name": sketch.get("name"), "entity_token": sketch.get("entity_token"),
                                      "component": sketch.get("component")})
        built["anchors"] += payload.get("anchors") or []  # Anchors stay even when their plane was refused.
        feature = payload.get("feature")
        if status in _STANDING:
            built["features"] += [row for row in (feature, payload.get("combine_feature")) if row]
            built["features"] += payload.get("features") or []
            built["bodies"] += (payload.get("bodies") or {}).get("created", [])
            if payload.get("plane"):
                built["planes"].append({**payload["plane"], "reused": bool(payload.get("reused"))})
        elif status == "refused" and feature:
            # A feature that could not be taken back: refused, and still in the timeline.
            built["features"].append({**feature, "not_taken_back": True})
        token = payload.get("baseline_token")
        if status in _STANDING and token and self.baseline_token is None:
            self.baseline_token = token

    def _left_behind(self):
        return {key: len(rows) for key, rows in self.built.items() if rows}

    def _not_run(self):
        ran = {row["step"] for row in self.steps}
        return [label for label in self._plan if label not in ran]

    def _status(self, not_run):
        if self.crash or (self.stopped and self.stopped["status"] == "failed"):
            return "failed"
        if self.stopped is None:
            return "suspect" if self.doubts else "built"
        if self.stopped["status"] == "suspect" and not not_run:
            return "suspect"
        return "partial" if self._left_behind() else "refused"

    def _warnings(self):
        warnings = list(self.doubts)
        for row in self.steps:
            for text in row.get("warnings", []):
                line = f"{row['step']}: {text}"
                if line not in warnings:
                    warnings.append(line)
        return warnings

    def result(self):
        not_run = self._not_run()
        status = self._status(not_run)
        payload = {"macro": self.name, "status": status, "ok": status == "built", "units": unit_labels(self.units),
                   "built": self.built, "steps": self.steps, "stopped_at": self.stopped, "not_run": not_run,
                   "baseline_token": self.baseline_token, "warnings": self._warnings(), **self.report}
        if status == "refused":
            payload["refusals"] = [self.stopped["why"]]
        if status not in ("built", "refused"):
            counts = self._left_behind().items()
            left = ", ".join(f"{count} {key}" for key, count in counts) or "nothing it knows of"
            payload["kept"] = f"nothing was undone: the design keeps {left}"
        if status == "failed":
            payload["kept"] += ", and the last step may have changed more"
        return {**payload, **(self.crash or {})}


def _check_ref(ref):
    """check_solids' refs take no ``body_index``; bodies sharing a name are checked together."""
    return {"occurrence_path": ref["occurrence_path"], "body_name": ref["body_name"]}


def refused(name, units, text):
    """The result of arguments refused before any step."""
    return {"macro": name, "status": "refused", "ok": False, "units": unit_labels(units), "built": _nothing_built(),
            "steps": [], "stopped_at": None, "not_run": [], "baseline_token": None, "warnings": [], "refusals": [text]}


def run_macro(name, tools, arguments, parse, plan, build):
    """Validate, then run *build*; always returns a result, and never loses the steps it took."""
    try:
        args = parse(arguments)
    except ValueError as exc:
        units = arguments.get("units")
        return refused(name, units if isinstance(units, str) and units in ("in", "cm", "mm") else "in", str(exc))
    run = Run(name, tools, args["units"], plan(args))
    try:
        build(run, args)
    except Stop:
        pass
    except Exception as exc:
        run.crashed(exc)
    return run.result()


# -- Steps more than one macro takes ----------------------------------------------


def placement(body):
    """The keys that put a sketch or feature in *body*'s occurrence; none in root."""
    path = inputs.path_of(body)
    return {} if inputs.is_root(path) else {"occurrence_path": path}


def band(diameter, tolerance):
    """A diameter filter. Reported diameters are rounded, so an exact min and max can miss."""
    return {"min": diameter - tolerance, "max": diameter + tolerance}


def plane_face_at(run, label, body, point, normal, tolerance):
    """The one planar face of *body* at *point*, outward along *normal* if one is given."""
    filters = {"surface_types": ["plane"], "near": {"point": list(point), "within": tolerance}, "bodies": [body]}
    if normal is not None:
        filters["normal"] = {"direction": list(normal)}
    return run.one(label, "face", **filters)


def circle_on(run, label, token, center, diameter, body):
    """One circle at a world *center* on a plane or face, whole or ``Stop``.

    A circle over a face's edge is cut by the edges Fusion projects into the
    sketch, so its profile is a clipped piece: the wrong shape, and no error.
    """
    payload = run.tool(label, "sketch_profile", action="circles", plane_entity_token=token, space="world",
                       circles=[{"center": list(center), "diameter": diameter}], **placement(body))
    require_whole(run, payload)
    return payload


def require_whole(run, sketch):
    if not all(row.get("whole") is True for row in sketch.get("circles", [])):
        run.fail("the circle is not whole in its sketch: it crosses the edge of the face or another curve")


def extrude_at(run, label, sketch, center, body, **kwargs):
    """An extrude of the profile holding *center*.

    Never ``only``: a sketch on a face holds the face's projected edges as well,
    so it has several profiles.
    """
    return run.tool(label, "extrude_profile", sketch_token=sketch["sketch"]["entity_token"],
                    profile_select="containing_point", containing_point=list(center), target_bodies=[body],
                    **placement(body), **kwargs)


def end_face(run, extrude):
    """The one face an extrude ended on."""
    faces = extrude.get("faces", {}).get("end", [])
    if len(faces) != 1:
        run.fail(f"expected the extrude to end on one face, it lists {len(faces)}")
    return faces[0]


def body_after(extrude, body):
    """*body*, as the extrude that changed it reports it."""
    rows = [row for row in extrude.get("bodies", {}).get("touched", []) if row.get("role") == "target"]
    return rows[0]["ref"] if rows else body


def rim(run, label, body, center, normal, diameter, tolerance):
    """The one circular edge of *diameter* around *center* on the plane through it."""
    return run.one(label, "edge", curve_types=["circle"], diameter=band(diameter, tolerance),
                   on_plane={"point": list(center), "normal": list(normal), "tolerance": tolerance},
                   axis={"direction": list(normal), "through": list(center), "through_tolerance": tolerance},
                   bodies=[body])


def cut_extent(depth):
    return {"type": "through_all"} if depth is None else {"type": "distance", "distance": depth}


def offset(point, direction, distance):
    return list(along(point, direction, distance))


def reverse(direction):
    return [-c + 0.0 for c in direction]
