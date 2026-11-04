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
    """Return the members of a Fusion collection (or any iterable) as a list."""
    if collection is None:
        return []
    as_array = getattr(collection, "asArray", None)
    if callable(as_array):
        return list(as_array())
    count = getattr(collection, "count", None)
    item = getattr(collection, "item", None)
    if isinstance(count, int) and callable(item):
        return [item(i) for i in range(count)]
    return list(collection)


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


def split_path(path):
    """Split a folder path ("a/b/c", "/a/b/", "") into segments.

    Raises ValueError for '.' / '..' segments, which Fusion has no notion of.
    """
    if path is None:
        return []
    if not isinstance(path, str):
        raise ValueError("folder path must be a string")
    segments = [s.strip() for s in path.replace("\\", "/").split("/")]
    segments = [s for s in segments if s]
    for seg in segments:
        if seg in (".", ".."):
            raise ValueError(f"folder path may not contain {seg!r}")
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


def find_child_folder(parent, name):
    """Find the subfolder of *parent* named *name* (case-insensitive, like Fusion).

    Returns the folder, or raises LookupError when it is missing or when
    several siblings match (differing only by case, or true duplicates).
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
    raise LookupError(
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
