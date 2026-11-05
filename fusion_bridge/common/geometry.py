"""Vector math on plain tuples and unit conversion. Fusion lengths are cm."""

import math

# Multiplying cm by the factor gives the unit; multiplying the unit by CM_PER_UNIT gives cm.
UNIT_FACTORS = {"in": 1.0 / 2.54, "cm": 1.0, "mm": 10.0}
CM_PER_UNIT = {"in": 2.54, "cm": 1.0, "mm": 0.1}
DIGITS = 5


# -- Vectors -------------------------------------------------------------------


def xyz(p):
    """A Fusion point or vector as a tuple."""
    return (p.x, p.y, p.z)


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def scaled(v, k):
    return tuple(c * k for c in v)


def along(origin, direction, t):
    return tuple(o + d * t for o, d in zip(origin, direction))


def unit(v):
    """Unit vector, or None for a vector with no usable length."""
    n = math.sqrt(dot(v, v))
    return tuple(c / n for c in v) if n > 1e-12 else None


def to_local(point, frame):
    """A point in the coordinates of *frame*: ``(origin, x, y, z)``, orthonormal axes."""
    origin, *axes = frame
    offset = sub(point, origin)
    return tuple(dot(offset, axis) for axis in axes)


def direction_from_local(vector, frame):
    """A vector in *frame*'s coordinates, in world coordinates."""
    _, *axes = frame
    return tuple(sum(c * axis[i] for c, axis in zip(vector, axes)) for i in range(3))


def from_local(point, frame):
    """A point in *frame*'s coordinates, in world coordinates; undoes ``to_local``."""
    return add(frame[0], direction_from_local(point, frame))


# -- Units ---------------------------------------------------------------------


def unit_labels(units):
    return {"length": units, "area": f"{units}^2", "volume": f"{units}^3"}


def to_units(value_cm, units, power=1, digits=DIGITS):
    """A cm length (*power* 1), area (2) or volume (3) in *units*; None stays None."""
    if value_cm is None:
        return None
    return round(value_cm * UNIT_FACTORS[units] ** power, digits)


def to_cm(value, units, power=1):
    return value / UNIT_FACTORS[units] ** power


def from_cm(value, units, digits=DIGITS):
    """A cm length in *units*, by division and with no negative zero."""
    return round(value / CM_PER_UNIT[units], digits) + 0.0


def display_point(xyz_cm, units):
    """A cm point in *units*, as a list for a report."""
    return [to_units(c, units) + 0.0 for c in xyz_cm]


def display_direction(vector):
    """Unit vector rounded for a report; None for a zero vector."""
    n = math.sqrt(sum(c * c for c in vector))
    if n == 0:
        return None
    return [round(c / n, 6) + 0.0 for c in vector]
