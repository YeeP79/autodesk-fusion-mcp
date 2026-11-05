"""Thread catalog lookup, recommendation and application: apply_thread.

``catalog`` narrows Fusion's thread library by what is given, ``recommend`` asks
Fusion for the nearest thread to a diameter, and ``apply`` puts a thread on one
face.

``apply`` refuses a split face, cosmetic or modeled alike. A shaft that came back
as two half-cylinders is the case that silently cuts a different solid: 2.57x the
material, measured, with no error. So the face must be whole, and only that face
goes in. Other cylinders on the body do not matter, since a plate of holes is
fine. ``modeled`` is required because the two differ by one flag and 9.8% of a
rod, and the volume is measured across the add to prove which one happened.

The API's spellings are not the dialog's. Sizes are decimals, gauge sizes carry
the number in brackets (``0.19(#10)``) and ``1/4`` raises for the Unified family,
yet ACME and the pipe families really are spelled ``1/4``. So ``normalize_size``
always answers from the family's own ``allSizes``.

Catalog diameters depend on the class: ``1/4-20 UNC`` reads 0.2428 in major under
``1A``, 0.25 nominal. ``recommend`` is by nearest size: a 0.2269 in rod comes back
``12-24 UNC`` whatever it was meant to be. Fusion cannot take a measured pitch either; a thread
is a designation from the library or nothing.

Everything above the Fusion layer takes a duck-typed query, session and plain
values, so it is testable with fakes. Fusion lengths are cm.
"""

import difflib
import json
import re
import traceback

import adsk.core
import adsk.fusion

from .common import feature_ops as fo
from .common import geometry
from .common.api import attempt, safe
from .common.args import bool_arg, error_pair, is_finite_number, reject_unknown, require_object, text_arg
from .common.bodies import VOLUME_ABS_TOLERANCE_CM3, label, native
from .common.entity_refs import (
    FusionScene,
    describe_face,
    owner_entry,
    parse_ref,
    resolve,
    resolved_row,
)
from .common.geometry import UNIT_FACTORS, to_cm, unit_labels
from .common.results import error_result, ok_result
from .dispatch import get_app, log

ACTIONS = ("catalog", "recommend", "apply")
_COMMON_FIELDS = {"action", "units", "internal", "family"}
_ACTION_FIELDS = {
    "catalog": {"size", "designation", "thread_class"},
    "recommend": {"diameter"},
    "apply": {"size", "designation", "thread_class", "face", "modeled", "right_handed", "full_length", "length",
              "length_expression", "offset", "offset_expression", "measured_from", "dry_run"},
}

_DIGITS = 5
MM_PER_IN = 25.4
NEAR_LIMIT = 5
SIZE_MATCH = 1e-6  # relative; sizes are spelled to at most four decimals

# Tried in order. Inch tapping families offer only a bare 2B, and take the first.
PREFERRED_CLASSES = {False: ("2A", "6g"), True: ("2B", "6H")}

# A face sweeping less than this is not a whole cylinder.
WHOLE_SWEEP_DEG = 355.0
SPLIT_REFUSAL = "split shafts cut 2.57x too much, or fail; measured"
# How far a face may be from the thread's major (external) or minor (internal) diameter.
DIAMETER_TOLERANCE = 0.05
_FRACTION_EPSILON = 1e-9
# Kernel volumes drift on a large body, so a delta under this fraction of it is noise.
VOLUME_NOISE_REL = 1e-6
LOCATIONS = {"high": "HighEndThreadLocation", "low": "LowEndThreadLocation"}
STALE_FACE = "the threaded face is gone after a modeled thread; find it again before naming it"

RECOMMEND_WARNING = ("nearest size only, not what the part is: Fusion picks by diameter alone, "
                     "and a thread's pitch cannot be set apart from its designation")

_FRACTION = re.compile(r"(?:(\d+)[ -]+)?(\d+)/(\d+)")
_DECIMAL = re.compile(r"\d+(?:\.\d*)?|\.\d+")
_SUFFIX = re.compile(r'\s*(mm|in|inch|inches|")\s*$', re.IGNORECASE)
_GAUGE = re.compile(r"#\s*(\d+)")

# Fusion's library is fixed for a session: family -> {"sizes": [...], "designations": {size: [...]}}.
_CACHE = {}


