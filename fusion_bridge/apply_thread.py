"""Thread catalog lookup and recommendation: apply_thread, read-only half.

``catalog`` narrows Fusion's thread library by what is given, and ``recommend``
asks Fusion for the nearest thread to a diameter. Applying a thread arrives with
``feature_ops``; until then ``apply`` is refused.

The API's spellings are not the dialog's. Sizes are decimals, gauge sizes carry
the number in brackets (``0.19(#10)``) and ``1/4`` raises for the Unified family,
yet ACME and the pipe families really are spelled ``1/4``. So ``normalize_size``
always answers from the family's own ``allSizes``.

Catalog diameters depend on the class: ``1/4-20 UNC`` reads 0.2428 in major under
``1A``, 0.25 nominal. ``recommend`` is by nearest size: a 0.2269 in rod comes back
``12-24 UNC`` whatever it was meant to be. Fusion cannot take a measured pitch either; a thread
is a designation from the library or nothing.

Everything above the Fusion layer takes a duck-typed query and plain values, so
it is testable with fakes. Fusion lengths are cm.
"""

import difflib
import json
import re
import traceback

import adsk.core
import adsk.fusion

from .common import geometry
from .common.api import attempt, safe
from .common.args import bool_arg, error_pair, is_finite_number, reject_unknown, require_object, text_arg
from .common.geometry import UNIT_FACTORS, to_cm, unit_labels
from .common.results import error_result, ok_result
from .dispatch import get_app, log

ACTIONS = ("catalog", "recommend")
_COMMON_FIELDS = {"action", "units", "internal", "family"}
_ACTION_FIELDS = {
    "catalog": {"size", "designation", "thread_class"},
    "recommend": {"diameter"},
}

_DIGITS = 5
MM_PER_IN = 25.4
NEAR_LIMIT = 5
SIZE_MATCH = 1e-6  # relative; sizes are spelled to at most four decimals

# Tried in order. Inch tapping families offer only a bare 2B, and take the first.
PREFERRED_CLASSES = {False: ("2A", "6g"), True: ("2B", "6H")}

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


# -- Argument validation -------------------------------------------------------


@error_pair
def validate_args(arguments):
    """Return (options, error_text). Unused narrowing keys stay None."""
    require_object(arguments)
    action = arguments.get("action")
    if action == "apply":
        raise ValueError("'apply' is not available yet; it needs feature_ops. Use 'catalog' or 'recommend'")
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


def make_info(internal, family, designation, cls):
    """A ThreadInfo: ``ThreadInfo.create``, else the retired ``createThreadInfo`` that worked on 2704."""
    create = safe(safe(adsk.fusion, "ThreadInfo"), "create")
    if callable(create):
        return create(False, internal, family, designation, cls, True)
    return _design_threads().createThreadInfo(internal, family, designation, cls)


def apply_thread(arguments):
    """Read Fusion's thread library: catalog narrowing and nearest-size recommendation."""
    opts, err = validate_args(arguments)
    if err:
        return error_result(err)
    try:
        library = open_library()
        if opts["action"] == "catalog":
            return ok_result({"action": "catalog", **run_catalog(library, make_info, opts)})
        return ok_result({"action": "recommend", **run_recommend(library, opts)})
    except ThreadError as exc:
        return error_result(json.dumps(exc.payload(), indent=2))
    except Exception as exc:
        log(f"[MCP] apply_thread failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return error_result(f"Error: {exc}\n{traceback.format_exc()}")
