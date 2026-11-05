"""Enumerate the B-Rep bodies of a design, duck-typed so fakes can drive it.

Bodies inside occurrences are read through the occurrence's body proxies, whose
geometry Fusion returns in the root assembly context, so everything here is in
world coordinates. A body hidden by itself or by its occurrence is often stale
reference geometry; callers decide what to do with one (``is_hidden``).
"""

from .api import safe, strict_items
from .args import is_int
from .geometry import xyz

ROOT_PATH = "root"
# How far a temporary copy's box may sit from its source's and still be the same place.
PLACEMENT_TOLERANCE_CM = 1e-3


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


def bbox_overlap(a, b, tol=0.0):
    """True when two (min, max) boxes overlap (or touch within tol)."""
    if a is None or b is None:
        return True
    (amin, amax), (bmin, bmax) = a, b
    return all(amin[i] <= bmax[i] + tol and bmin[i] <= amax[i] + tol for i in range(3))


def bbox_shift(before, after):
    """Largest movement of any box coordinate; None when a box is missing."""
    if before is None or after is None:
        return None
    return max(abs(a - b) for pb, pa in zip(before, after) for b, a in zip(pb, pa))


def label(target):
    return f"{target['occurrence_path']}/{target['body']}"


def body_entries(root):
    """Every body in the design, read through occurrence proxies (world coordinates)."""
    for entry in iter_body_entries(root):
        body = entry["_body"]
        yield {
            "occurrence_path": entry["occurrence_path"],
            "body": entry["body"],
            "index": entry["index"],
            "visible": not is_hidden(entry),
            "solid": bool(safe(body, "isSolid", False)),
            "bbox": bbox(body),
            "_body": body,
        }


def parse_body_ref(value, where):
    """A BodyRef object as ``(occurrence_path, body_name, body_index)``; raises ValueError."""
    if not isinstance(value, dict) or set(value) - {"occurrence_path", "body_name", "body_index"}:
        raise ValueError(f"{where} takes occurrence_path, body_name and optional body_index")
    path, name, index = value.get("occurrence_path"), value.get("body_name"), value.get("body_index")
    if not isinstance(path, str) or not path.strip() or not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where} needs a non-empty occurrence_path and body_name")
    if index is not None and (not is_int(index) or index < 0):
        raise ValueError("'body_index' must be a non-negative integer")
    return path, name, index


def resolve_targets(entries, wanted):
    """Pick the bodies to sample. Returns (targets, warnings, error_payload).

    Without *wanted*, every visible solid. Otherwise each request must match
    exactly one entry; a miss or an ambiguity is refused with candidates, since
    sampling the wrong body answers the question wrongly without looking wrong.
    """
    def brief(e):
        return {"occurrence_path": e["occurrence_path"], "body_name": e["body"], "body_index": e["index"],
                "visible": e["visible"], "solid": e["solid"]}

    if wanted is None:
        targets = [e for e in entries if e["visible"] and e["solid"]]
        if not targets:
            return None, [], {"error": "the active design has no visible solid bodies"}
        return targets, [], None

    targets, warnings = [], []
    for path, name, index in wanted:
        matches = [e for e in entries if e["occurrence_path"] == path and e["body"] == name
                   and (index is None or e["index"] == index)]
        if len(matches) != 1:
            near = [e for e in entries if e["body"] == name or e["occurrence_path"] == path]
            return None, [], {
                "error": (f"ambiguous: several bodies match {path}/{name}; pass body_index" if matches
                          else f"no body matches {path}/{name}"),
                "candidates": [brief(e) for e in (matches or near)[:50]],
            }
        target = matches[0]
        if target not in targets:
            targets.append(target)
        if not target["solid"]:
            warnings.append(f"{label(target)} is not solid: containment and intervals are not meaningful for it")
    return targets, warnings, None


def temp_copy(manager, body):
    """A temporary copy of *body* sitting where *body* sits, in world coordinates.

    *manager* is the ``TemporaryBRepManager``. The copy of an occurrence proxy
    may come back in component space, so it is compared with the proxy's box and
    moved by the occurrence's transform when it landed elsewhere. Raises
    RuntimeError when no copy can be placed: a misplaced copy would compare
    wrongly without looking wrong.
    """
    def placed(copy):
        shift = bbox_shift(bbox(copy), bbox(body))
        return shift is not None and shift <= PLACEMENT_TOLERANCE_CM

    copy = manager.copy(body)
    if copy is None:
        raise RuntimeError(f"no temporary copy of {safe(body, 'name')}")
    if placed(copy):
        return copy
    occurrence = safe(body, "assemblyContext")
    if occurrence is not None and manager.transform(copy, occurrence.transform2) and placed(copy):
        return copy
    raise RuntimeError(f"the temporary copy of {safe(body, 'name')} did not land on the body")
