"""Walk and address Fusion cloud data (projects, folders, files) by path.

Nothing here imports ``adsk``: every function is duck-typed over objects
shaped like ``DataProject`` / ``DataFolder`` / ``DataFile`` so the logic can
be tested with fakes and reused by tools that move or rename items.

Gotchas this module encodes:

- Folder names are case-insensitive in Fusion: ``Tubes`` and ``tubes`` cannot
  coexist under one parent, and ``DataFolders.itemByName`` matches either.
  Path lookups here match case-insensitively and refuse to guess when two
  siblings differ only by case.
- File names are NOT unique: a folder (or the tree) can hold several files
  with the same name. Links ("uses" / "used by") must be resolved by id, never
  by name; a name alone is ambiguous across copies.
- Every property read on a cloud object may be a round trip, so paths are
  cached by folder id and the walk honours a file cap and a time budget.
"""

import time
from datetime import datetime, timezone

from .api import safe, strict_items

ID_PREFIX = "urn:"
MAX_DEPTH = 50
_MAX_PARENT_HOPS = 64

FILE_TYPES = {
    "f3d": "Fusion design",
    "f3z": "Fusion archive",
    "f2d": "Fusion drawing",
    "cam360": "Fusion CAM",
    "fsch": "Fusion electronics schematic",
    "fbrd": "Fusion electronics board",
    "flbr": "Fusion electronics library",
}


# -- Collection / attribute helpers ----------------------------------------


def items(collection):
    """Members of a cloud collection; ``asArray`` fetches them in one go where it exists."""
    as_array = getattr(collection, "asArray", None)
    if callable(as_array):
        return list(as_array())
    return strict_items(collection)


def iso_utc(epoch_seconds):
    """UNIX epoch seconds (``DataFile.dateModified``) to ISO-8601 UTC, or None."""
    if not isinstance(epoch_seconds, (int, float)) or isinstance(epoch_seconds, bool) or epoch_seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def file_type(extension):
    ext = (extension or "").lower().lstrip(".")
    return FILE_TYPES.get(ext, ext.upper() + " file" if ext else "unknown")


# -- Path parsing and lookup -----------------------------------------------


def path_segments(path):
    """Split "Folder/Sub/File" into non-empty segments; the last is the file."""
    return [part.strip() for part in path.replace("\\", "/").split("/") if part.strip()]


def split_path(path, what="folder path"):
    """Split a folder path ("a/b/c", "/a/b/", "") into segments.

    Raises ValueError for '.' / '..' segments, which Fusion has no notion of.
    *what* names the path in those messages.
    """
    if path is None:
        return []
    if not isinstance(path, str):
        raise ValueError(f"{what} must be a string")
    segments = path_segments(path)
    for seg in segments:
        if seg in (".", ".."):
            raise ValueError(f"{what} may not contain {seg!r}")
    return segments


def join_path(*parts):
    return "/".join(p for p in parts if p)


def match_name(objects, name):
    """Return (exact, case_insensitive) matches of *name* among *objects*."""
    exact = [o for o in objects if o.name == name]
    folded = [o for o in objects if o.name.casefold() == name.casefold()]
    return exact, folded


def find_project(projects, name_or_id):
    """Pick one project by id, exact name, then case-insensitive name.

    Raises LookupError with a message listing the candidates when the name is
    missing or ambiguous (two projects can share a name; pass the id then).
    """
    by_id = [p for p in projects if p.id == name_or_id]
    if by_id:
        return by_id[0]
    exact, folded = match_name(projects, name_or_id)
    candidates = exact or folded
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        names = sorted({p.name for p in projects}, key=str.casefold)
        shown = ", ".join(repr(n) for n in names[:50])
        more = f" (+{len(names) - 50} more)" if len(names) > 50 else ""
        raise LookupError(f"No project named {name_or_id!r} in the active hub. Projects: {shown}{more}")
    listed = "; ".join(f"{p.name!r} id={p.id}" for p in candidates)
    raise LookupError(f"Project name {name_or_id!r} is ambiguous; pass the project id instead: {listed}")


class AmbiguousFolder(LookupError):
    """Several sibling folders match one path segment."""


