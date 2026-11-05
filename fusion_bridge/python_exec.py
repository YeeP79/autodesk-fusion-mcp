"""Python execution helpers and version metadata."""

import contextlib
import inspect
import io
import json
import os
import queue
import re
import threading
import time
import traceback
import urllib

import adsk.core
import adsk.fusion

from .. import settings
from ..lib import fusionAddInUtils as futil
from ..lib import mcp_server as mcp_server_module
from . import document_guard
from . import timeline_health
from .common.results import error_result, ok_result
from .dispatch import get_app


# session_id -> the globals dict every call in that session executes in. One
# live dict, never a copy: a function's __globals__ is the dict it was defined
# in, so a helper only sees names from other calls if they all share it.
SCRIPT_SESSIONS = {}
# Rebuilt by _build_runtime_globals on every call, so app/ui stay current and a
# script rebinding one does not outlive its call. Not reported as session names.
_INJECTED_NAMES = frozenset({
    "adsk",
    "app",
    "contextlib",
    "futil",
    "inspect",
    "io",
    "json",
    "mcp",
    "mcp_server_module",
    "os",
    "queue",
    "re",
    "settings",
    "threading",
    "time",
    "traceback",
    "ui",
    "urllib",
})
_CACHED_VERSION_INFO = None


def get_addin_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_git_dirs(addin_dir):
    git_entry = os.path.join(addin_dir, ".git")
    if os.path.isdir(git_entry):
        git_dir = git_entry
    elif os.path.isfile(git_entry):
        with open(git_entry, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
        if not raw.startswith("gitdir:"):
            raise ValueError(f"Unsupported .git file format at {git_entry}")
        git_dir = raw.split(":", 1)[1].strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.normpath(os.path.join(addin_dir, git_dir))
    else:
        raise FileNotFoundError(f"Missing .git entry at {git_entry}")

    common_dir = git_dir
    commondir_file = os.path.join(git_dir, "commondir")
    if os.path.exists(commondir_file):
        with open(commondir_file, "r", encoding="utf-8") as handle:
            common_dir = handle.read().strip()
        if not os.path.isabs(common_dir):
            common_dir = os.path.normpath(os.path.join(git_dir, common_dir))

    return git_dir, common_dir


def _read_ref(git_dir, common_dir, ref_name):
    for base_dir in (git_dir, common_dir):
        ref_path = os.path.join(base_dir, ref_name)
        if os.path.exists(ref_path):
            with open(ref_path, "r", encoding="utf-8") as handle:
                return handle.read().strip()[:8]

    packed_refs = os.path.join(common_dir, "packed-refs")
    if os.path.exists(packed_refs):
        with open(packed_refs, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip().endswith(ref_name):
                    return line.split()[0][:8]

    return None


class MCPBridgeStub:
    def call(self, tool_name, arguments):
        del arguments
        return {
            "error": (
                "mcp.call() is not available in standalone mode. "
                f"Tool '{tool_name}' must be configured as a separate MCP server in Claude Desktop."
            )
        }


def create_mcp_bridge():
    return MCPBridgeStub()


def _build_runtime_globals():
    app = get_app()
    return {
        "__builtins__": __builtins__,
        "adsk": adsk,
        "app": app,
        "contextlib": contextlib,
        "futil": futil,
        "inspect": inspect,
        "io": io,
        "json": json,
        "mcp": create_mcp_bridge(),
        "mcp_server_module": mcp_server_module,
        "os": os,
        "queue": queue,
        "re": re,
        "settings": settings,
        "threading": threading,
        "time": time,
        "traceback": traceback,
        "ui": app.userInterface,
        "urllib": urllib,
    }


def _describe_session(namespace):
    """Group the names a script left in a session by kind, for the result."""
    groups = {"variables": [], "modules": [], "classes": [], "functions": []}
    for name, value in namespace.items():
        if name.startswith("__") or name == "_mcp_result" or name in _INJECTED_NAMES:
            continue
        if inspect.ismodule(value):
            kind = "modules"
        elif inspect.isclass(value):
            kind = "classes"
        elif callable(value):
            kind = "functions"
        else:
            kind = "variables"
        groups[kind].append(name)
    return {
        "session_variables": sorted(groups.pop("variables")),
        "session_definitions": {kind: sorted(names) for kind, names in groups.items()},
    }


def clear_sessions():
    """Forget every session, releasing what they hold.

    Each dict is emptied in place because a persisted function and its
    __globals__ form a cycle: dropping the outer reference alone would leave
    held Fusion objects to the cycle collector, on whichever thread it runs.
    """
    for namespace in SCRIPT_SESSIONS.values():
        namespace.clear()
    SCRIPT_SESSIONS.clear()


def run_python(arguments):
    code = arguments.get("code")
    if not code:
        return error_result("ERROR: 'code' parameter required")

    session_id = arguments.get("session_id", "default")
    persistent = arguments.get("persistent", True)

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    runtime_globals = None

    try:
        runtime_globals = SCRIPT_SESSIONS.setdefault(session_id, {}) if persistent else {}
        runtime_globals.update(_build_runtime_globals())
        # A call that failed after setting it must not answer for this one.
        runtime_globals.pop("_mcp_result", None)

        compiled = compile(code, "<mcp-script>", "exec")
        with (
            contextlib.redirect_stdout(stdout_buffer),
            contextlib.redirect_stderr(stderr_buffer),
        ):
            exec(compiled, runtime_globals)

        result_value = runtime_globals.pop("_mcp_result", None)

        session_names = _describe_session(runtime_globals) if persistent else {"session_variables": []}
        payload = {
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "return_value": str(result_value) if result_value is not None else None,
            **session_names,
            "success": True,
        }
        # Non-blocking: edits to a read-only document apply but never save.
        warning = document_guard.active_document_warning()
        if warning:
            payload["warnings"] = [warning]
        return ok_result(payload)
    except Exception as exc:
        error_text = str(exc)
        tb = traceback.format_exc()
        summary = timeline_health.rollback_summary(error_text)
        if summary:
            error_text = f"{summary}\n\n{timeline_health.truncate(error_text)}"
            tb = timeline_health.truncate(tb)
        payload = {
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue(),
            "error": error_text,
            "traceback": tb,
            "success": False,
        }
        # Names bound before the failure stay in the session, so say which.
        if persistent and runtime_globals is not None:
            payload.update(_describe_session(runtime_globals))
        return error_result(payload)


def read_git_commit(addin_dir):
    """Short commit of the checkout's HEAD as it is on disk now (uncached).

    Reads the git files directly so it works without a ``git`` executable;
    raises when the add-in directory is not a git checkout.
    """
    git_dir, common_dir = _resolve_git_dirs(addin_dir)
    head_file = os.path.join(git_dir, "HEAD")
    if not os.path.exists(head_file):
        return None
    with open(head_file, "r", encoding="utf-8") as handle:
        head = handle.read().strip()
    if head.startswith("ref: "):
        return _read_ref(git_dir, common_dir, head[5:])
    return head[:8]


def get_version_info(addin_dir):
    global _CACHED_VERSION_INFO
    if _CACHED_VERSION_INFO is not None:
        return _CACHED_VERSION_INFO

    version = mcp_server_module.SERVER_INFO.get("version", "unknown")
    try:
        git_commit = read_git_commit(addin_dir)
    except Exception as exc:
        git_commit = None
        futil.log(f"[version] git commit lookup failed: {exc}")

    _CACHED_VERSION_INFO = f"v{version} ({git_commit})" if git_commit else f"v{version}"
    return _CACHED_VERSION_INFO
