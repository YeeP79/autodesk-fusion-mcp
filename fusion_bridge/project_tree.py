"""Read-only listing of a Fusion cloud project as a folder/file tree.

The walk and path logic live in ``data_tree`` (Fusion-free); this module
validates arguments, finds the project in the active hub and formats the
result. Cloud reads are slow, so the walk stops early on ``max_files`` or a
time budget kept below the main-thread dispatch timeout, and says so.
"""

import json
import time
import traceback

from .. import settings
from . import data_tree
from .dispatch import get_app, log

DEFAULT_DEPTH = 3
DEFAULT_MAX_FILES = 500
MAX_FILES_LIMIT = 5000
# Leave headroom under the dispatcher's main-thread timeout so a slow walk
# returns partial results instead of a "may still be executing" timeout.
_BUDGET_MARGIN_S = 20.0
_MIN_BUDGET_S = 10.0

_KNOWN_ARGS = {"project", "folder", "depth", "max_files", "include_links", "name_contains", "list_projects"}

NOTES = [
    "Folder names are case-insensitive in Fusion; 'folder' matches regardless of case.",
    "File names are not unique: identify files (and links) by id, not name.",
]


def _error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_arguments(arguments):
    """Return (options, None) or (None, error_text). Touches nothing in Fusion."""
    if not isinstance(arguments, dict):
        return None, "Error: arguments must be an object"
    unknown = sorted(set(arguments) - _KNOWN_ARGS)
    if unknown:
        return None, f"Error: unknown argument(s): {', '.join(unknown)}"

    list_projects = arguments.get("list_projects", False)
    if not isinstance(list_projects, bool):
        return None, "Error: 'list_projects' must be a boolean"

    project = arguments.get("project")
    if project is not None and (not isinstance(project, str) or not project.strip()):
        return None, "Error: 'project' must be a non-empty string (project name or id)"
    if not list_projects and project is None:
        return None, "Error: 'project' is required unless 'list_projects' is true"

    try:
        segments = data_tree.split_path(arguments.get("folder"))
    except ValueError as exc:
        return None, f"Error: 'folder': {exc}"

    depth = arguments.get("depth", DEFAULT_DEPTH)
    if not _is_int(depth) or not 0 <= depth <= data_tree.MAX_DEPTH:
        return None, f"Error: 'depth' must be an integer from 0 to {data_tree.MAX_DEPTH}"

    max_files = arguments.get("max_files", DEFAULT_MAX_FILES)
    if not _is_int(max_files) or not 1 <= max_files <= MAX_FILES_LIMIT:
        return None, f"Error: 'max_files' must be an integer from 1 to {MAX_FILES_LIMIT}"

    include_links = arguments.get("include_links", True)
    if not isinstance(include_links, bool):
        return None, "Error: 'include_links' must be a boolean"

    name_contains = arguments.get("name_contains")
    if name_contains is not None and (not isinstance(name_contains, str) or not name_contains):
        return None, "Error: 'name_contains' must be a non-empty string"

    return {
        "list_projects": list_projects,
        "project": project.strip() if project else None,
        "segments": segments,
        "depth": depth,
        "max_files": max_files,
        "include_links": include_links,
        "name_contains": name_contains,
    }, None


def time_budget():
    return max(_MIN_BUDGET_S, settings.MCP_MAIN_THREAD_TIMEOUT - _BUDGET_MARGIN_S)


def _hub_info(data):
    hub = data.activeHub
    return {"name": hub.name, "id": hub.id} if hub is not None else None


def _ok(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1)}], "isError": False}


def project_tree(arguments):
    """List a project's folders and files (with links resolved to paths)."""
    options, problem = validate_arguments(arguments)
    if problem:
        return _error(problem)

    started = time.monotonic()
    try:
        data = get_app().data
        projects = data_tree.items(data.dataProjects)

        if options["list_projects"]:
            listing = sorted(({"name": p.name, "id": p.id} for p in projects),
                             key=lambda p: p["name"].casefold())
            return _ok({
                "hub": _hub_info(data),
                "projects": listing,
                # Same-name projects can only be told apart by id.
                "duplicate_project_names": data_tree.case_collisions([p["name"] for p in listing]),
                "elapsed_seconds": round(time.monotonic() - started, 2),
            })

        try:
            project = data_tree.find_project(projects, options["project"])
            folder, actual = data_tree.resolve_folder(project.rootFolder, options["segments"])
        except LookupError as exc:
            return _error(f"Error: {exc}")

        start_path = data_tree.join_path(project.name, *actual)
        tree, summary = data_tree.build_tree(
            folder, start_path,
            depth=options["depth"],
            max_files=options["max_files"],
            name_contains=options["name_contains"],
            include_links=options["include_links"],
            budget_s=time_budget(),
        )
        elapsed = time.monotonic() - started
        summary["elapsed_seconds"] = round(elapsed, 2)
        log(f"[MCP] project_tree {start_path}: {summary['files_listed']} files, "
            f"{summary['folders_visited']} folders in {elapsed:.2f}s"
            + (f" (truncated: {summary['truncation']['reason']})" if summary["truncated"] else ""))
        return _ok({
            "hub": _hub_info(data),
            "project": {"name": project.name, "id": project.id},
            "root": start_path,
            "summary": summary,
            "notes": NOTES,
            "tree": tree,
        })
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] project_tree failed: {exc}")
        return _error(f"Error: {exc}\n{tb}")

