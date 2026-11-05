"""Read the health of timeline items."""

import adsk.fusion

from .api import safe

# Fallback enum values, used when adsk.fusion.FeatureHealthStates is unavailable.
DEFAULT_STATES = {"healthy": 0, "warning": 1, "error": 2, "unknown": 3}


def health_states():
    """``{"healthy": value, ...}`` from this build's FeatureHealthStates."""
    enum = getattr(adsk.fusion, "FeatureHealthStates", None)
    if enum is None:
        return DEFAULT_STATES
    return {
        "healthy": getattr(enum, "HealthyFeatureHealthState", 0),
        "warning": getattr(enum, "WarningFeatureHealthState", 1),
        "error": getattr(enum, "ErrorFeatureHealthState", 2),
        "unknown": getattr(enum, "UnknownFeatureHealthState", 3),
    }


def state_name(value, states=None):
    """Map a FeatureHealthStates value to 'healthy' / 'warning' / 'error' / 'unknown'."""
    for name, enum_value in (states or DEFAULT_STATES).items():
        if value == enum_value:
            return name
    return "unknown"


def health_of(timeline_object, states=None):
    """The health of one timeline item; 'unknown' when it cannot be read."""
    return state_name(safe(timeline_object, "healthState"), states or health_states())


def health_counts(design):
    """Count timeline items by health state; name the ones in error."""
    states = health_states()
    counts = {"healthy": 0, "warning": 0, "error": 0}
    errors = []
    for item in design.timeline:
        state = item.healthState
        if state == states["error"]:
            counts["error"] += 1
            errors.append(item.name)
        elif state == states["healthy"]:
            counts["healthy"] += 1
        else:
            counts["warning"] += 1
    return counts, errors
