"""Repeat features, bodies or faces and prove how many came out: pattern_features.

A pattern is easy to get silently wrong in both directions. Left unset, direction
two is not one instance: a quantity of 2 built six bodies (Build line 1516), so
it is always set, on a placeholder axis with quantity 1 when there is no second
direction. And a cut makes no bodies, so a body count says nothing about a
patterned bore, while volume alone cannot tell a missed instance from a short
pattern. The result therefore carries ``elements`` (what Fusion made),
``bodies_made``, and ``instances_reached`` (the volume change over the seed's
own), and any of them disagreeing with what was asked is ``suspect``.

Positions are predicted by the pure functions below. Once built they are measured
from each element's faces. ``PatternElement.transform`` is not used: on a 6-hole
circular pattern placed correctly it read 0, 120, 180, 240, 300 and 0 degrees, and
never 60. Element 0 is taken as the seed, and faces that cannot be read leave the
prediction standing with no position check. The sense of an edge direction
belongs to Fusion, so it is asked of the input; the sense of rotation about an
edge or a face is not available before the add.

A pattern lands in the seeds' component, whose axes ``axis: x|y|z`` means, and
direction entities must be in that component too. Inside it Fusion works on
native entities, so proxies are unwrapped and vectors read in component space
go through the occurrence's frame on the way out. A bystander reached by an
instance is taken back by ``on_collateral``: the seed's bodies are the only targets.

Everything above ``FusionPattern`` takes duck-typed collaborators, so fakes can
drive it. Internal values are cm and degrees.
"""

import math
import traceback

import adsk.core
import adsk.fusion

from .common import feature_ops as fo
from .common.api import attempt, items, safe, strict_items
from .common.args import bool_arg, error_pair, int_arg, is_finite_number, reject_unknown, require_object, text_arg
from .common.bodies import (
    ROOT_PATH,
    SURFACE_TYPES,
    bbox,
    find_occurrence,
    label,
    native,
    object_type,
    occurrence_frame,
    parse_body_ref,
    resolve_targets,
)
from .common.entity_refs import (
    CURVE_TYPES,
    REF_TOLERANCE_CM,
    FusionScene,
    bbox_distance,
    body_ref,
    make_ref,
    owner_entry,
    parse_ref,
    resolve,
    resolved_row,
    same_entity,
    unique_entities,
)
from .common.geometry import (
    CM_PER_UNIT,
    UNIT_FACTORS,
    add,
    along,
    direction_from_local,
    display_direction,
    display_point,
    dot,
    from_local,
    rotate_about,
    sub,
    to_cm,
    to_units,
    unit,
    xyz,
)
from .common.results import error_result
from .common.timeline import find_row, read_rows
from .dispatch import get_app, log

_RECTANGULAR_ONLY = {"direction_one", "count_one", "spacing_one", "spacing_one_expression", "direction_two",
                     "count_two", "spacing_two", "spacing_two_expression"}
_CIRCULAR_ONLY = {"axis", "count", "total_angle_deg", "total_angle_expression", "symmetric"}
_SHARED = {"type", "entities", "compute", "seed_point", "seed_volume_delta", "feature_name", "on_collateral",
           "units", "dry_run"}

TYPES = ("rectangular", "circular")
COMPUTE = {"adjust": "AdjustPatternCompute", "identical": "IdenticalPatternCompute",
           "optimized": "OptimizedPatternCompute"}
ON_COLLATERAL = ("undo", "keep")
AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
_SEED_KEYS = ("feature", "feature_token", "body", "face")
_DIRECTION_KEYS = ("axis", "edge", "sketch_line_token")
_ROTATION_KEYS = ("axis", "edge", "face")

MAX_COUNT = 1000
FULL_TURN_DEG = 360.0
_ANGLE_EPSILON = 1e-9
PARALLEL_DOT = 1.0 - 1e-9
# Instances are whole: further than this from a whole number, one only partly reached material.
REACHED_DIGITS = 2
REACHED_TOLERANCE = 0.05
# adsk.fusion.FeatureOperations values.
_NEW_BODY_OPERATION = 3
_ADDING_OPERATIONS = (0, 3, 4)  # join, new body, new component: nothing to miss outside the target
_AXIS_SURFACES = ("cylinder", "cone", "torus")


# -- Pure logic -----------------------------------------------------------------


def unused_axis(direction):
    """The component axis furthest from *direction*: the placeholder for direction two.

    Quantity 1 moves nothing, so which one means nothing, but it must not be
    parallel to direction one. Ties go to the first in x, y, z.
    """
    wanted = unit(direction)
    return min(AXES, key=lambda name: abs(dot(wanted, AXES[name])))