def find_child_folder(parent, name):
    """Find the subfolder of *parent* named *name* (case-insensitive, like Fusion).

    Returns the folder. Raises LookupError when it is missing and
    AmbiguousFolder when several siblings match (differing only by case, or
    true duplicates).
    """
    children = items(parent.dataFolders)
    exact, folded = match_name(children, name)
    if len(exact) == 1 and len(folded) == 1:
        return exact[0]
    if len(folded) == 1:
        return folded[0]
    if not folded:
        names = sorted((c.name for c in children), key=str.casefold)
        raise LookupError(
            f"No folder {name!r} in {parent.name!r}. Subfolders: "
            + (", ".join(repr(n) for n in names) or "(none)")
        )
    listed = "; ".join(f"{c.name!r} id={c.id}" for c in folded)
    raise AmbiguousFolder(
        f"Folder name {name!r} matches several folders in {parent.name!r} "
        f"(Fusion folder names are case-insensitive): {listed}"
    )


def resolve_folder(root, segments):
    """Walk *segments* down from *root*; return (folder, segments_as_named_in_fusion)."""
    folder = root
    actual = []
    for seg in segments:
        folder = find_child_folder(folder, seg)
        actual.append(folder.name)
    return folder, actual


def resolve_project(projects, wanted, match):
    """The project with id *wanted*, else the one *match(projects, wanted, "project")* picks."""
    by_id = [p for p in projects if safe(p, "id") == wanted]
    return by_id[0] if by_id else match(projects, wanted, "project")


def resolve_path(root, segments, match):
    """The file at *segments* below *root*; *match(objects, name, label)* picks each step."""
    folder = root
    for segment in segments[:-1]:
        folder = match(strict_items(folder.dataFolders), segment, "folder")
    return match(strict_items(folder.dataFiles), segments[-1], "file")


def case_collisions(names):
    """Groups of names that are equal ignoring case (Fusion treats them as one)."""
    groups = {}
    for n in names:
        groups.setdefault(n.casefold(), []).append(n)
    return [sorted(g) for g in groups.values() if len(g) > 1]


# -- Path resolution by id --------------------------------------------------


class PathResolver:
    """Resolve "Project/folder/sub" paths for folders and files, cached by folder id."""

    def __init__(self):
        self._folder_paths = {}

    def remember(self, folder_id, path):
        if folder_id:
            self._folder_paths[folder_id] = path

    def folder_path(self, folder):
        chain = []
        current = folder
        prefix = None
        for _ in range(_MAX_PARENT_HOPS):
            if current is None:
                break
            cached = self._folder_paths.get(current.id)
            if cached is not None:
                prefix = cached
                break
            if current.isRoot:
                project = current.parentProject
                prefix = project.name if project is not None else current.name
                self.remember(current.id, prefix)
                break
            chain.append(current)
            current = current.parentFolder
        path = prefix or "?"
        for f in reversed(chain):
            path = join_path(path, f.name)
            self.remember(f.id, path)
        return path

    def file_path(self, data_file):
        return join_path(self.folder_path(data_file.parentFolder), data_file.name)


# -- Addressing files by path or id -----------------------------------------


class Refusal(Exception):
    """A planning problem; *problem* is a dict reported to the caller."""

    def __init__(self, problem):
        super().__init__(problem.get("message", ""))
        self.problem = problem


def problem(kind, message, **extra):
    return dict({"problem": kind, "message": message}, **extra)


def project_arg(arguments):
    """The required 'project' argument, stripped; raises ValueError."""
    project = arguments.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("'project' must be a non-empty string (project name or id)")
    return project.strip()


def describe_file(data_file, resolver):
    return {"path": resolver.file_path(data_file), "version": data_file.versionNumber, "id": data_file.id}