class ThreadError(Exception):
    """A request the library cannot answer, with what it could have meant."""

    def __init__(self, message, near=()):
        super().__init__(message)
        self.near = list(near)

    def payload(self):
        result = {"error": str(self)}
        if self.near:
            result["near_matches"] = self.near
        return result


# -- Spelling (no Fusion) ------------------------------------------------------


def size_number(text):
    """The number a size spells: ``1 1/8``, ``0.19(#10)``, ``6.0``. None when it is not one."""
    head = text.split("(", 1)[0].strip()
    fraction = _FRACTION.fullmatch(head)
    if fraction:
        whole, top, bottom = fraction.groups()
        return int(whole or 0) + int(top) / int(bottom) if int(bottom) else None
    return float(head) if _DECIMAL.fullmatch(head) else None


def _wanted_size(text, unit):
    """What a caller's size means in the family's *unit*: a number, or None."""
    gauge = _GAUGE.fullmatch(text)
    stated = None
    if gauge:  # the screw gauge formula, for families that spell gauges as bare decimals
        value, stated = 0.060 + 0.013 * int(gauge.group(1)), "in"
    else:
        suffix = _SUFFIX.search(text)
        if suffix:
            stated = "mm" if suffix.group(1).lower() == "mm" else "in"
            text = text[:suffix.start()]
        value = size_number(text)
    if value is None or stated is None or unit is None or stated == unit:
        return value
    return value * MM_PER_IN if stated == "in" else value / MM_PER_IN


def normalize_size(value, sizes, unit=None):
    """The member of *sizes* the caller means, or None.

    *sizes* is the family's ``allSizes`` and *unit* its ``threadTypeUnit``. An
    exact spelling always wins, since some families keep a literal ``1/4``. Two
    sizes matching the same number are not guessed between.
    """
    text = value.strip()
    if text in sizes:
        return text
    gauge = _GAUGE.fullmatch(text)
    if gauge:
        tagged = [s for s in sizes if s.endswith(f"(#{gauge.group(1)})")]
        if len(tagged) == 1:
            return tagged[0]
    wanted = _wanted_size(text, unit)
    if wanted is None:
        return None
    numbered = [(s, size_number(s)) for s in sizes]
    found = [s for s, n in numbered if n is not None and abs(n - wanted) <= SIZE_MATCH * max(1.0, wanted)]
    return found[0] if len(found) == 1 else None


def near_sizes(value, sizes, unit=None):
    """The sizes closest to what the caller wrote, for a refusal."""
    wanted = _wanted_size(value.strip(), unit)
    numbered = [(abs(n - wanted), s) for s, n in ((s, size_number(s)) for s in sizes) if n is not None] if wanted else []
    if numbered:
        return [s for _, s in sorted(numbered)[:NEAR_LIMIT]]
    return near_names(value, sizes)


def near_names(value, options):
    return difflib.get_close_matches(value, list(options), n=NEAR_LIMIT, cutoff=0.4)


def canonical(value, options):
    """*value* as *options* spells it, ignoring case; None when it is not one of them."""
    if value in options:
        return value
    found = [o for o in options if o.casefold() == value.strip().casefold()]
    return found[0] if len(found) == 1 else None


def pick_class(internal, classes, requested=None):
    """The class to use. Returns (class, defaulted), *defaulted* when the caller named none.

    ``classes[0]`` is the loosest fit (``1A``), so the everyday class is
    preferred when the family offers it. A named class must be offered.
    """
    if not classes:
        raise ThreadError(f"no {'internal' if internal else 'external'} classes for this designation")
    if requested is not None:
        found = canonical(requested, classes)
        if found is None:
            raise ThreadError(f"class {requested!r} is not offered", classes)
        return found, False
    for preferred in PREFERRED_CLASSES[internal]:
        if preferred in classes:
            return preferred, True
    return classes[0], True


def resolve_family(family, families, tapered=()):
    """The family as Fusion spells it. A tapered family is refused by name, not as unknown."""
    found = canonical(family, families)
    if found is not None:
        return found
    if canonical(family, tapered) is not None:
        raise ThreadError(f"{family!r} is a tapered family: Fusion cuts those as tapped holes, not thread features")
    raise ThreadError(f"unknown thread family {family!r}", near_names(family, families))