def parallel(a, b):
    """Are two directions parallel, either sense? A vector with no length is parallel to nothing."""
    a, b = unit(a), unit(b)
    return a is not None and b is not None and abs(dot(a, b)) >= PARALLEL_DOT


def rectangular_positions(seed, one, count_one, spacing_one, two=None, count_two=1, spacing_two=0.0):
    """Where each instance of a grid lands; *one* and *two* are unit vectors, spacing may be negative."""
    rows = [along(seed, one, i * spacing_one) for i in range(count_one)]
    if two is None:
        return rows
    return [along(row, two, j * spacing_two) for j in range(count_two) for row in rows]


def is_full_turn(total_deg):
    return abs(total_deg) >= FULL_TURN_DEG - _ANGLE_EPSILON


def circular_angles(count, total_deg, symmetric=False):
    """Each instance's angle from the seed. A full turn divides by n, a partial total by n - 1.

    A negative total runs the other way. ``symmetric`` centers a partial total on the seed.
    """
    if count == 1:
        return [0.0]
    full = is_full_turn(total_deg)
    step = total_deg / (count if full else count - 1)
    start = -total_deg / 2.0 if symmetric and not full else 0.0
    return [start + i * step for i in range(count)]


def circular_positions(seed, origin, axis, count, total_deg, symmetric=False):
    """Where each instance lands, right-handed about unit *axis* through *origin*."""
    offset = sub(seed, origin)
    return [add(origin, rotate_about(offset, axis, angle)) for angle in circular_angles(count, total_deg, symmetric)]


def instances_reached(volume_change, seed_delta):
    """``volume_change / seed_delta + 1``, or None without a seed delta. The seed was made before the pattern."""
    if not seed_delta or volume_change is None:
        return None
    reached = round(volume_change / seed_delta + 1.0, REACHED_DIGITS)
    return int(reached) if reached == int(reached) else reached


def count_problems(asked, elements, reached, bodies_made, bodies_expected):
    """Why the pattern is not what was asked for. *reached* and *bodies_expected* may be None."""
    problems = []
    if elements is None:
        problems.append("the pattern reports no element count, so nothing proves how many were made")
    elif elements != asked:
        problems.append(f"asked for {asked} instances, Fusion made {elements} elements")
    if reached is not None and abs(reached - asked) > REACHED_TOLERANCE:
        problems.append(f"asked for {asked} instances, but the volume change says {reached} reached material")
    if bodies_expected is not None and bodies_made != bodies_expected:
        problems.append(f"expected {bodies_expected} new bodies, the pattern made {bodies_made}")
    return problems


def element_marker(faces):
    """``(point, exact)``: a point that moves with an element's *faces*, or None when they cannot be read.

    A lone cylinder gives its axis origin, right under any motion. Anything else
    gives the center of its box, which a rotation shifts unless the shape is round,
    so it is not *exact*.
    """
    if not faces:
        return None
    if len(faces) == 1:
        g = safe(faces[0], "geometry")
        if SURFACE_TYPES.get(safe(g, "surfaceType")) == "cylinder":
            origin = attempt(lambda: xyz(g.origin))
            return None if origin is None else (origin, True)
    boxes = [bbox(face) for face in faces]
    if None in boxes:
        return None
    low = [min(box[0][i] for box in boxes) for i in range(3)]
    high = [max(box[1][i] for box in boxes) for i in range(3)]
    return tuple((a + b) / 2.0 for a, b in zip(low, high)), False


def measured_positions(seed, markers):
    """Each instance as *seed* plus how far its marker sits from element 0's."""
    return [add(seed, sub(marker, markers[0])) for marker in markers]


def same_positions(predicted, built, tolerance_cm=REF_TOLERANCE_CM):
    """Does every predicted position have a built one, in any order?"""
    if len(predicted) != len(built):
        return False
    return all(any(math.dist(p, b) <= tolerance_cm for b in built) for p in predicted)


def outside_boxes(positions, boxes, tolerance_cm=REF_TOLERANCE_CM):
    """The positions in none of *boxes*: an instance there would cut nothing."""
    known = [box for box in boxes if box is not None]
    if not known:
        return []
    return [p for p in positions if all(bbox_distance(box, p) > tolerance_cm for box in known)]


def occurrence_volume(records, path):
    """Total volume of one occurrence's bodies, so instances of a component are not counted twice."""
    return sum(rec["volume"] or 0.0 for rec in records.values() if rec["occurrence_path"] == path)


def is_feature(entity):
    """A timeline item can also be a sketch, a plane or a group."""
    return entity is not None and object_type(entity).endswith("Feature")


