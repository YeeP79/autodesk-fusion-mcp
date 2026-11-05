"""Helpers shared by the tool modules.

A tool module holds what is specific to its tool and imports the rest from
here. ``results``, ``args``, ``api``, ``geometry``, ``cloud`` and ``app_log``
never import ``adsk`` or ``dispatch``, so they load outside Fusion and on the
HTTP server thread.
"""
