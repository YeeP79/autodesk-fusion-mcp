"""The feature tools as plain calls, for ``execute_python`` and ``timeline_insert`` code.

``primitives.extrude_profile(sketch_name="S1", ...)`` runs the MCP tool's own
handler on the same arguments and returns its payload as a dict, so a script
never restates what a tool checks or how it measures.

A refused or failed tool comes back as its payload, not an exception: one bad
step must not cost a script what it already built. Every payload has ``status``
and ``ok``, so read them before going on:

- A write keeps its envelope: ``built``, ``planned``, ``partial``, ``suspect``,
  ``undone`` or ``refused``.
- A tool with no status of its own reads ``done`` when it answered and
  ``refused`` when it would not, with ``error`` saying why.
- ``failed`` means the handler raised, so what it changed is unknown. ``error``
  and ``traceback`` say where.

``ok`` is the payload's own when it has one: check_solids' means nothing was
found wrong, so ``ok`` false with ``status`` done is a finished check, not a
failed call.

Schema keys that are Python keywords take a trailing underscore: ``from_="xy"``.
"""

import json
import keyword
import traceback

import adsk.core

from . import (
    apply_thread,
    combine_bodies,
    compare_bodies,
    construction_plane,
    edge_treatment,
    extrude_profile,
    find_entities,
    pattern_features,
    sketch_profile,
    solid_check,
    tool_surface,
)
from .common.args import reject_unknown
from .dispatch import log

HANDLERS = {
    "find_entities": find_entities.find_entities,
    "sketch_profile": sketch_profile.sketch_profile,
    "extrude_profile": extrude_profile.extrude_profile,
    "edge_treatment": edge_treatment.edge_treatment,
    "pattern_features": pattern_features.pattern_features,
    "combine_bodies": combine_bodies.combine_bodies,
    "construction_plane": construction_plane.construction_plane,
    "apply_thread": apply_thread.apply_thread,
    "compare_bodies": compare_bodies.compare_bodies,
    "check_solids": solid_check.check_solids,
}

# A script reaches the handler without the client's schema check, and some
# handlers ignore a key they do not know, so a typo here would read as a default.
_SCHEMA_KEYS = {tool["name"]: set(tool["inputSchema"]["properties"]) for tool in tool_surface.TOOL_DEFINITIONS
                if tool["name"] in HANDLERS}

# Every handler ends its catch-all error text with format_exc(); its refusals never do.
_TRACEBACK = "Traceback (most recent call last)"


def _from_text(text, is_error):
    """A payload for a result that is not a JSON object."""
    if not is_error:
        return {"result": text}
    error, mark, trace = text.partition(f"\n{_TRACEBACK}")
    if not mark:
        return {"error": text}
    return {"status": "failed", "error": error, "traceback": _TRACEBACK + trace}


def parse_result(result):
    """The payload of a handler's MCP result, with ``status`` and ``ok`` always set."""
    is_error = bool(result.get("isError"))
    text = result["content"][0]["text"]
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = _from_text(text, is_error)
    payload.setdefault("status", "refused" if is_error else "done")
    payload.setdefault("ok", not is_error)
    return payload


def tool_arguments(name, kwargs):
    """Keywords as tool *name*'s arguments; ``from_`` becomes ``from``. Raises ValueError."""
    arguments = {}
    for key, value in kwargs.items():
        key_name = key[:-1] if key.endswith("_") and keyword.iskeyword(key[:-1]) else key
        if key_name in arguments:
            raise ValueError(f"give one of '{key_name}' and '{key_name}_', not both")
        arguments[key_name] = value
    reject_unknown(arguments, _SCHEMA_KEYS[name])
    return arguments


def call(name, kwargs):
    """Run one tool's handler; always returns a payload."""
    try:
        arguments = tool_arguments(name, kwargs)
    except ValueError as exc:
        return {"status": "refused", "ok": False, "error": f"Error: {exc}"}
    try:
        return parse_result(HANDLERS[name](arguments))
    except Exception as exc:
        log(f"[MCP] primitives.{name} failed: {exc}", adsk.core.LogLevels.ErrorLogLevel)
        return {"status": "failed", "ok": False, "error": f"Error: {exc}", "traceback": traceback.format_exc()}


class Primitives:
    """One method per tool in HANDLERS: its arguments as keywords, its payload back."""


def _method(name):
    def method(self, **kwargs):
        return call(name, kwargs)

    method.__name__ = method.__qualname__ = name
    method.__doc__ = f"The {name} tool: see its MCP description for the arguments."
    return method


for _name in HANDLERS:
    setattr(Primitives, _name, _method(_name))