def edge_axis(edge, round_ok=False):
    """``(origin, direction)`` of a native edge, in its component; None when it defines no axis.

    A straight edge runs from start vertex to end vertex. *round_ok* lets a circle
    or arc stand for its normal through its center, as a rotation axis.
    """
    g = safe(edge, "geometry")
    kind = CURVE_TYPES.get(safe(g, "curveType"))
    if kind == "line":
        start = attempt(lambda: xyz(edge.startVertex.geometry))
        end = attempt(lambda: xyz(edge.endVertex.geometry))
        return None if start is None or end is None else (start, sub(end, start))
    if round_ok and kind in ("circle", "arc"):
        return xyz(g.center), xyz(g.normal)
    return None


def face_axis(face):
    """``(origin, direction)`` of a native face with an axis; None for anything else."""
    g = safe(face, "geometry")
    if SURFACE_TYPES.get(safe(g, "surfaceType")) not in _AXIS_SURFACES:
        return None
    return xyz(g.origin), xyz(g.axis)


# -- Argument validation --------------------------------------------------------


def _one_key(value, keys, where):
    if not isinstance(value, dict) or len(value) != 1 or next(iter(value)) not in keys:
        raise ValueError(f"{where} must be an object with exactly one of {list(keys)}")
    return next(iter(value.items()))


def _parse_seed(value, units, where):
    key, item = _one_key(value, _SEED_KEYS, where)
    if key in ("feature", "feature_token"):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where} '{key}' must be a non-empty string")
        return {"kind": "feature", key: item}
    if key == "body":
        return {"kind": "body", "ref": parse_body_ref(item, f"{where} 'body'")}
    return {"kind": "face", "ref": _parse_entity(item, units, f"{where}.face", "face")}


def _parse_entity(value, units, where, kind):
    ref = parse_ref(value, units, where)
    if ref["kind"] not in (None, kind):
        raise ValueError(f"'{where}' must be a {kind}, not a {ref['kind']}")
    return {**ref, "kind": kind}


def _parse_axis_spec(value, units, where, keys):
    key, item = _one_key(value, keys, f"'{where}'")
    if key == "axis":
        if item not in AXES:
            raise ValueError(f"'{where}.axis' must be one of {sorted(AXES)}, got {item!r}")
        return {"axis": item}
    if key == "sketch_line_token":
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"'{where}.sketch_line_token' must be a non-empty string")
        return {"sketch_line_token": item}
    return {key: _parse_entity(item, units, f"{where}.{key}", key)}


def _parse_rectangular(arguments, units):
    for key in ("direction_one", "count_one"):
        if key not in arguments:
            raise ValueError(f"'{key}' is required for a rectangular pattern")
    two = arguments.get("direction_two")
    parts = ("count_two", "spacing_two", "spacing_two_expression")
    if two is None and any(arguments.get(key) not in (None, "") for key in parts):
        raise ValueError("'count_two' and 'spacing_two' need 'direction_two'")
    if two is not None and "count_two" not in arguments:
        raise ValueError("'direction_two' needs 'count_two'")
    opts = {"direction_one": _parse_axis_spec(arguments["direction_one"], units, "direction_one", _DIRECTION_KEYS),
            "count_one": int_arg(arguments, "count_one", None, 1, MAX_COUNT),
            "spacing_one": fo.read_value(arguments, "spacing_one", units),
            "direction_two": None, "count_two": 1, "spacing_two": None}
    if two is not None:
        opts.update(direction_two=_parse_axis_spec(two, units, "direction_two", _DIRECTION_KEYS),
                    count_two=int_arg(arguments, "count_two", None, 1, MAX_COUNT),
                    spacing_two=fo.read_value(arguments, "spacing_two", units))
    return opts


def _parse_circular(arguments, units):
    if "axis" not in arguments or "count" not in arguments:
        raise ValueError("'axis' and 'count' are required for a circular pattern")
    total = fo.read_value(arguments, "total_angle_deg", "deg", expression_key="total_angle_expression", required=False)
    total = total or {"text": "360 deg", "number": FULL_TURN_DEG}
    if total["number"] is not None and (total["number"] == 0 or abs(total["number"]) > FULL_TURN_DEG):
        raise ValueError("'total_angle_deg' must be nonzero and no more than 360 either way")
    return {"axis": _parse_axis_spec(arguments["axis"], units, "axis", _ROTATION_KEYS),
            "count": int_arg(arguments, "count", None, 1, MAX_COUNT), "total_angle": total,
            "symmetric": bool_arg(arguments, "symmetric", False)}