# -- The library ---------------------------------------------------------------


def _value(obj, name):
    """A property, or a method on builds that expose it as one."""
    found = getattr(obj, name)
    return found() if callable(found) else found


class ThreadLibrary:
    """Cached reads over a ThreadDataQuery."""

    def __init__(self, query, tapered_query=None, cache=_CACHE):
        self.query = query
        self._tapered = tapered_query
        self._cache = cache

    def families(self):
        return list(_value(self.query, "allThreadTypes"))

    def tapered_families(self):
        return list(attempt(lambda: _value(self._tapered, "allThreadTypes"), []) or [])

    def family(self, name):
        return resolve_family(name, self.families(), self.tapered_families())

    def unit(self, family):
        return attempt(lambda: self.query.threadTypeUnit(family)) or None

    def _entry(self, family):
        return self._cache.setdefault(family, {"sizes": None, "designations": {}})

    def sizes(self, family):
        entry = self._entry(family)
        if not entry["sizes"]:
            entry["sizes"] = list(self.query.allSizes(family))
        return entry["sizes"]

    def size(self, family, value):
        sizes = self.sizes(family)
        unit = self.unit(family)
        found = normalize_size(value, sizes, unit)
        if found is None:
            raise ThreadError(f"unknown size {value!r} in {family}", near_sizes(value, sizes, unit))
        return found

    def designations(self, family, size):
        known = self._entry(family)["designations"]
        if not known.get(size):
            known[size] = list(self.query.allDesignations(family, size))
        return known[size]

    def all_designations(self, family):
        """Every designation in the family. A size Fusion will not expand is one missing row."""
        found = []
        for size in self.sizes(family):
            found.extend(attempt(lambda: self.designations(family, size), []))
        return found

    def designation(self, family, value, size=None):
        options = self.designations(family, size) if size is not None else self.all_designations(family)
        found = canonical(value, options)
        if found is None:
            where = f"size {size} of {family}" if size is not None else family
            raise ThreadError(f"unknown designation {value!r} in {where}", near_names(value, options))
        return found

    def classes(self, internal, family, designation):
        found = list(self.query.allClasses(internal, family, designation) or [])
        if not found and attempt(lambda: self.query.allClasses(not internal, family, designation)):
            raise ThreadError(f"{designation} has no {'internal' if internal else 'external'} form in {family}")
        return found


def class_row(cls, make_info, units):
    """One class with the dimensions that belong to it, in *units*."""
    try:
        info = make_info(cls)
    except RuntimeError as exc:
        return {"class": cls, "error": str(exc)}
    return {"class": cls,
            "major": geometry.to_units(safe(info, "majorDiameter"), units, 1, _DIGITS),
            "minor": geometry.to_units(safe(info, "minorDiameter"), units, 1, _DIGITS),
            "pitch": geometry.to_units(safe(info, "threadPitch"), units, 1, _DIGITS)}


def run_catalog(library, make_info, opts):
    """Narrow the library by what the caller gave. *make_info* builds a ThreadInfo."""
    if opts["family"] is None:
        return {"families": library.families()}
    family = library.family(opts["family"])
    result = {"family": family}
    size = library.size(family, opts["size"]) if opts["size"] is not None else None
    if opts["designation"] is None:
        if size is None:
            return {**result, "size_unit": library.unit(family), "sizes": library.sizes(family)}
        return {**result, "size": size, "designations": library.designations(family, size)}

    designation = library.designation(family, opts["designation"], size)
    internal, units = opts["internal"], opts["units"]
    classes = library.classes(internal, family, designation)
    default, _ = pick_class(internal, classes)
    wanted = classes
    if opts["thread_class"] is not None:
        wanted = [pick_class(internal, classes, opts["thread_class"])[0]]
    return {**result, "designation": designation, "internal": internal, "units": unit_labels(units),
            "default_class": default,
            "classes": [class_row(cls, lambda c: make_info(internal, family, designation, c), units) for cls in wanted]}


