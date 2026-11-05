"""Read-only listing of a Fusion cloud project as a folder/file tree.

The walk and path logic live in ``data_tree`` (Fusion-free); this module
validates arguments, finds the project in the active hub and formats the
result. Cloud reads are slow, so the walk stops early on ``max_files`` or a
time budget kept below the main-thread dispatch timeout, and says so.
"""

import time
import traceback

from .. import settings
from . import data_tree
from .common.args import bool_arg, error_pair, int_arg, reject_unknown, require_object, text_arg
from .common.results import error_result, ok_result
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


@error_pair
def validate_arguments(arguments):
    """Return (options, None) or (None, error_text). Touches nothing in Fusion."""
    require_object(arguments)
    reject_unknown(arguments, _KNOWN_ARGS)
    list_projects = bool_arg(arguments, "list_projects")
    project = text_arg(arguments, "project", suffix=" (project name or id)")
    if not list_projects and project is None:
        raise ValueError("'project' is required unless 'list_projects' is true")

    try:
        segments = data_tree.split_path(arguments.get("folder"))
    except ValueError as exc:
        raise ValueError(f"'folder': {exc}")

    depth = int_arg(arguments, "depth", DEFAULT_DEPTH, 0, data_tree.MAX_DEPTH)
    max_files = int_arg(arguments, "max_files", DEFAULT_MAX_FILES, 1, MAX_FILES_LIMIT)
    include_links = bool_arg(arguments, "include_links", True)

    name_contains = arguments.get("name_contains")
    if name_contains is not None and (not isinstance(name_contains, str) or not name_contains):
        raise ValueError("'name_contains' must be a non-empty string")

    return {
        "list_projects": list_projects,
        "project": project.strip() if project else None,
        "segments": segments,
        "depth": depth,
        "max_files": max_files,
        "include_links": include_links,
        "name_contains": name_contains,
    }


def time_budget():
    return max(_MIN_BUDGET_S, settings.MCP_MAIN_THREAD_TIMEOUT - _BUDGET_MARGIN_S)


def _hub_info(data):
    hub = data.activeHub
    return {"name": hub.name, "id": hub.id} if hub is not None else None


def project_tree(arguments):
    """List a project's folders and files (with links resolved to paths)."""
    options, problem = validate_arguments(arguments)
    if problem:
        return error_result(problem)

    started = time.monotonic()
    try:
        data = get_app().data
        projects = data_tree.items(data.dataProjects)

        if options["list_projects"]:
            listing = sorted(({"name": p.name, "id": p.id} for p in projects),
                             key=lambda p: p["name"].casefold())
            return ok_result({
                "hub": _hub_info(data),
                "projects": listing,
                # Same-name projects can only be told apart by id.
                "duplicate_project_names": data_tree.case_collisions([p["name"] for p in listing]),
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }, indent=1)

        try:
            project = data_tree.find_project(projects, options["project"])
            folder, actual = data_tree.resolve_folder(project.rootFolder, options["segments"])
        except LookupError as exc:
            return error_result(f"Error: {exc}")

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
        return ok_result({
            "hub": _hub_info(data),
            "project": {"name": project.name, "id": project.id},
            "root": start_path,
            "summary": summary,
            "notes": NOTES,
            "tree": tree,
        }, indent=1)
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"[MCP] project_tree failed: {exc}")
        return error_result(f"Error: {exc}\n{tb}")

