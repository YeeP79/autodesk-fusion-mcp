"""Temporarily hide non-body clutter (sketches, construction, joints...) for a capture.

Fusion-free bookkeeping: every flag is read and written with getattr/setattr so
tests can drive it with plain fake objects. Bodies are never touched.

A capture should not leave files dirty, and restoring a flag does not undo that:
a light bulb written on a component marks its document modified, and the API has
no way to clear the mark. So a folder with nothing in it is not touched, and the
folders of a linked component, which live in another document, are left alone
unless the caller asks. ``ModifiedWatch`` reports what was dirtied anyway.
"""

from .common.api import safe
from .common.bodies import is_under

CATEGORIES = ("sketches", "construction", "origin", "joints", "canvases", "decals")

# Design-wide Navigation bar "Object Visibility" flags (Design.objectVisibility).
# They cover every component, local or referenced, in one place.
DESIGN_FLAGS = {
    "sketches": ("isSketchesVisible",),
    "construction": ("isUserWorkPlanesVisible", "isUserWorkAxesVisible", "isUserWorkPointsVisible"),
    "origin": ("isOriginPlanesVisible", "isOriginAxesVisible", "isOriginPointsVisible"),
    "joints": ("isJointsVisible", "isJointOriginsVisible", "isJointOriginAxesVisible"),
}
# Per-component browser folder light bulbs. Canvases and decals have no
# Object Visibility flag; the rest are a fallback when objectVisibility is absent.
FOLDER_FLAGS = {
    "sketches": ("isSketchFolderLightBulbOn",),
    "construction": ("isConstructionFolderLightBulbOn",),
    "origin": ("isOriginFolderLightBulbOn",),
    "joints": ("isJointsFolderLightBulbOn", "isJointOriginsFolderLightBulbOn"),
    "canvases": ("isCanvasFolderLightBulbOn",),
    "decals": ("isDecalFolderLightBulbOn",),
}
# What fills each folder. Origin has no count: it is always there.
FOLDER_CONTENT = {
    "sketches": ("sketches",),
    "construction": ("constructionPlanes", "constructionAxes", "constructionPoints"),
    "joints": ("joints", "jointOrigins"),
    "canvases": ("canvases",),
    "decals": ("decals",),
}
MAX_LISTED = 20
MODIFIED_WARNING = ("marked modified during this capture; the API cannot clear that, so save them or close them "
                    "without saving")


def validate_keep(value):
    """Return the keep list as a tuple of known categories."""
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in CATEGORIES for item in value):
        raise ValueError(f"clean_view_keep must be a list of: {', '.join(CATEGORIES)}")
    return tuple(c for c in CATEGORIES if c in value)


def _referenced(occurrence):
    return safe(occurrence, "isReferencedComponent") is not False


def _components(design):
    """``(local, linked)``: every component reachable from the design, deduplicated.

    A component is linked when an occurrence of it is, or sits inside one. An
    unreadable flag counts as linked, the side that writes nothing.
    """
    found = []

    def add(component):
        if component is not None and not any(component == seen for seen in found):
            found.append(component)

    root = design.rootComponent
    add(root)
    for component in design.allComponents:
        add(component)
    # Occurrences reach linked (referenced) components even if allComponents omits them.
    occurrences = list(root.allOccurrences)
    for occurrence in occurrences:
        add(occurrence.component)
    paths = {safe(occurrence, "fullPathName") for occurrence in occurrences if _referenced(occurrence)}
    held = [occurrence.component for occurrence in occurrences
            if _referenced(occurrence) or is_under(safe(occurrence, "fullPathName") or "", paths)]
    linked = [component for component in found if any(component == seen for seen in held)]
    return [component for component in found if not any(component == seen for seen in held)], linked


def _component_label(component):
    try:
        return f"component '{component.name}'"
    except Exception:
        return "component"


def holds(component, category):
    """Does this folder have anything in it? None when that cannot be read."""
    counts = [safe(safe(component, name), "count") for name in FOLDER_CONTENT.get(category, ())]
    if any(counts):
        return True
    return None if not counts or None in counts else False