def run_recommend(library, opts):
    """Fusion's nearest thread to a diameter. *family* defaults by the caller's units."""
    query = library.query
    name = opts["family"]
    if name is None:
        name = _value(query, "defaultInchThreadType" if opts["units"] == "in" else "defaultMetricThreadType")
    family = library.family(name)
    internal, diameter = opts["internal"], opts["diameter"]
    found, designation, thread_class = query.recommendThreadData(to_cm(diameter, opts["units"]), internal, family)
    if not found or not designation:
        raise ThreadError(f"Fusion has no recommendation for {diameter} {opts['units']} in {family}")
    return {"family": family, "internal": internal, "diameter": diameter, "units": unit_labels(opts["units"]),
            "designation": designation, "thread_class": thread_class, "nearest_only": True,
            "warnings": [RECOMMEND_WARNING]}


# -- Applying (no Fusion) ------------------------------------------------------


def is_split(sweep_deg, siblings):
    """Is this cylinder less than one whole wall? A sweep that cannot be read proves nothing, so it counts."""
    return siblings > 0 or sweep_deg is None or sweep_deg < WHOLE_SWEEP_DEG


def concavity_problem(internal, concavity):
    """An internal thread belongs in a bore, an external one on a shaft. Unknown is not a contradiction."""
    wanted = "concave" if internal else "convex"
    if concavity in ("concave", "convex") and concavity != wanted:
        side, shape = ("internal", "a shaft") if internal else ("external", "a bore")
        return f"'internal' says {side}, but the face is {shape} ({concavity})"
    return None


def face_problems(rec, internal):
    """Why this face cannot take a thread; *rec* is ``describe_face``'s record."""
    if rec["type"] != "cylinder":
        return [f"the face is a {rec['type']}, not a cylinder"]
    problems = []
    siblings, sweep = rec.get("split_siblings", 0), rec.get("sweep_deg")
    if is_split(sweep, siblings):
        problems.append(f"the face is not a whole cylinder (sweep_deg {sweep}, split_siblings {siblings}): "
                        f"{SPLIT_REFUSAL}")
    contradiction = concavity_problem(internal, rec.get("concavity"))
    return problems + ([contradiction] if contradiction else [])


def diameter_problem(face_diameter, reference, internal, units):
    """Is the face the size this thread goes on? Both in *units*, the reference read for the class in use."""
    name = "minor" if internal else "major"
    if not reference:
        return f"the thread has no {name} diameter to check the face against"
    if abs(face_diameter - reference) / reference > DIAMETER_TOLERANCE + _FRACTION_EPSILON:
        return (f"the face is {face_diameter} {units} across, more than {DIAMETER_TOLERANCE:.0%} from the "
                f"thread's {name} diameter {reference} {units}")
    return None


def volume_problem(modeled, volume_before, volume_after, faces_changed):
    """Did the add do what ``modeled`` said? A small modeled thread on a big body may sit under the noise."""
    if volume_before is None or volume_after is None:
        return "the body's volume could not be read, so nothing proves what the thread did"
    noise = max(VOLUME_ABS_TOLERANCE_CM3, VOLUME_NOISE_REL * abs(volume_before))
    moved = abs(volume_after - volume_before) > noise
    if modeled and not moved and not faces_changed:
        return "a modeled thread changed nothing on the body"
    if not modeled and moved:
        return "a cosmetic thread changed the body's volume"
    return None


def pick_thread(library, info_of, opts):
    """The thread *opts* name: family, designation, class and its ThreadInfo. Raises ThreadError."""
    internal = opts["internal"]
    family = library.family(opts["family"])
    size = library.size(family, opts["size"]) if opts["size"] is not None else None
    designation = library.designation(family, opts["designation"], size)
    chosen, defaulted = pick_class(internal, library.classes(internal, family, designation), opts["thread_class"])
    try:
        info = info_of(internal, family, designation, chosen, opts["right_handed"])
    except RuntimeError as exc:
        raise ThreadError(f"Fusion could not build {designation} class {chosen}: {exc}") from exc
    return {"family": family, "designation": designation, "thread_class": chosen, "class_defaulted": defaulted,
            "_info": info}


