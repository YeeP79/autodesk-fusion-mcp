"""Fusion bridge server assembly and startup/shutdown wiring."""

import platform
import threading
import time
import traceback

import adsk.core

from .. import settings
from ..lib import mcp_server as mcp_server_module
from . import doc_lookup, python_exec, tool_surface
from .dispatch import (
    dispatch_to_main_thread,
    drain_logs,
    format_duration,
    get_shutdown_flag,
    init_main_thread_dispatch,
    log,
    set_tool_handler,
    stop_main_thread_dispatch,
    wait_for_main_thread,
    request_main_thread_shutdown,
)


_server = None
_server_lock = threading.Lock()


def handle_any_tool(call_data):
    """Route to the correct handler based on tool name, timing execution."""
    from . import operations

    tool_name = call_data.get("params", {}).get("name", "")
    if tool_name == "__startup_ping__":
        # Internal readiness probe used by wait_for_main_thread(); not a
        # real tool, so skip routing and per-command timing.
        return {"content": [{"type": "text", "text": "ready"}]}
    handler = operations.TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
            "isError": True,
        }

    start = time.perf_counter()
    result = None
    try:
        result = handler(call_data)
        return result
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        is_error = result is None or (
            isinstance(result, dict) and result.get("isError")
        )
        status = "failed" if is_error else "completed"
        log(f"[MCP] {tool_name} {status} in {format_duration(elapsed_ms)}")


def create_server():
    from . import operations

    set_tool_handler(handle_any_tool)

    server = mcp_server_module.MCPServer(
        port=settings.MCP_SERVER_PORT,
        tools=tool_surface.TOOL_DEFINITIONS,
        tool_handlers={
            name: dispatch_to_main_thread for name in operations.TOOL_HANDLERS
        },
        log_callback=log,
    )

    server.resources.append(
        {
            "uri": tool_surface.RESOURCE_URI,
            "name": tool_surface.RESOURCE_NAME,
            "description": tool_surface.RESOURCE_DESCRIPTION,
            "mimeType": "text/markdown",
            "content_fn": doc_lookup.read_design_guide,
        }
    )

    try:
        info = python_exec.get_version_info(python_exec.get_addin_dir())
        if "(" in info:
            server.git_commit = info.split("(")[1].rstrip(")")
    except Exception:
        pass

    return server


def _start_server():
    global _server

    if _server and _server.is_running:
        log("MCP server already running")
        return True

    log(f"Starting MCP server on port {settings.MCP_SERVER_PORT}...")
    attempt = 0
    shutdown_flag = get_shutdown_flag()
    while not shutdown_flag.is_set():
        try:
            _server = create_server()
            _server.start()
            break
        except OSError as exc:
            if "Address already in use" not in str(exc):
                raise
            attempt += 1
            delay = 2 if attempt <= 60 else min(2 ** (attempt - 60), 60)
            log(
                f"Port {settings.MCP_SERVER_PORT} busy, retrying in {delay}s (attempt {attempt})..."
            )
            shutdown_flag.wait(delay)
    else:
        log("MCP server start aborted (add-in stopping)")
        return False

    version_info = python_exec.get_version_info(python_exec.get_addin_dir())
    log(
        f"[SUCCESS] MCP server {version_info} running at http://127.0.0.1:{settings.MCP_SERVER_PORT}/mcp"
    )
    return True


def _server_start_worker(shutdown_flag=None):
    """Bind the MCP port only once Fusion's main thread is pumping events.

    During a cold start Fusion auto-loads add-ins before its event loop is
    live.  Binding the socket from ``run()`` in that window leaves a
    listening port that never serves requests, so MCP clients hang until
    the add-in is manually restarted.  Waiting here replicates the
    conditions of a manual (post-startup) add-in start.
    """
    shutdown_flag = shutdown_flag if shutdown_flag is not None else get_shutdown_flag()
    if shutdown_flag.is_set():
        return

    log("Waiting for the Fusion main thread to become ready...")
    wait_started = time.monotonic()
    if not wait_for_main_thread(poll_interval=1.0, shutdown=shutdown_flag):
        log("Add-in stopped before Fusion was ready; MCP server not started")
        return
    log(
        "Fusion main thread ready after "
        f"{format_duration((time.monotonic() - wait_started) * 1000)}"
    )

    with _server_lock:
        if shutdown_flag.is_set():
            return
        try:
            if not _start_server():
                request_main_thread_shutdown()
        except Exception as exc:
            log(
                f"ERROR: Failed to start MCP server: {exc}",
                adsk.core.LogLevels.ErrorLogLevel,
            )
            log(traceback.format_exc(), adsk.core.LogLevels.ErrorLogLevel)
            request_main_thread_shutdown()


def start():
    version_info = python_exec.get_version_info(python_exec.get_addin_dir())
    log(f"MCP Integration starting... add-in {version_info}")
    fusion_version = getattr(adsk.core.Application.get(), "version", "unknown")
    log(f"Runtime: Autodesk Fusion {fusion_version}; Python {platform.python_version()}")

    try:
        init_main_thread_dispatch()
    except Exception as exc:
        log(
            f"ERROR: Failed to initialize main-thread dispatch: {exc}",
            adsk.core.LogLevels.ErrorLogLevel,
        )
        log(traceback.format_exc(), adsk.core.LogLevels.ErrorLogLevel)
        return False

    if settings.MCP_AUTO_CONNECT:
        try:
            starter = threading.Thread(
                target=_server_start_worker,
                args=(get_shutdown_flag(),),
                name="AutodeskFusionMCP-server-start",
                daemon=True,
            )
            starter.start()
            log("MCP Integration started (server binds once Fusion is ready)")
        except Exception as exc:
            log(
                f"ERROR: Failed to start MCP server: {exc}",
                adsk.core.LogLevels.ErrorLogLevel,
            )
            log(traceback.format_exc(), adsk.core.LogLevels.ErrorLogLevel)
            stop_main_thread_dispatch()
            return False
    else:
        log("MCP_AUTO_CONNECT is False - MCP server disabled")
        log("Set MCP_AUTO_CONNECT = True in settings.py to enable")

    drain_logs()
    return True


def stop():
    global _server

    log("MCP Integration stopping...")
    # Also releases any pending readiness wait inside _server_start_worker.
    stop_main_thread_dispatch()

    with _server_lock:
        if _server:
            _server.stop()
            _server = None

    python_exec.clear_sessions()

    log("MCP Integration stopped")
    drain_logs()
