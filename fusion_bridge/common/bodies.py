"""Enumerate the B-Rep bodies of a design, duck-typed so fakes can drive it.

Bodies inside occurrences are read through the occurrence's body proxies, whose
geometry Fusion returns in the root assembly context, so everything here is in
world coordinates. A body hidden by itself or by its occurrence is often stale
reference geometry; callers decide what to do with one (``is_hidden``).
"""

from .api import safe, strict_items
from .geometry import xyz

ROOT_PATH = "root"


def iter_body_entries(root):
    """Yield one entry per B-Rep body: root bodies, then every occurrence's proxies.

    Keys starting with ``_`` hold Fusion objects and never reach a report.
    """
    containers = [(None, root)]
    containers.extend((occ, occ) for occ in strict_items(safe(root, "allOccurrences")))
    for occ, owner in containers:
        component = safe(occ, "component") if occ is not None else root
        path = safe(occ, "fullPathName") if occ is not None else ROOT_PATH
        for index, body in enumerate(strict_items(safe(owner, "bRepBodies"))):
            yield {
                "occurrence_path": path,
                "component": safe(component, "name"),
                "linked": bool(safe(occ, "isReferencedComponent", False)) if occ is not None else False,
                "body": safe(body, "name"),
                "index": index,
                "visible": safe(body, "isVisible"),
                "occurrence_visible": safe(occ, "isVisible") if occ is not None else None,
                "_body": body,
            }


def find_occurrence(root, path):
    """The occurrence whose ``fullPathName`` is *path*, or None."""
    return next((occ for occ in strict_items(safe(root, "allOccurrences"))
                 if safe(occ, "fullPathName") == path), None)


def is_hidden(entry):
    """Hidden if the body is hidden or its owning occurrence is."""
    return entry["visible"] is False or entry["occurrence_visible"] is False


def bbox(entity):
    """``(min, max)`` corners of the entity's bounding box in cm, or None."""
    box = safe(entity, "boundingBox")
    return None if box is None else (xyz(box.minPoint), xyz(box.maxPoint))


def native(entity):
    """The native object behind a proxy; *entity* itself when it is not one."""
    found = safe(entity, "nativeObject")
    return found if found is not None else entity