def thread_row(thread, opts):
    """The result's ``thread``: what was asked for and the dimensions of the class in use."""
    info, units = thread["_info"], opts["units"]
    row = {key: thread[key] for key in ("family", "designation", "thread_class")}
    row.update(internal=opts["internal"], modeled=opts["modeled"], right_handed=opts["right_handed"],
               pitch=geometry.to_units(safe(info, "threadPitch"), units, 1, _DIGITS),
               major=geometry.to_units(safe(info, "majorDiameter"), units, 1, _DIGITS),
               minor=geometry.to_units(safe(info, "minorDiameter"), units, 1, _DIGITS),
               class_defaulted=thread["class_defaulted"], full_length=opts["full_length"])
    if not opts["full_length"]:
        row.update(length=opts["length"]["text"], offset=opts["offset"]["text"], measured_from=opts["measured_from"])
    return row


def face_row(rec, check):
    return {"ref": rec["ref"], "diameter": rec.get("diameter"), "sweep_deg": rec.get("sweep_deg"),
            "split_siblings": rec.get("split_siblings"), "diameter_check": check, "concavity": rec.get("concavity"),
            "axis_world": rec.get("axis_direction"), "length_along_axis": rec.get("length_along_axis")}


def _read_face(session, opts, problems, warnings, resolved):
    """Returns (face, entry, record); a miss leaves them None with the reason in *problems*."""
    found = resolve(session, opts["face"])
    if found.refusal:
        problems.append(f"face: {found.refusal['error']} ({found.refusal['code']})")
        return None, None, None
    resolved.append(resolved_row(found, "face"))
    warnings += found.warnings
    entry = owner_entry(found.entity, session.entries())
    if entry is None:
        problems.append("face: it is not on a body of this design")
        return None, None, None
    if not entry["solid"]:
        problems.append(f"{label(entry)} is a surface body: a thread needs a solid")
    elif not entry["visible"]:
        warnings.append(f"{label(entry)} is hidden; hidden bodies are often stale")
    return found.entity, entry, session.describe(found.entity, opts["units"], entry)


def _check_diameter(rec, thread, opts, problems):
    """The result's ``diameter_check``; a face that cannot be judged was refused for that already."""
    if thread is None or rec is None or rec["type"] != "cylinder":
        return "not_checked"
    info = thread["_info"]
    reference = safe(info, "minorDiameter" if opts["internal"] else "majorDiameter")
    problem = diameter_problem(rec["diameter"], geometry.to_units(reference, opts["units"], 1, _DIGITS),
                               opts["internal"], opts["units"])
    if problem:
        problems.append(problem)
        return "out_of_range"
    return "ok"


def _check_lengths(session, opts, problems):
    """An expression Fusion cannot read is a refusal, before anything is built."""
    if opts["full_length"]:
        return
    for value in (opts["length"], opts["offset"]):
        try:
            session.value(value)
        except ValueError as exc:
            problems.append(str(exc))


def _finish_apply(design, outcome, job):
    """Measure across the add; *job* holds ``entry``, ``before``, ``token``, ``opts`` and the report rows."""
    feature, health, opts, key = outcome["feature"], outcome["health"], job["opts"], job["entry"]["key"]
    before, units = job["before"], opts["units"]
    after = fo.snapshot(design)
    changes = fo.body_changes(before, after, [key], units)
    old, new = before.get(key), after.get(key)
    volume_before, volume_after = (None if rec is None else rec["volume"] for rec in (old, new))
    faces_changed = old is not None and new is not None and old["faces"] != new["faces"]

    problems = []
    problem = volume_problem(opts["modeled"], volume_before, volume_after, faces_changed)
    if problem:
        problems.append(problem)
    lost = fo.lost_outside(before, after, [key])
    if lost:
        problems.append(f"bodies nobody named lost volume or went: {lost}")
    delta = None if None in (volume_before, volume_after) else volume_after - volume_before
    warnings = list(job["warnings"]) + ([STALE_FACE] if opts["modeled"] else [])
    others = [item["ref"] for item in changes["touched"] if item["role"] == "other_instance"]
    if others:
        warnings.append(f"other instances of this component changed with it: {others}")
    return fo.envelope("suspect" if problems else "built", units, feature=fo.feature_row(feature, health),
                       bodies=changes, baseline_token=job["token"], resolved=job["resolved"],
                       warnings=problems + warnings, action="apply", thread=job["thread"], face=job["face"],
                       volume_delta=geometry.to_units(delta, units, 3, _DIGITS + 1))


