# -*- coding: utf-8 -*-
"""Autodesk Fusion MCP add-in runtime wiring."""

from . import commands, settings
from .fusion_bridge import reloader
from .fusion_bridge import runtime as fusion_runtime
from .lib import fusionAddInUtils as futil


def startup():
    """Start the bridge and UI commands; ``False`` when startup aborted.

    Shared by Fusion's ``run()`` and the in-process ``reload_addin`` path,
    which needs to know whether the fresh code actually came up.
    """
    fusion_runtime.log("Autodesk Fusion MCP starting...")
    fusion_runtime.log(f"  MCP_AUTO_CONNECT: {settings.MCP_AUTO_CONNECT}")
    fusion_runtime.log(f"  MCP_SERVER_PORT: {settings.MCP_SERVER_PORT}")
    if not fusion_runtime.start():
        fusion_runtime.log("ERROR: Autodesk Fusion MCP startup aborted")
        return False
    commands.start()
    fusion_runtime.log("[OK] Autodesk Fusion MCP started")
    return True


def shutdown():
    """Stop the bridge and UI commands, exactly as Fusion's ``stop()`` does."""
    fusion_runtime.log("Autodesk Fusion MCP stopping...")
    fusion_runtime.stop()
    commands.stop()
    reloader.release_stale_events()
    futil.clear_handlers()
    fusion_runtime.log("[OK] Autodesk Fusion MCP stopped")


def run(context):
    del context
    try:
        startup()
    except Exception:
        futil.handle_error("run")


def stop(context):
    del context
    try:
        shutdown()
    except Exception:
        futil.handle_error("stop")