def resolve_file(project, spec, find_by_id, resolver, describe=describe_file):
    """Resolve a file path or id inside *project*; raise Refusal when it is missing or ambiguous.

    *describe(data_file, resolver)* writes the candidate rows of an ambiguous path.
    """
    if spec.startswith(ID_PREFIX):
        try:
            data_file = find_by_id(spec)
        except Exception as exc:
            raise Refusal(problem("not_found", f"No file with id {spec!r}: {exc}", file=spec))
        if data_file is None:
            raise Refusal(problem("not_found", f"No file with id {spec!r}", file=spec))
        owner = data_file.parentProject
        if owner is not None and owner.id != project.id:
            raise Refusal(problem(
                "wrong_project", f"File {spec!r} is in project {owner.name!r}, not {project.name!r}", file=spec))
        return data_file

    try:
        segments = split_path(spec, "path")
    except ValueError as exc:
        raise Refusal(problem("bad_path", str(exc), file=spec))
    if not segments:
        raise Refusal(problem("bad_path", "file path is empty", file=spec))
    try:
        folder, _ = resolve_folder(project.rootFolder, segments[:-1])
    except AmbiguousFolder as exc:
        raise Refusal(problem("ambiguous", str(exc), file=spec))
    except LookupError as exc:
        raise Refusal(problem("not_found", str(exc), file=spec))

    wanted = segments[-1].casefold()
    files = items(folder.dataFiles)
    matches = [f for f in files
               if f.name.casefold() == wanted or f"{f.name}.{f.fileExtension}".casefold() == wanted]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        names = sorted({f.name for f in files}, key=str.casefold)
        raise Refusal(problem(
            "not_found",
            f"No file {segments[-1]!r} in {resolver.folder_path(folder)!r}. Files: "
            + (", ".join(repr(n) for n in names) or "(none)"),
            file=spec))
    raise Refusal(problem(
        "ambiguous",
        f"{spec!r} matches {len(matches)} files (names are not unique); pass one of these ids instead",
        file=spec,
        candidates=[describe(f, resolver) for f in sorted(matches, key=lambda f: f.id)]))


def open_file_ids(documents):
    """Return ``(id -> {document, visible}, unreadable document names)`` for open documents.

    Hidden documents count: opening a parent loads the files it links.
    """
    opened, unreadable = {}, []
    for doc in items(documents):
        try:
            data_file = doc.dataFile
            if data_file is not None:  # unsaved documents have no data file
                opened[data_file.id] = {"document": doc.name, "visible": bool(getattr(doc, "isVisible", True))}
        except Exception:
            unreadable.append(getattr(doc, "name", "?"))
    return opened, unreadable


# -- Tree walk --------------------------------------------------------------


class _Walk:
    def __init__(self, *, depth, max_files, name_contains, include_links, budget_s, clock, resolver):
        self.depth = depth
        self.max_files = max_files
        self.needle = name_contains.casefold() if name_contains else None
        self.include_links = include_links
        self.clock = clock
        self.deadline = clock() + budget_s if budget_s else None
        self.resolver = resolver
        self.files = []          # every listed file node, in walk order
        self.file_ids = {}       # id -> [file nodes]
        self.folder_count = 0
        self.empty_folders = []
        self.depth_limited = []
        self.collisions = []
        self.pruned = 0
        self.truncated = None    # {"reason", "at"} once the walk stops early
        self.link_errors = 0

    def _out_of_time(self):
        return self.deadline is not None and self.clock() >= self.deadline

    def _stop(self, reason, path):
        if self.truncated is None:
            self.truncated = {"reason": reason, "at": path}

    def file_node(self, data_file, folder_path):
        version = data_file.versionNumber
        latest = data_file.latestVersionNumber
        node = {
            "name": data_file.name,
            "extension": data_file.fileExtension,
            "type": file_type(data_file.fileExtension),
            "version": version,
            "is_read_only": data_file.isReadOnly,
            "date_modified": iso_utc(data_file.dateModified),
            "id": data_file.id,
            "path": join_path(folder_path, data_file.name),
        }
        if isinstance(latest, int) and latest != version:
            node["latest_version"] = latest
        if self.include_links:
            self._add_links(node, data_file)
        return node

    def _ref(self, ref):
        node = {"name": ref.name, "path": self.resolver.file_path(ref), "version": ref.versionNumber, "id": ref.id}
        latest = ref.latestVersionNumber
        if isinstance(latest, int) and latest != ref.versionNumber:
            node["latest_version"] = latest
        return node

    def _add_links(self, node, data_file):
        try:
            if data_file.hasChildReferences:
                node["uses"] = [self._ref(r) for r in items(data_file.childReferences)]
                if data_file.hasOutofDateChildReferences:
                    node["has_out_of_date_links"] = True
            if data_file.hasParentReferences:
                node["used_by"] = [self._ref(r) for r in items(data_file.parentReferences)]
        except Exception as exc:  # one bad file must not sink the whole walk
            node["links_error"] = f"{type(exc).__name__}: {exc}"
            self.link_errors += 1

    def folder(self, folder, path, level):
        """Return the folder node, or None when a name filter pruned it."""
        self.folder_count += 1
        self.resolver.remember(folder.id, path)
        node = {"name": folder.name, "path": path, "id": folder.id}
        if self._out_of_time():
            self._stop("time_budget", path)
            node["not_listed"] = True
            return node

        data_files = items(folder.dataFiles)
        subfolders = items(folder.dataFolders)
        if not data_files and not subfolders:
            node["empty"] = True
            self.empty_folders.append(path)
        for group in case_collisions([f.name for f in subfolders]):
            self.collisions.append({"parent": path, "names": group})

        files = []
        for data_file in sorted(data_files, key=lambda f: (f.name.casefold(), f.id)):
            if self.truncated is not None:
                break
            if self.needle and self.needle not in data_file.name.casefold():
                continue
            if len(self.files) >= self.max_files:
                self._stop("max_files", path)
                break
            if self._out_of_time():
                self._stop("time_budget", path)
                break
            fnode = self.file_node(data_file, path)
            files.append(fnode)
            self.files.append(fnode)
            self.file_ids.setdefault(fnode["id"], []).append(fnode)

        children = []
        if subfolders and level >= self.depth:
            node["subfolders_not_listed"] = len(subfolders)
            self.depth_limited.append(path)
        else:
            for sub in sorted(subfolders, key=lambda f: (f.name.casefold(), f.id)):
                if self.truncated is not None:
                    break
                child = self.folder(sub, join_path(path, sub.name), level + 1)
                if child is not None:
                    children.append(child)

        if children:
            node["folders"] = children
        if files:
            node["files"] = files
        if self.needle and not files and not children and level > 0:
            self.pruned += 1
            return None
        return node