def run_apply(design, document, library, info_of, opts, session):
    """One thread: every refusal listed together, then the add unless this is a dry run."""
    units, dry_run = opts["units"], opts["dry_run"]
    problems, warnings, resolved, near = [], [], [], []
    thread = None
    try:
        thread = pick_thread(library, info_of, opts)
    except ThreadError as exc:
        problems.append(str(exc))
        near = exc.near

    face, entry, rec = _read_face(session, opts, problems, warnings, resolved)
    if rec is not None:
        problems += face_problems(rec, opts["internal"])
    check = _check_diameter(rec, thread, opts, problems)
    _check_lengths(session, opts, problems)
    blocked, notes = fo.preflight(design, document, [entry] if entry is not None else ())
    problems += blocked
    warnings += notes

    shown = {"thread": thread_row(thread, opts) if thread else None, "face": face_row(rec, check) if rec else None}
    if problems:
        extra = {"near_matches": near} if near else {}
        return fo.refused(units, problems, dry_run=dry_run, resolved=resolved, warnings=warnings, action="apply",
                          **shown, **extra)
    if dry_run:
        return fo.envelope("planned", units, dry_run=True, resolved=resolved, warnings=warnings, action="apply",
                           plan={"component": entry["component"], "target": entry["key"]}, **shown)

    before, token = fo.baseline(design)
    outcome = fo.add_feature(lambda: session.add(face, thread["_info"], opts))
    warnings += outcome["warnings"]
    if outcome["status"] == "refused":
        return fo.refused(units, [outcome["refusal"]], resolved=resolved, warnings=warnings, baseline_token=token,
                          action="apply", **shown)
    job = {"entry": entry, "before": before, "token": token, "opts": opts, "resolved": resolved, "warnings": warnings,
           **shown}
    try:
        return _finish_apply(design, outcome, job)
    except Exception as exc:
        log(f"[MCP] apply_thread post-check failed: {exc}")
        return fo.envelope("suspect", units, feature=fo.feature_row(outcome["feature"], outcome["health"]),
                           baseline_token=token, resolved=resolved, action="apply", **shown,
                           warnings=warnings + [f"the thread was added but its check failed: {exc}"])


# -- Argument validation -------------------------------------------------------


def _partial_length(arguments, units):
    """``length``, ``offset`` and ``measured_from``, which only a partial thread takes."""
    length = fo.read_value(arguments, "length", units, minimum=0)
    offset = fo.read_value(arguments, "offset", units, required=False) or {"text": f"0 {units}", "number": 0.0}
    if offset["number"] is not None and offset["number"] < 0:
        raise ValueError("'offset' must not be negative")
    measured_from = arguments.get("measured_from", "high")
    if measured_from not in LOCATIONS:
        raise ValueError(f"'measured_from' must be one of {sorted(LOCATIONS)}, got {measured_from!r}")
    return {"length": length, "offset": offset, "measured_from": measured_from}


def _apply_options(arguments, opts):
    for key in ("family", "designation"):
        if opts[key] is None:
            raise ValueError(f"'{key}' is required for apply")
    if "modeled" not in arguments:
        raise ValueError("'modeled' is required: the two differ by one flag and 9.8% of a rod")
    if "face" not in arguments:
        raise ValueError("'face' is required for apply")
    ref = parse_ref(arguments["face"], opts["units"], "face")
    if ref["kind"] not in (None, "face"):
        raise ValueError(f"'face' must be a face, not a {ref['kind']}")
    full_length = bool_arg(arguments, "full_length", True)
    opts.update(face={**ref, "kind": "face"}, modeled=bool_arg(arguments, "modeled"),
                right_handed=bool_arg(arguments, "right_handed", True), full_length=full_length,
                dry_run=bool_arg(arguments, "dry_run", False))
    partial = sorted(k for k in ("length", "length_expression", "offset", "offset_expression", "measured_from")
                     if k in arguments)
    if full_length:
        if partial:
            raise ValueError(f"{partial} need 'full_length': false")
        return opts
    return {**opts, **_partial_length(arguments, opts["units"])}


