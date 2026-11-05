"""Macros for ``execute_python`` and ``timeline_insert`` code: parts built from primitives.

``macros.bored_boss(body=..., point=..., ...)`` is a plain function over the
``primitives`` a script already has, so it checks and measures nothing the tools
do not. Each module's docstring gives its arguments, and ``steps`` the contract
they share:

- Keyword arguments, with ``units`` (default in) and ``tolerance``. Every point and
  vector is in world coordinates, and every dimension is an argument: no name is read.
- Bad arguments are refused before a step runs.
- The first step that is not ok ends the run, and what was built stays and is listed.
- The result has ``status``, ``ok``, ``steps``, ``built``, ``stopped_at``, ``not_run``,
  ``warnings`` and what the macro made. ``ok`` is true only for ``built``.

There is no dry run: each step needs what the one before it built.
"""

from .. import primitives
from . import bored_boss, drilled_hole, flanged_plate, gusset


class Macros:
    """One method per macro: its arguments as keywords, its result back. Never raises."""

    def __init__(self, tools=None):
        self._tools = primitives.Primitives() if tools is None else tools

    def bored_boss(self, **arguments):
        return bored_boss.bored_boss(self._tools, **arguments)

    def flanged_plate(self, **arguments):
        return flanged_plate.flanged_plate(self._tools, **arguments)

    def gusset(self, **arguments):
        return gusset.gusset(self._tools, **arguments)

    def drilled_hole(self, **arguments):
        return drilled_hole.drilled_hole(self._tools, **arguments)
