"""Reading a macro's arguments, before the design is touched. Each check raises ValueError.

Macros work on numbers, because they do arithmetic on them, so no dimension here
takes an expression.
"""

from ..common.args import is_finite_number, reject_unknown, vector_arg
from ..common.bodies import ROOT_PATH, parse_body_ref
from ..common.entity_refs import REF_TOLERANCE_CM
from ..common.geometry import UNIT_FACTORS, unit


def start(arguments, known):
    """``units`` and ``tolerance``, once no key is unknown.

    ``tolerance`` is how far a point may sit from what it names and how far a
    found diameter may read from the one asked for. It defaults to the bridge's
    own ref tolerance, 0.001 in, whatever the units.
    """
    reject_unknown(arguments, set(known) | {"units", "tolerance"})
    units = arguments.get("units", "in")
    if not isinstance(units, str) or units not in UNIT_FACTORS:
        raise ValueError(f"'units' must be one of {sorted(UNIT_FACTORS)}, got {units!r}")
    tolerance = length(arguments, "tolerance", required=False)
    return {"units": units, "tolerance": REF_TOLERANCE_CM * UNIT_FACTORS[units] if tolerance is None else tolerance}


def length(arguments, key, required=True):
    """A dimension above zero; None when it is optional and absent."""
    value = arguments.get(key)
    if value is None:
        if required:
            raise ValueError(f"'{key}' is required")
        return None
    if not is_finite_number(value) or value <= 0:
        raise ValueError(f"'{key}' must be a number above zero")
    return float(value)


def point(arguments, key, required=True):
    """A world ``[x, y, z]`` as a tuple."""
    value = arguments.get(key)
    if value is None:
        if required:
            raise ValueError(f"'{key}' is required")
        return None
    return vector_arg(value, key)


def direction(arguments, key, required=True):
    """A world direction as a unit tuple."""
    value = point(arguments, key, required)
    if value is None:
        return None
    found = unit(value)
    if found is None:
        raise ValueError(f"'{key}' has no length")
    return found


def body(arguments, key):
    """A BodyRef, as given."""
    if arguments.get(key) is None:
        raise ValueError(f"'{key}' is required")
    parse_body_ref(arguments[key], f"'{key}'")
    return arguments[key]


def path_of(body_ref):
    return body_ref["occurrence_path"]


def is_root(path):
    return path in (None, ROOT_PATH)
