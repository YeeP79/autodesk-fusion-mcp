"""Temporarily hide non-body clutter (sketches, construction, joints...) for a capture.

Fusion-free bookkeeping: every flag is read and written with getattr/setattr so
tests can drive it with plain fake objects. Bodies are never touched.
"""

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


def validate_keep(value):
    """Return the keep list as a tuple of known categories."""
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in CATEGORIES for item in value):
        raise ValueError(f"clean_view_keep must be a list of: {', '.join(CATEGORIES)}")
    return tuple(c for c in CATEGORIES if c in value)


def _components(design):
    """Root plus every component reachable from the design, deduplicated."""
    found = []

    def add(component):
        if component is not None and not any(component == seen for seen in found):
            found.append(component)

    root = design.rootComponent
    add(root)
    for component in design.allComponents:
        add(component)
    # Occurrences reach linked (referenced) components even if allComponents omits them.
    for occurrence in root.allOccurrences:
        add(occurrence.component)
    return found


def _component_label(component):
    try:
        return f"component '{component.name}'"
    except Exception:
        return "component"


def plan(design, keep=()):
    """List (category, label, target, attribute) toggles to switch off."""
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
    if folder_categories:
        for component in _components(design):
            label = _component_label(component)
            for category in folder_categories:
                for attribute in FOLDER_FLAGS[category]:
                    toggles.append((category, f"{label}.{attribute}", component, attribute))
    return toggles


class CleanView:
    """Switch planned flags off, remember originals, and put them back."""

    def __init__(self, toggles, keep=()):
        self.toggles = toggles
        self.keep = tuple(keep)
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
        warnings = list(self.warnings)
        if self.restore_failures:
            warnings.append("RESTORE FAILED; these remain hidden: " + "; ".join(self.restore_failures))
        if warnings:
            result["warnings"] = warnings
        return result