@error_pair
def validate_args(arguments):
    """Return (options, error_text)."""
    require_object(arguments)
    reject_unknown(arguments, _SHARED | _RECTANGULAR_ONLY | _CIRCULAR_ONLY)
    kind = arguments.get("type")
    if kind not in TYPES:
        raise ValueError(f"'type' must be one of {list(TYPES)}, got {kind!r}")
    other = sorted(set(arguments) & (_CIRCULAR_ONLY if kind == "rectangular" else _RECTANGULAR_ONLY))
    if other:
        raise ValueError(f"a {kind} pattern does not take {other}")
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    compute = arguments.get("compute", "adjust")
    if compute not in COMPUTE:
        raise ValueError(f"'compute' must be one of {sorted(COMPUTE)}, got {compute!r}")
    on_collateral = arguments.get("on_collateral", "undo")
    if on_collateral not in ON_COLLATERAL:
        raise ValueError(f"'on_collateral' must be one of {list(ON_COLLATERAL)}, got {on_collateral!r}")

    entities = arguments.get("entities")
    if not isinstance(entities, list) or not entities:
        raise ValueError("'entities' must be a non-empty array")
    seeds = [_parse_seed(value, units, f"entities[{i}]") for i, value in enumerate(entities)]
    kinds = sorted({seed["kind"] for seed in seeds})
    if len(kinds) > 1:
        raise ValueError(f"'entities' must all be one kind; Fusion cannot mix them, got {kinds}")

    seed_point = arguments.get("seed_point")
    if seed_point is not None:
        if not isinstance(seed_point, list) or len(seed_point) != 3 or not all(map(is_finite_number, seed_point)):
            raise ValueError("'seed_point' must be an array of three finite numbers [x, y, z]")
        seed_point = tuple(c * CM_PER_UNIT[units] for c in seed_point)
    delta = arguments.get("seed_volume_delta")
    if delta is not None and (not is_finite_number(delta) or delta == 0):
        raise ValueError("'seed_volume_delta' must be a nonzero number")

    shape = (_parse_rectangular if kind == "rectangular" else _parse_circular)(arguments, units)
    return {"type": kind, "seeds": seeds, "compute": compute, "seed_point": seed_point,
            "seed_delta_cm3": None if delta is None else to_cm(delta, units, 3),
            "feature_name": text_arg(arguments, "feature_name", blank_ok=True) or None,
            "on_collateral": on_collateral, "units": units, "dry_run": bool_arg(arguments, "dry_run", False), **shape}


# -- Filling the inputs ---------------------------------------------------------


def rectangular_input(session, component, members, opts, one, two, placeholder):
    """A ready input. Direction two is set whether or not there is one; see the module docstring."""
    features = component.features.rectangularPatternFeatures
    entity = features.createInput(session.collection(members), one["entity"], session.real(opts["count_one"]),
                                  session.value(opts["spacing_one"]), session.spacing_type())
    if two is not None:
        done = entity.setDirectionTwo(two["entity"], session.real(opts["count_two"]),
                                      session.value(opts["spacing_two"]))
    else:
        done = entity.setDirectionTwo(session.construction_axis(component, placeholder), session.real(1),
                                      session.real(0.0))
    if done is False:
        raise RuntimeError("Fusion would not take direction two")
    entity.isSymmetricInDirectionOne = False
    entity.isSymmetricInDirectionTwo = False
    entity.patternComputeOption = session.compute_option(opts["compute"])
    return features, entity


def circular_input(session, component, members, opts, axis):
    """A ready input, with everything a default could otherwise decide stated."""
    features = component.features.circularPatternFeatures
    entity = features.createInput(session.collection(members), axis["entity"])
    entity.quantity = session.real(opts["count"])
    entity.totalAngle = session.value(opts["total_angle"], "deg")
    entity.isSymmetric = opts["symmetric"]
    entity.patternComputeOption = session.compute_option(opts["compute"])
    return features, entity


# -- One call -------------------------------------------------------------------


