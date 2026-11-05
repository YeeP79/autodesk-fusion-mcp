"""Report which add-in code is running versus what is on disk now.

Lets a client confirm that ``reload_addin`` picked up new code: the running
commit and load time are fixed when this module is imported (each reload
imports it afresh), while the disk commit and source mtimes are read on
every call.
"""

import os
import sys
import time
from datetime import datetime

from ..lib import mcp_server as mcp_server_module
from . import python_exec, reloader
from .common.results import error_result, ok_result

# Directories under the add-in that never hold running add-in code.
_SKIPPED_DIRS = {".git", "__pycache__", "tests", "third_party", "docs", "guides"}
_MAX_LISTED_CHANGES = 20

LOADED_AT = time.time()


def _safe_commit(addin_dir):
    try:
        return python_exec.read_git_commit(addin_dir)
    except Exception:
        return None


_LOADED_COMMIT = _safe_commit(python_exec.get_addin_dir())


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def package_root(module_name=__name__):
    """Name of the synthetic package Fusion imported the add-in under.

    ``<root>.fusion_bridge.bridge_info`` -> ``<root>``; ``""`` when this
    module was not imported through the add-in package (e.g. top-level).
    """
    suffix = ".fusion_bridge.bridge_info"
    if module_name.endswith(suffix):
        return module_name[: -len(suffix)]
    return ""


def loaded_modules(modules, root, addin_dir):
    """Sorted ``(short name, path relative to the add-in)`` of the add-in's modules.

    Names are relative to the package (``fusion_bridge.bodies``, not
    ``<root>.fusion_bridge.bodies``): Fusion's synthetic root is a long
    URL-encoded path, reported once as ``package``. The root package itself
    is left out.
    """
    if not root:
        return []
    prefix = root + "."
    found = []
    for name, module in list(modules.items()):
        if not name.startswith(prefix):
            continue
        path = getattr(module, "__file__", None)
        if path:
            path = os.path.relpath(os.path.abspath(path), addin_dir)
        found.append((name[len(prefix):], path))
    return sorted(found)


def changed_sources(addin_dir, since):
    """Python files under *addin_dir* modified after *since* (epoch seconds).

    Returns ``(newest_mtime, [relative paths])``; the list is newest first.
    """
    newest = 0.0
    changed = []
    for dirpath, dirnames, filenames in os.walk(addin_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIPPED_DIRS]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            newest = max(newest, mtime)
            if mtime > since:
                changed.append((mtime, os.path.relpath(path, addin_dir)))
    changed.sort(reverse=True)
    return newest, [rel for _, rel in changed]


def collect(modules=None, addin_dir=None, root=None):
    """Build the bridge_info payload (Fusion-free; inputs injectable)."""
    modules = sys.modules if modules is None else modules
    addin_dir = addin_dir or python_exec.get_addin_dir()
    root = package_root() if root is None else root

    disk_commit = _safe_commit(addin_dir)
    newest, changed = changed_sources(addin_dir, LOADED_AT)
    module_rows = loaded_modules(modules, root, addin_dir)
    return {
        "version": mcp_server_module.SERVER_INFO.get("version", "unknown"),
        "pid": os.getpid(),
        "running": {
            "commit": _LOADED_COMMIT,
            "loaded_at": _iso(LOADED_AT),
            "generation": reloader.GENERATION,
            "last_reload": reloader.LAST_RELOAD,
        },
        "disk": {
            "commit": disk_commit,
            "newest_source_change": _iso(newest) if newest else None,
            "changed_since_load": changed[:_MAX_LISTED_CHANGES],
            "changed_count": len(changed),
        },
        "up_to_date": disk_commit == _LOADED_COMMIT and not changed,
        "addin_dir": addin_dir,
        "package": root or None,
        "modules": {name: path for name, path in module_rows},
    }


def bridge_info(arguments):
    del arguments
    try:
        payload = collect()
    except Exception as exc:
        return error_result(f"Error: {type(exc).__name__}: {exc}")
    return ok_result(payload)
