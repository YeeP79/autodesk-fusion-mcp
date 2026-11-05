"""Check tool arguments. The legacy transport does not validate against the schema.

The ``*_arg`` checks raise ValueError with the text a caller reports after
``Error: ``, and return the value. A validator keeping the ``(value, error
text)`` convention wraps itself in ``error_pair``.
"""

import functools
import math

UNKNOWN = "unknown argument(s)"
UNKNOWN_CAPITALIZED = "Unknown argument(s)"


def is_int(value):
    """True for an integer; bool is an int in Python but never an integer argument."""
    return isinstance(value, int) and not isinstance(value, bool)


def int_or_none(value):
    """*value* when it is an integer. For version numbers read back from Fusion."""
    return value if is_int(value) else None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value):
    if not is_number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a JSON integer too large for a float
        return False


def error_pair(validator):
    """Make a raising validator return ``(value, None)`` or ``(None, "Error: ...")``."""
    @functools.wraps(validator)
    def wrapper(*args, **kwargs):
        try:
            return validator(*args, **kwargs), None
        except ValueError as exc:
            return None, f"Error: {exc}"

    return wrapper


def require_object(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")


def unknown_message(arguments, known, label=UNKNOWN):
    """Text naming the keys of *arguments* outside *known*; None when there are none."""
    unknown = sorted(set(arguments) - set(known))
    return f"{label}: {', '.join(unknown)}" if unknown else None


def reject_unknown(arguments, known, label=UNKNOWN):
    message = unknown_message(arguments, known, label)
    if message:
        raise ValueError(message)


def bool_arg(arguments, key, default=False):
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"'{key}' must be a boolean")
    return value


def int_arg(arguments, key, default, low, high):
    value = arguments.get(key, default)
    if not is_int(value) or not low <= value <= high:
        raise ValueError(f"'{key}' must be an integer from {low} to {high}")
    return value


def vector_arg(value, where):
    """An ``[x, y, z]`` of finite numbers as a tuple of floats."""
    if not isinstance(value, list) or len(value) != 3 or not all(is_finite_number(c) for c in value):
        raise ValueError(f"'{where}' must be an array of three finite numbers [x, y, z]")
    return tuple(float(c) for c in value)


def text_arg(arguments, key, *, required=False, blank_ok=False, strip=False, suffix=""):
    """A string argument, or None when it is absent and not *required*.

    *suffix* is appended to the complaint, e.g. ``" (project name or id)"``.
    """
    value = arguments.get(key)
    if value is None:
        if required:
            raise ValueError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or (not blank_ok and not value.strip()):
        raise ValueError(f"'{key}' must be a non-empty string{suffix}")
    return value.strip() if strip else value