def _plan(design, keep, include_linked):
    """``(toggles, what stays showing, linked components left alone)``."""
    if design is None or not hasattr(design, "rootComponent"):
        raise ValueError("clean_view requires an active Fusion design")
    hide = [c for c in CATEGORIES if c not in keep]
    visibility = getattr(design, "objectVisibility", None)
    toggles = []
    folder_categories = []
    for category in hide:
        if visibility is not None and category in DESIGN_FLAGS:
            for attribute in DESIGN_FLAGS[category]:
                toggles.append((category, f"objectVisibility.{attribute}", visibility, attribute))
        else:
            folder_categories.append(category)
    if not folder_categories:
        return toggles, [], 0
    local, linked = _components(design)
    showing = []
    for component in [*local, *(linked if include_linked else ())]:
        label = _component_label(component)
        for category in folder_categories:
            if holds(component, category) is not False:
                for attribute in FOLDER_FLAGS[category]:
                    toggles.append((category, f"{label}.{attribute}", component, attribute))
    if include_linked:
        return toggles, [], 0
    for component in linked:
        for category in folder_categories:
            found = holds(component, category)
            if found is not False:
                showing.append(f"{_component_label(component)}: {category}" + ("" if found else " (could not read)"))
    return toggles, showing, len(linked)


def plan(design, keep=(), include_linked=False):
    """List (category, label, target, attribute) toggles to switch off."""
    return _plan(design, keep, include_linked)[0]


def prepare(design, keep=(), include_linked=False):
    """A ``CleanView`` for *design*, knowing what it chose to leave alone."""
    toggles, showing, left_alone = _plan(design, keep, include_linked)
    return CleanView(toggles, keep, showing, left_alone)


class ModifiedWatch:
    """Which of the open documents, clean at the start, are modified now."""

    def __init__(self, documents):
        self._clean = [document for document in documents if safe(document, "isModified") is False]

    def newly_modified(self):
        return [safe(document, "name") or "unnamed document" for document in self._clean
                if safe(document, "isModified") is True]


class CleanView:
    """Switch planned flags off, remember originals, and put them back."""

    def __init__(self, toggles, keep=(), left_showing=(), linked_left_alone=0):
        self.toggles = toggles
        self.keep = tuple(keep)
        self.left_showing = list(left_showing)
        self.linked_left_alone = linked_left_alone
        self.applied = []   # (category, label, target, attribute, original)
        self.warnings = []
        self.restore_failures = []
        self.restored = None

    def hide(self):
        """Turn each visible flag off. Appends as it goes so a later failure can restore."""
        for category, label, target, attribute in self.toggles:
            try:
                original = getattr(target, attribute)
            except Exception as exc:
                self.warnings.append(f"could not read {label}: {exc}")
                continue
            if type(original) is not bool:
                self.warnings.append(f"skipped {label}: not a boolean")
                continue
            if not original:
                continue  # Already hidden; nothing to restore.
            try:
                setattr(target, attribute, False)
            except Exception as exc:
                self.warnings.append(f"could not hide {label}: {exc}")
                try:
                    changed = getattr(target, attribute) is not original
                except Exception:
                    changed = True  # Unknown state: restoring True is the safe direction.
                if not changed:
                    continue
            self.applied.append((category, label, target, attribute, original))

    def restore(self):
        """Write every original value back, newest first. Returns the labels that failed."""
        failures = []
        for category, label, target, attribute, original in reversed(self.applied):
            try:
                setattr(target, attribute, original)
                if getattr(target, attribute) != original:
                    raise ValueError("value did not change")
            except Exception as exc:
                failures.append(f"{label}: {exc}")
        self.restore_failures = failures
        self.restored = not failures
        return failures

    def report(self):
        hidden = {entry[0] for entry in self.applied}
        result = {
            "hidden": [c for c in CATEGORIES if c in hidden],
            "kept": list(self.keep),
            "restored": self.restored,
        }
        if self.linked_left_alone:
            result["linked_left_alone"] = self.linked_left_alone
        if self.left_showing:
            result["left_showing"] = self.left_showing[:MAX_LISTED]
            result["left_showing_total"] = len(self.left_showing)
        warnings = list(self.warnings)
        if self.restore_failures:
            warnings.append("RESTORE FAILED; these remain hidden: " + "; ".join(self.restore_failures))
        if warnings:
            result["warnings"] = warnings
        return result