@error_pair
def validate_args(arguments):
    """Return (options, error_text). Unused narrowing keys stay None."""
    require_object(arguments)
    action = arguments.get("action")
    if action not in ACTIONS:
        raise ValueError(f"'action' must be one of {list(ACTIONS)}")
    reject_unknown(arguments, _COMMON_FIELDS | _ACTION_FIELDS[action])
    units = arguments.get("units", "in")
    if units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    opts = {"action": action, "units": units, "internal": bool_arg(arguments, "internal", False),
            "family": text_arg(arguments, "family")}
    if action == "recommend":
        value = arguments.get("diameter")
        if not is_finite_number(value) or value <= 0:
            raise ValueError("'diameter' must be a positive number")
        opts["diameter"] = value
        return opts
    for key in ("size", "designation", "thread_class"):
        opts[key] = text_arg(arguments, key)
    if action == "apply":
        return _apply_options(arguments, opts)
    if opts["family"] is None and (opts["size"] or opts["designation"]):
        raise ValueError("'size' and 'designation' need 'family'; call with no arguments to list families")
    if opts["thread_class"] is not None and opts["designation"] is None:
        raise ValueError("'thread_class' needs 'designation'")
    return opts


# -- Fusion layer --------------------------------------------------------------


def _design_threads():
    """The active design's ThreadFeatures, for the routes the current API has retired."""
    design = adsk.fusion.Design.cast(get_app().activeProduct)
    if design is None:
        raise ThreadError("the thread library needs an active design on this Fusion build")
    return design.rootComponent.features.threadFeatures


def open_library():
    """The library, by ``ThreadDataQuery.create``, or through the design on older builds."""
    create = safe(safe(adsk.fusion, "ThreadDataQuery"), "create")
    if callable(create):
        return ThreadLibrary(create(False), attempt(lambda: create(True)))
    return ThreadLibrary(_design_threads().threadDataQuery)


def make_info(internal, family, designation, cls, right_handed=True):
    """A ThreadInfo: ``ThreadInfo.create``, else the retired ``createThreadInfo`` that worked on 2704."""
    create = safe(safe(adsk.fusion, "ThreadInfo"), "create")
    if callable(create):
        return create(False, internal, family, designation, cls, right_handed)
    info = _design_threads().createThreadInfo(internal, family, designation, cls)
    if not right_handed:
        info.isRightHanded = False
    return info


class FusionThread(FusionScene):
    """``FusionScene`` plus the add. Lives for one call."""

    def __init__(self, design):
        super().__init__(design)
        self._units = design.unitsManager

    def describe(self, face, units, entry):
        return describe_face(face, units, entry)

    def value(self, value):
        return fo.value_input(value, self._units)

    def add(self, face, info, opts):
        """One whole face, in a collection as Build proved it; a feature works on native entities."""
        member = native(face)
        threads = member.body.parentComponent.features.threadFeatures
        faces = adsk.core.ObjectCollection.create()
        faces.add(member)
        entity = threads.createInput(faces, info)
        entity.isModeled = opts["modeled"]
        entity.isFullLength = opts["full_length"]
        if not opts["full_length"]:
            entity.threadLength = self.value(opts["length"])
            entity.threadOffset = self.value(opts["offset"])
            entity.threadLocation = getattr(adsk.fusion.ThreadLocations, LOCATIONS[opts["measured_from"]])
        return threads.add(entity)


def _apply(opts):
    app = get_app()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        refusals, _ = fo.preflight(None)
        return fo.write_result(fo.refused(opts["units"], refusals, dry_run=opts["dry_run"], action="apply"))
    payload = run_apply(design, safe(app, "activeDocument"), open_library(), make_info, opts, FusionThread(design))
    log(f"[MCP] apply_thread apply: {payload['status']}")
    return fo.write_result(payload)


def apply_thread(arguments):
    """Fusion's thread library: catalog narrowing, nearest-size recommendation, and applying a thread."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        if opts["action"] == "apply":
            return _apply(opts)
        library = open_library()
        if opts["action"] == "catalog":
            return ok_result({"action": "catalog", **run_catalog(library, make_info, opts)})
        return ok_result({"action": "recommend", **run_recommend(library, opts)})
    except ThreadError as exc:
        return error_result(json.dumps(exc.payload(), indent=2))
    except Exception as exc:
        log(f"[MCP] apply_thread failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
