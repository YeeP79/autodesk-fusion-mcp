"""Run Fusion's own compute commands (Compute All / Assembly Compute).

``Design.computeAll()`` in the API does not always match the UI command:
parameter-driven sketches, sweeps and joint positions can stay stale until
the user runs Modify > Compute All. This tool executes the real UI command
and waits for it to finish.
"""

import time
import traceback

import adsk.core
import adsk.fusion

from .common.args import is_number
from .common.results import error_result, tool_result
from .common.timeline import health_counts
from .dispatch import get_app, log

COMMAND_IDS = {
    "all": "FusionComputeAllCommand",
    "assembly": "XRefsBUComputeCmd",
}
DEFAULT_TIMEOUT_S = 300.0
_IDLE_COMMAND = "SelectCommand"
_SETTLE_POLLS = 3


def _active_command_id(ui):
    try:
        return ui.activeCommand
    except Exception:
        return None


def compute(arguments):
    """Execute Compute All (or Assembly Compute) and wait for it to finish."""
    scope = arguments.get("scope", "all")
    if scope not in COMMAND_IDS:
        return error_result(f"Error: 'scope' must be one of {sorted(COMMAND_IDS)}, got {scope!r}")
    timeout_s = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_S)
    if not is_number(timeout_s) or timeout_s <= 0:
        return error_result("Error: 'timeout_seconds' must be a positive number")

    try:
        app = get_app()
        ui = app.userInterface
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return error_result("Error: the active product is not a Fusion design")

        command_id = COMMAND_IDS[scope]
        cmd_def = ui.commandDefinitions.itemById(command_id)
        if cmd_def is None:
            return error_result(f"Error: Fusion command {command_id!r} is not available in this build")

        started = time.monotonic()
        cmd_def.execute()

        # The command only runs once control returns to Fusion's event loop,
        # so pump events until the UI is back to the idle command.
        idle_polls = 0
        seen_running = False
        timed_out = False
        while True:
            adsk.doEvents()
            active = _active_command_id(ui)
            if active == command_id:
                seen_running = True
                idle_polls = 0
            elif active in (_IDLE_COMMAND, None, ""):
                idle_polls += 1
                if idle_polls >= _SETTLE_POLLS and (seen_running or time.monotonic() - started > 1.0):
                    break
            if time.monotonic() - started > timeout_s:
                timed_out = True
                break
            time.sleep(0.05)

        elapsed = time.monotonic() - started
        counts, errors = health_counts(design)
        result = {
            "scope": scope,
            "command": command_id,
            "completed": not timed_out,
            "elapsed_seconds": round(elapsed, 2),
            "document": app.activeDocument.name if app.activeDocument else None,
            "timeline_health": counts,
            "timeline_errors": errors,
        }
        if timed_out:
            result["note"] = f"Still running after {timeout_s}s; check Fusion before relying on results."
        log(f"[MCP] compute {scope}: {'done' if not timed_out else 'TIMEOUT'} in {elapsed:.2f}s")
        return tool_result(result, timed_out)
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] compute failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")