class Job:
    """One pattern: read the seeds, place them, plan, then add unless this is a dry run."""

    def __init__(self, design, document, opts, session):
        self.design, self.document, self.opts, self.session = design, document, opts, session
        self.units = opts["units"]
        self.problems, self.warnings, self.resolved = [], [], []
        self.seeds, self.targets = [], []
        self.component, self.path, self.frame = None, ROOT_PATH, None
        self.features, self.input = None, None
        self.directions = []
        self.positions = None
        self.predict = None
        self.positions_trusted = True

    def run(self):
        self._read_seeds()
        if not self.problems:
            self._place()
        blocked, notes = fo.preflight(self.design, self.document, self.targets)
        self.problems += blocked
        self.warnings += notes
        if not self.problems:
            self._read_layout()
        if self.problems:
            return self._refused(self.problems)
        self._warn_outside()
        if self.opts["dry_run"]:
            return fo.envelope("planned", self.units, dry_run=True, resolved=self.resolved, warnings=self.warnings,
                               plan=self._plan(), pattern=self._row(), positions=self._shown(self.positions),
                               positions_basis="predicted")
        return self._execute()

    def _refused(self, refusals, **extra):
        return fo.refused(self.units, refusals, dry_run=self.opts["dry_run"], resolved=self.resolved,
                          warnings=self.warnings, **extra)

    # -- Seeds --------------------------------------------------------------------

    def _read_seeds(self):
        read = {"feature": self._feature_seed, "body": self._body_seed, "face": self._face_seed}
        for seed in self.opts["seeds"]:
            found = read[seed["kind"]](seed)
            if found is not None:
                self.seeds.append(found)
        if self.problems:
            return
        if len(unique_entities(seed["member"] for seed in self.seeds)) != len(self.seeds):
            self.problems.append("a seed is listed twice")
        components = unique_entities(seed["component"] for seed in self.seeds)
        if len(components) != 1:
            names = sorted(str(safe(c, "name")) for c in components)
            self.problems.append(f"the seeds are in different components: {names}. A pattern lives in one")
            return
        self.component = components[0]
        targets = {e["key"]: e for seed in self.seeds for e in seed["targets"]}
        self.targets = [targets[key] for key in sorted(targets)]
        if self.seeds[0]["kind"] == "feature" and not self.targets:
            self.warnings.append("the seed reports no bodies, so any body this pattern changes counts as collateral")
        self.warnings += [f"{label(e)} is hidden; hidden bodies are often stale" for e in self.targets
                          if not e["visible"]]

    def _find_feature(self, seed):
        if "feature_token" in seed:
            found = [e for e in self.session.by_token(seed["feature_token"]) if is_feature(e)]
            if len(found) != 1:
                self.problems.append(f"the feature token names {len(found)} features")
                return None
            return found[0]
        rows, objects = read_rows(self.design.timeline)
        position, problem = find_row(rows, seed["feature"])
        if problem:
            self.problems.append(problem)
            return None
        entity = safe(objects[position], "entity")
        if not is_feature(entity):
            self.problems.append(f"{seed['feature']!r} is a {rows[position].get('entity_type')}, not a feature")
            return None
        return entity

    def _feature_seed(self, seed):
        feature = self._find_feature(seed)
        if feature is None:
            return None
        made = items(safe(feature, "bodies"))
        named = items(safe(feature, "participantBodies"))
        wanted = [native(body) for body in made + named]
        entries = self.session.entries()
        return {"kind": "feature", "member": feature, "component": safe(feature, "parentComponent"),
                "targets": [e for e in entries if any(same_entity(native(e["_body"]), body) for body in wanted)],
                "new_bodies": len(made) if safe(feature, "operation") == _NEW_BODY_OPERATION and made else None,
                "cuts": safe(feature, "operation") not in _ADDING_OPERATIONS,
                "row": {"kind": "feature", "name": safe(feature, "name"),
                        "type": object_type(feature)}}

    def _body_seed(self, seed):
        targets, _, problem = resolve_targets(self.session.entries(), [seed["ref"]])
        if problem:
            self.problems.append(problem["error"])
            return None
        entry = targets[0]
        body = native(entry["_body"])
        return {"kind": "body", "member": body, "component": safe(body, "parentComponent"), "targets": [entry],
                "new_bodies": 1, "cuts": False, "row": {"kind": "body", "ref": body_ref(entry)},
                "path": entry["occurrence_path"]}

    def _face_seed(self, seed):
        face = self._entity(seed["ref"], "face")
        if face is None:
            return None
        entry = owner_entry(face, self.session.entries())
        if entry is None:
            self.problems.append("face: it is not on a body of this design")
            return None
        member = native(face)
        return {"kind": "face", "member": member, "component": safe(safe(member, "body"), "parentComponent"),
                "targets": [entry], "new_bodies": None, "cuts": True, "path": entry["occurrence_path"],
                "row": {"kind": "face", "ref": make_ref(face, "face", self.units, entry)}}

    def _entity(self, ref, role):
        found = resolve(self.session, ref)
        if found.refusal:
            self.problems.append(f"{role}: {found.refusal['error']} ({found.refusal['code']})")
            return None
        self.resolved.append(resolved_row(found, role))
        self.warnings += found.warnings
        return found.entity

    # -- Where the pattern lands --------------------------------------------------

    def _place(self):
        """Which occurrence world coordinates go through; root needs none."""
        root = self.design.rootComponent
        if same_entity(self.component, root):
            return
        paths = sorted({seed["path"] for seed in self.seeds if "path" in seed})
        if len(paths) > 1:
            self.problems.append(f"the seeds are named through different instances: {paths}")
            return
        occurrence = find_occurrence(root, paths[0]) if paths else self._only_instance()
        if occurrence is not None:
            self.path = safe(occurrence, "fullPathName")
            self.frame = occurrence_frame(occurrence)

    def _only_instance(self):
        instances = [o for o in strict_items(safe(self.design.rootComponent, "allOccurrences"))
                     if same_entity(safe(o, "component"), self.component)]
        if not instances:
            self.problems.append(f"no occurrence of {safe(self.component, 'name')!r} to read positions through")
            return None
        if len(instances) > 1:
            self.warnings.append(f"{safe(self.component, 'name')!r} has {len(instances)} instances; world values go "
                                 f"through {safe(instances[0], 'fullPathName')!r}")
        return instances[0]

    def _world_direction(self, vector):
        return vector if self.frame is None else direction_from_local(vector, self.frame)

    def _world_point(self, point):
        return point if self.frame is None else from_local(point, self.frame)

    def _in_component(self, owner, role):
        if same_entity(owner, self.component):
            return True
        self.problems.append(f"{role}: it is in {safe(owner, 'name')!r}, and the pattern lands in "
                             f"{safe(self.component, 'name')!r}; a direction must be in the seeds' component")
        return False

    # -- Directions ---------------------------------------------------------------

    def _named_axis(self, name):
        return {"entity": self.session.construction_axis(self.component, name), "origin": (0.0, 0.0, 0.0),
                "local": AXES[name], "named": True}

    def _edge(self, ref, role, round_ok):
        edge = self._entity(ref, role)
        if edge is None:
            return None
        member = native(edge)
        if not self._in_component(safe(safe(member, "body"), "parentComponent"), role):
            return None
        found = edge_axis(member, round_ok)
        if found is None:
            self.problems.append(f"{role}: the edge is not straight" + (", a circle or an arc" if round_ok else ""))
            return None
        return {"entity": member, "origin": found[0], "local": found[1], "named": False}

    def _face(self, ref, role):
        face = self._entity(ref, role)
        if face is None:
            return None
        member = native(face)
        if not self._in_component(safe(safe(member, "body"), "parentComponent"), role):
            return None
        found = face_axis(member)
        if found is None:
            self.problems.append(f"{role}: the face has no axis; use a cylinder, cone or torus")
            return None
        return {"entity": member, "origin": found[0], "local": found[1], "named": False}

    def _sketch_line(self, token, role):
        lines = [e for e in self.session.by_token(token) if object_type(e) == "SketchLine"]
        if len(lines) != 1:
            self.problems.append(f"{role}: the token names {len(lines)} sketch lines; a direction must be straight")
            return None
        member = native(lines[0])
        owner = safe(safe(member, "parentSketch"), "parentComponent")
        if not self._in_component(owner, role):
            return None
        line = member.worldGeometry
        start = xyz(line.startPoint)
        return {"entity": member, "origin": start, "local": sub(xyz(line.endPoint), start), "named": False}

    def _direction(self, spec, role, round_ok=False):
        if "axis" in spec:
            return self._named_axis(spec["axis"])
        if "edge" in spec:
            return self._edge(spec["edge"], role, round_ok)
        if "face" in spec:
            return self._face(spec["face"], role)
        return self._sketch_line(spec["sketch_line_token"], role)

    def _sensed(self, direction, name):
        """A direction's world unit vector. The sense of an edge is Fusion's, so the input is asked first."""
        read = safe(self.input, name)
        if read is not None:
            return unit(self._world_direction(xyz(read)))
        if not direction["named"]:
            self.warnings.append(f"Fusion did not give {name} before the add; its sense is assumed from the entity")
        return unit(self._world_direction(direction["local"]))

    # -- The layout ---------------------------------------------------------------

    def _read_layout(self):
        members = [seed["member"] for seed in self.seeds]
        read = self._read_rectangular if self.opts["type"] == "rectangular" else self._read_circular
        try:
            read(members)
        except (ValueError, RuntimeError) as exc:
            self.problems.append(str(exc))

    def _spacing_cm(self, count, key):
        spacing = self.session.length_cm(self.opts[key], self.units)
        if count > 1 and abs(spacing) <= REF_TOLERANCE_CM:
            self.problems.append(f"'{key}' is zero, so every instance would land on the seed")
        return spacing

    def _read_rectangular(self, members):
        opts = self.opts
        one = self._direction(opts["direction_one"], "direction_one")
        two = self._direction(opts["direction_two"], "direction_two") if opts["direction_two"] else None
        if one is None or (opts["direction_two"] and two is None):
            return
        if unit(one["local"]) is None or (two is not None and unit(two["local"]) is None):
            self.problems.append("a direction has no length")
            return
        if two is not None and parallel(one["local"], two["local"]):
            self.problems.append("'direction_two' is parallel to 'direction_one'")
            return
        spacing_one = self._spacing_cm(opts["count_one"], "spacing_one")
        spacing_two = self._spacing_cm(opts["count_two"], "spacing_two") if two is not None else 0.0
        if self.problems:
            return
        placeholder = None if two is not None else unused_axis(one["local"])
        self.features, self.input = rectangular_input(self.session, self.component, members, opts, one, two,
                                                      placeholder)
        world_one = self._sensed(one, "directionOne")
        world_two = self._sensed(two, "directionTwo") if two is not None else None
        self.directions = [world_one] + ([world_two] if two is not None else [])
        self.predict = lambda seed: rectangular_positions(seed, world_one, opts["count_one"], spacing_one, world_two,
                                                          opts["count_two"], spacing_two)
        if opts["seed_point"] is not None:
            self.positions = self.predict(opts["seed_point"])

    def _total_deg(self):
        value = self.opts["total_angle"]
        if value["number"] is not None:
            return value["number"]
        return math.degrees(self.session.evaluate(value, "deg"))

    def _read_circular(self, members):
        opts = self.opts
        axis = self._direction(opts["axis"], "axis", round_ok=True)
        if axis is None:
            return
        direction = unit(self._world_direction(axis["local"]))
        total = self._total_deg()
        if direction is None or abs(total) <= _ANGLE_EPSILON or abs(total) > FULL_TURN_DEG + _ANGLE_EPSILON:
            self.problems.append("the axis has no length" if direction is None
                                 else f"the total angle is {total:g} deg: it must be nonzero and within a full turn")
            return
        self.features, self.input = circular_input(self.session, self.component, members, opts, axis)
        self.directions = [direction]
        full = is_full_turn(total)
        self.positions_trusted = not opts["symmetric"] and (axis["named"] or full)
        origin = self._world_point(axis["origin"])
        self.predict = lambda seed: circular_positions(seed, origin, direction, opts["count"], total,
                                                       opts["symmetric"])
        if opts["seed_point"] is None:
            return
        self.positions = self.predict(opts["seed_point"])
        if not self.positions_trusted:
            self.warnings.append("predicted positions assume the rotation sense"
                                 + (" and that 'symmetric' centers the total on the seed" if opts["symmetric"] else
                                    " about this entity") + "; built positions come from the elements")

    def _asked(self):
        opts = self.opts
        return opts["count"] if opts["type"] == "circular" else opts["count_one"] * opts["count_two"]

    def _warn_outside(self):
        """Dry run or not: an instance past the target's box would cut nothing, and no error would say so."""
        if self.positions is None or not all(seed["cuts"] for seed in self.seeds):
            return
        outside = outside_boxes(self.positions, [t["bbox"] for t in self.targets])
        if outside:
            self.warnings.append(f"{len(outside)} of {len(self.positions)} positions are outside the target's box, "
                                 f"so those instances may cut nothing: {self._shown(outside)}")

    # -- Reports ------------------------------------------------------------------

    def _shown(self, positions):
        return None if positions is None else [display_point(p, self.units) for p in positions]

    def _row(self, elements=None, bodies_made=None, volume_change=None, reached=None):
        rectangular = self.opts["type"] == "rectangular"
        return {"type": self.opts["type"], "asked_for": self._asked(), "elements": elements,
                "bodies_made": bodies_made, "volume_change": volume_change, "instances_reached": reached,
                "direction_world": [display_direction(d) for d in self.directions],
                "direction_two_set": (("explicit" if self.opts["direction_two"] else "placeholder")
                                      if rectangular else None),
                "compute": self.opts["compute"]}

    def _plan(self):
        return {"seeds": [seed["row"] for seed in self.seeds], "component": safe(self.component, "name"),
                "targets": [body_ref(t) for t in self.targets], "occurrence_path": self.path}

    # -- Building -----------------------------------------------------------------

    def _execute(self):
        before, token = fo.baseline(self.design)
        outcome = fo.add_feature(lambda: self.features.add(self.input))
        self.warnings += outcome["warnings"]
        if outcome["status"] == "refused":
            return self._refused([outcome["refusal"]], baseline_token=token)
        try:
            return self._finish(outcome, before, token)
        except Exception as exc:
            log(f"[MCP] pattern_features post-check failed: {exc}")
            return fo.envelope("suspect", self.units, feature=fo.feature_row(outcome["feature"], outcome["health"]),
                               baseline_token=token, resolved=self.resolved,
                               warnings=self.warnings + [f"the feature was added but its check failed: {exc}"])

    def _markers(self, feature):
        """World ``(markers, exact)``, one per element; None unless every element's faces can be read."""
        elements = attempt(lambda: strict_items(feature.patternElements))
        if not elements:
            return None
        points, exact = [], True
        for element in elements:
            faces = items(safe(element, "faces"))
            marker = element_marker(faces)
            if marker is None:
                return None
            in_world = safe(faces[0], "assemblyContext") is not None
            points.append(marker[0] if in_world else self._world_point(marker[0]))
            exact = exact and marker[1]
        return points, exact

    def _expected_bodies(self):
        counts = [seed["new_bodies"] for seed in self.seeds]
        return None if None in counts else (self._asked() - 1) * sum(counts)

    def _check_positions(self, markers, exact, problems):
        """Element 0's marker, carried by the prediction, must land on the others: any seed_point then checks."""
        if same_positions(self.predict(markers[0]), markers):
            return
        text = f"the built positions differ from the predicted ones: {self._shown(self.positions)}"
        strict = self.positions_trusted and (exact or self.opts["type"] == "rectangular")
        (problems if strict else self.warnings).append(text)

    def _finish(self, outcome, before, token):
        feature, health = outcome["feature"], outcome["health"]
        after = fo.snapshot(self.design)
        keys = [t["key"] for t in self.targets]
        changes = fo.body_changes(before, after, keys, self.units)
        row = fo.feature_row(feature, health)

        collateral = sorted(set(fo.lost_outside(before, after, keys) + fo.grew_outside(before, after, keys)))
        text = f"bodies the seeds do not touch changed: {collateral}"
        if collateral and self.opts["on_collateral"] == "undo":
            undone = fo.undo(self.design, feature, before)
            return fo.envelope("undone" if undone["undone"] else "refused", self.units, feature=row, bodies=changes,
                               baseline_token=token, resolved=self.resolved, collateral=collateral,
                               warnings=self.warnings + [text] + undone["warnings"])

        change_cm3 = occurrence_volume(after, self.path) - occurrence_volume(before, self.path)
        reached = instances_reached(change_cm3, self.opts["seed_delta_cm3"])
        made = sum(1 for item in changes["created"] if item["ref"]["occurrence_path"] == self.path)
        elements = safe(safe(feature, "patternElements"), "count")
        problems = count_problems(self._asked(), elements, reached, made, self._expected_bodies())
        if collateral:
            problems.append(text)
        measured = self._markers(feature) if self.positions is not None else None
        if measured is not None:
            self._check_positions(*measured, problems)

        if self.opts["feature_name"]:
            try:
                feature.name = self.opts["feature_name"]
                row = fo.feature_row(feature, health)
            except RuntimeError as exc:
                self.warnings.append(f"could not name the feature: {exc}")
        others = [item["ref"] for item in changes["touched"] if item["role"] == "other_instance"]
        if others:
            self.warnings.append(f"other instances of this component changed with it: {others}")

        shown = self.positions
        if measured is not None:
            shown = measured_positions(self.opts["seed_point"], measured[0])
        return fo.envelope(
            "suspect" if problems else "built", self.units, feature=row, bodies=changes, baseline_token=token,
            resolved=self.resolved, warnings=problems + self.warnings,
            pattern=self._row(elements, made, to_units(change_cm3, self.units, 3), reached),
            positions=self._shown(shown), positions_basis="predicted" if measured is None else "faces")