def duplicate_names(file_nodes):
    """Group listed files sharing a name (case-insensitive) so each copy is distinguishable."""
    groups = {}
    for node in file_nodes:
        groups.setdefault(node["name"].casefold(), []).append(node)
    dups = []
    for group in groups.values():
        if len(group) > 1:
            for node in group:
                node["duplicate_name"] = True
            dups.append({
                "name": group[0]["name"],
                "copies": [{"path": n["path"], "version": n["version"], "id": n["id"]} for n in group],
            })
    dups.sort(key=lambda d: d["name"].casefold())
    return dups


def _mark_in_tree(file_nodes, ids):
    for node in file_nodes:
        for key in ("uses", "used_by"):
            for ref in node.get(key, ()):
                ref["in_tree"] = ref["id"] in ids


def build_tree(start_folder, start_path, *, depth=3, max_files=500, name_contains=None,
               include_links=True, budget_s=None, clock=time.monotonic, resolver=None):
    """Walk *start_folder* and return ``(tree, summary)``.

    *tree* is nested folder nodes (``folders`` / ``files`` lists); *summary*
    holds counts, duplicate-name groups, empty folders, folder-name case
    collisions and truncation info. Pure over duck-typed Data* objects.
    """
    resolver = resolver or PathResolver()
    walk = _Walk(depth=depth, max_files=max_files, name_contains=name_contains,
                 include_links=include_links, budget_s=budget_s, clock=clock, resolver=resolver)
    tree = walk.folder(start_folder, start_path, 0)
    if include_links:
        _mark_in_tree(walk.files, walk.file_ids)
    summary = {
        "folders_visited": walk.folder_count,
        "files_listed": len(walk.files),
        "truncated": walk.truncated is not None,
    }
    if walk.truncated is not None:
        summary["truncation"] = walk.truncated
    if walk.depth_limited:
        summary["depth_limited_folders"] = walk.depth_limited
    summary["duplicate_names"] = duplicate_names(walk.files)
    summary["empty_folders"] = walk.empty_folders
    if walk.collisions:
        summary["folder_case_collisions"] = walk.collisions
    if walk.pruned:
        summary["folders_pruned_by_filter"] = walk.pruned
    if walk.link_errors:
        summary["files_with_link_errors"] = walk.link_errors
    return tree, summary