def run(design, document, opts, session):
    return Job(design, document, opts, session).run()


# -- Fusion layer ---------------------------------------------------------------


class FusionPattern(FusionScene):
    """``FusionScene`` plus what the inputs are built from. Lives for one call."""

    def __init__(self, design):
        super().__init__(design)
        self._units = design.unitsManager

    def collection(self, members):
        collection = adsk.core.ObjectCollection.create()
        for member in members:
            collection.add(member)
        return collection

    def real(self, number):
        return adsk.core.ValueInput.createByReal(number)

    def value(self, value, kind="cm"):
        return fo.value_input(value, self._units, kind)

    def evaluate(self, value, kind):
        """A checked expression in Fusion's internal units: cm, or radians for ``deg``."""
        fo.value_input(value, self._units, kind)
        return self._units.evaluateExpression(value["text"], kind)

    def length_cm(self, value, units):
        if value["number"] is not None:
            return value["number"] * CM_PER_UNIT[units]
        return self.evaluate(value, "cm")

    def spacing_type(self):
        return adsk.fusion.PatternDistanceType.SpacingPatternDistanceType

    def compute_option(self, name):
        return getattr(adsk.fusion.PatternComputeOptions, COMPUTE[name])

    def construction_axis(self, component, name):
        return getattr(component, f"{name}ConstructionAxis")


def pattern_features(arguments):
    """Pattern features, bodies or faces of the active design."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        app = get_app()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            refusals, _ = fo.preflight(None)
            return fo.write_result(fo.refused(opts["units"], refusals, dry_run=opts["dry_run"]))
        payload = run(design, safe(app, "activeDocument"), opts, FusionPattern(design))
        log(f"[MCP] pattern_features {opts['type']}: {payload['status']}")
        return fo.write_result(payload)
    except Exception as exc:
        log(f"[MCP] pattern_features failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
