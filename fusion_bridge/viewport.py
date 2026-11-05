"""Explicit camera controls and PNG capture for the active Fusion viewport."""

import base64
import json
import math
import os
import re
import tempfile

import adsk.core

from ..lib import png_image

VIEW_NAMES = {
    "front": "FrontViewOrientation", "back": "BackViewOrientation",
    "left": "LeftViewOrientation", "right": "RightViewOrientation",
    "top": "TopViewOrientation", "bottom": "BottomViewOrientation",
    "isometric": "IsoTopRightViewOrientation",
    "iso_top_left": "IsoTopLeftViewOrientation", "iso_top_right": "IsoTopRightViewOrientation",
    "iso_bottom_left": "IsoBottomLeftViewOrientation", "iso_bottom_right": "IsoBottomRightViewOrientation",
}
PROJECTIONS = {
    "orthographic": "OrthographicCameraType", "perspective": "PerspectiveCameraType",
    "perspective_with_ortho_faces": "PerspectiveWithOrthoFacesCameraType",
}
CAPTURE_UNITS = {"in": 2.54, "cm": 1.0, "mm": 0.1}  # Fusion's camera works in cm.
# (screen right, toward the viewer in a front view, up) per model up axis.
# Fusion's standard views assume Y-up, so a front view of a Z-up model is its plan.
MODEL_AXES = {"z": ((1, 0, 0), (0, -1, 0), (0, 0, 1)), "y": ((1, 0, 0), (0, 0, 1), (0, 1, 0))}
MODEL_VIEWS = ("top", "bottom", "front", "back", "left", "right", "iso")
MODEL_VIEW_MARGIN = 1.1


def _fail(message):
    return {"content": [{"type": "text", "text": str(message)}], "isError": True}


def _success(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": False}


def _viewport():
    vp = adsk.core.Application.get().activeViewport
    if vp is None:
        raise ValueError("No active viewport")
    return vp


def _number(value, label, minimum=None, maximum=None):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{label} must be a finite number")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return float(value)


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be boolean")
    return value


def _object(value, label, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f"{label} requires {', '.join(required) or 'an object'}; allowed fields: {', '.join(allowed)}")
    return value


def _vector(value, label):
    _object(value, label, ("x", "y", "z"), ("x", "y", "z"))
    return tuple(_number(value[k], f"{label}.{k}", -1e12, 1e12) for k in ("x", "y", "z"))


def _xyz(value):
    return {"x": value.x, "y": value.y, "z": value.z}


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _scale(v, factor):
    return tuple(x * factor for x in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(v):
    length = math.hypot(*v)
    if length < 1e-12:
        raise ValueError("Camera eye/target must differ and up vector must not be parallel to the viewing direction")
    return _scale(v, 1 / length)


def _rotate(v, axis, degrees):
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    dot = sum(a * b for a, b in zip(axis, v))
    return _add(_add(_scale(v, cosine), _scale(_cross(axis, v), sine)), _scale(axis, dot * (1 - cosine)))


def _projection(camera):
    for name, attribute in PROJECTIONS.items():
        if camera.cameraType == getattr(adsk.core.CameraTypes, attribute):
            return name
    raise ValueError("Unknown Fusion camera projection")


def _extents(camera):
    ok, width, height = camera.getExtents()
    if not ok:
        raise ValueError("Fusion could not read orthographic camera extents")
    return {"width": width, "height": height}


def _state(vp):
    camera = vp.camera
    projection = _projection(camera)
    result = {"eye": _xyz(camera.eye), "target": _xyz(camera.target),
              "up_vector": _xyz(camera.upVector), "projection": projection}
    if projection == "orthographic":
        result["extents"] = _extents(camera)
    else:
        result["perspective_angle"] = math.degrees(camera.perspectiveAngle)
    return {"width": vp.width, "height": vp.height, "units": {"length": "cm", "angle": "degrees"}, "camera": result}


def get_viewport(arguments):
    """Return a camera snapshot that can be passed back to set_viewport."""
    try:
        return _success(_state(_viewport()))
    except Exception as exc:
        return _fail(f"Error reading viewport: {exc}")


def _validate_controls(arguments):
    _object(arguments, "arguments", ("camera", "view", "projection", "fit", "orbit", "pan", "zoom", "description"))
    if "view" in arguments and arguments["view"] not in VIEW_NAMES:
        raise ValueError("Unknown standard view")
    if "projection" in arguments and arguments["projection"] not in PROJECTIONS:
        raise ValueError("Unknown projection")
    if "fit" in arguments:
        _boolean(arguments["fit"], "fit")
    if "zoom" in arguments:
        _number(arguments["zoom"], "zoom", 0.01, 100)
    for field, names, bound in (("orbit", ("yaw", "pitch", "roll"), 360), ("pan", ("x", "y"), 1e9)):
        if field in arguments:
            _object(arguments[field], field, names)
            for key, value in arguments[field].items():
                _number(value, f"{field}.{key}", -bound, bound)
    if "camera" in arguments:
        if set(arguments) - {"camera", "description"}:
            raise ValueError("camera cannot be combined with relative controls, view, projection, or fit")
        pose = _object(arguments["camera"], "camera", ("eye", "target", "up_vector", "projection", "extents", "perspective_angle"), ("eye", "target", "up_vector", "projection"))
        direction = _unit(_sub(_vector(pose["target"], "target"), _vector(pose["eye"], "eye")))
        _unit(_cross(direction, _vector(pose["up_vector"], "up_vector")))
        if pose["projection"] not in PROJECTIONS:
            raise ValueError("Unknown camera projection")
        if pose["projection"] == "orthographic":
            extents = _object(pose.get("extents"), "extents", ("width", "height"), ("width", "height"))
            for key, value in extents.items():
                _number(value, f"extents.{key}", 1e-9, 1e12)
            if "perspective_angle" in pose:
                raise ValueError("Orthographic camera does not use perspective_angle")
        else:
            _number(pose.get("perspective_angle"), "perspective_angle", 0.01, 179)
            if "extents" in pose:
                raise ValueError("Perspective camera does not use extents")


def _set_pose(camera, pose):
    camera.cameraType = getattr(adsk.core.CameraTypes, PROJECTIONS[pose["projection"]])
    camera.eye = adsk.core.Point3D.create(*_vector(pose["eye"], "eye"))
    camera.target = adsk.core.Point3D.create(*_vector(pose["target"], "target"))
    camera.upVector = adsk.core.Vector3D.create(*_vector(pose["up_vector"], "up_vector"))
    if pose["projection"] == "orthographic":
        if not camera.setExtents(pose["extents"]["width"], pose["extents"]["height"]):
            raise ValueError("Fusion could not set camera extents")
    else:
        camera.perspectiveAngle = math.radians(pose["perspective_angle"])


def _apply_controls(vp, arguments):
    camera = vp.camera
    camera.isSmoothTransition = False
    camera.isFitView = False
    if "camera" in arguments:
        _set_pose(camera, arguments["camera"])
    else:
        if "projection" in arguments:
            camera.cameraType = getattr(adsk.core.CameraTypes, PROJECTIONS[arguments["projection"]])
        if "view" in arguments:
            camera.viewOrientation = getattr(adsk.core.ViewOrientations, VIEW_NAMES[arguments["view"]])
        if arguments.get("fit", False):
            camera.isFitView = True
        # Fit and standard views may change eye/extents when applied. Read the
        # actual result before applying relative operations.
        vp.camera = camera
        camera = vp.camera
        camera.isSmoothTransition = False
        camera.isFitView = False
        eye, target, up = (_vector(_xyz(v), "camera vector") for v in (camera.eye, camera.target, camera.upVector))
        back = _sub(eye, target)
        up = _unit(up)
        orbit = arguments.get("orbit", {})
        back = _rotate(back, up, orbit.get("yaw", 0))
        right = _unit(_cross(up, back))
        back = _rotate(back, right, orbit.get("pitch", 0))
        up = _rotate(up, right, orbit.get("pitch", 0))
        up = _rotate(up, _unit(_scale(back, -1)), orbit.get("roll", 0))
        right = _unit(_cross(up, back))
        screen_up = _unit(_cross(back, right))
        pan = arguments.get("pan", {})
        offset = _add(_scale(right, pan.get("x", 0)), _scale(screen_up, pan.get("y", 0)))
        target = _add(target, offset)
        zoom = arguments.get("zoom", 1)
        if _projection(camera) == "orthographic":
            extents = _extents(camera)
            if not camera.setExtents(extents["width"] / zoom, extents["height"] / zoom):
                raise ValueError("Fusion could not zoom the orthographic camera")
        else:
            back = _scale(back, 1 / zoom)
        camera.eye = adsk.core.Point3D.create(*_add(target, back))
        camera.target = adsk.core.Point3D.create(*target)
        camera.upVector = adsk.core.Vector3D.create(*up)
    vp.camera = camera
    vp.refresh()


def set_viewport(arguments):
    """Apply explicit camera state or ordered view/fit/orbit/pan/zoom controls."""
    vp = original = None
    try:
        _validate_controls(arguments)
        vp = _viewport()
        original = vp.camera
        original.isSmoothTransition = False
        _apply_controls(vp, arguments)
        return _success(_state(vp))
    except Exception as exc:
        if vp is not None and original is not None:
            try:
                vp.camera = original
                vp.refresh()
            except Exception as restore_error:
                return _fail(f"Error changing viewport: {exc}; restoring the camera also failed: {restore_error}")
        return _fail(f"Error changing viewport: {exc}")


def _dimension(value, name):
    if type(value) not in (int, float) or type(value) is bool:
        raise ValueError(f"{name} must be an integer from 0 to 8192")
    _number(value, name, 0, 8192)
    if value != int(value):
        raise ValueError(f"{name} must be an integer from 0 to 8192")
    return int(value)


def _screen_up(back, up):
    """Return up made perpendicular to the viewing direction, as a unit vector."""
    return _unit(_cross(_unit(_cross(back, up)), back))


def _parse_capture_camera(value):
    """Validate a capture camera and return its pose in cm."""
    _object(value, "camera", ("eye", "target", "up", "units", "extents", "projection"), ("eye", "target", "up"))
    units = value.get("units", "cm")
    if not isinstance(units, str) or units not in CAPTURE_UNITS:
        raise ValueError(f"camera.units must be one of: {', '.join(CAPTURE_UNITS)}")
    projection = value.get("projection")
    if projection is not None and (not isinstance(projection, str) or projection not in PROJECTIONS):
        raise ValueError("Unknown camera projection")
    scale = CAPTURE_UNITS[units]
    eye = _scale(_vector(value["eye"], "camera.eye"), scale)
    target = _scale(_vector(value["target"], "camera.target"), scale)
    extents = value.get("extents")
    if extents is not None:
        extents = _number(extents, "camera.extents", 1e-9, 1e12) * scale
    back = _unit(_sub(eye, target))
    up = _screen_up(back, _unit(_vector(value["up"], "camera.up")))
    return {"eye": eye, "target": target, "up": up, "extents": extents, "projection": projection}


def _model_view_axes(name, up_axis):
    """Return (unit vector from target to eye, up) for a model-relative view."""
    right, front, up = MODEL_AXES[up_axis]
    return {
        "front": (front, up), "back": (_scale(front, -1), up),
        "right": (right, up), "left": (_scale(right, -1), up),
        "top": (up, _scale(front, -1)), "bottom": (_scale(up, -1), front),
        "iso": (_unit(_add(_add(front, right), up)), up),
    }[name]


def _frame_model(low, high, name, up_axis, aspect):
    """Return an orthographic pose framing the box low..high (cm) at width/height aspect."""
    half = _scale(_sub(high, low), 0.5)
    radius = math.hypot(*half)
    if radius < 1e-9:
        raise ValueError("Visible bodies have no size to frame")
    back, up = _model_view_axes(name, up_axis)
    up = _screen_up(back, up)
    right = _cross(up, back)
    half_width, half_height = (sum(abs(a) * h for a, h in zip(axis, half)) for axis in (right, up))
    # viewExtents is the radius of a sphere that fits the window's smaller side.
    extents = MODEL_VIEW_MARGIN * max(half_width / max(aspect, 1), half_height * min(aspect, 1))
    target = _scale(_add(low, high), 0.5)
    # Keep the eye outside the bounding sphere so nothing sits behind it.
    return {"eye": _add(target, _scale(back, 3 * radius)), "target": target, "up": up,
            "extents": extents, "projection": "orthographic"}


def _visible_bounds(root):
    """Return (low, high, shown, hidden) over visible bodies; hidden ones are stale reference geometry."""
    low = high = None
    shown = hidden = 0
    for owner in [root, *root.allOccurrences]:
        owner_visible = owner is root or owner.isVisible
        for body in owner.bRepBodies:
            if not (owner_visible and body.isVisible):
                hidden += 1
                continue
            shown += 1
            box = body.boundingBox
            corners = (box.minPoint.x, box.minPoint.y, box.minPoint.z), (box.maxPoint.x, box.maxPoint.y, box.maxPoint.z)
            low = corners[0] if low is None else tuple(map(min, low, corners[0]))
            high = corners[1] if high is None else tuple(map(max, high, corners[1]))
    if low is None:
        raise ValueError(f"model_view needs a visible body; found none ({hidden} hidden)")
    return low, high, shown, hidden


def _capture_aim(arguments, view, fit):
    """Validate camera/model_view, both temporary like view/fit; None keeps the view."""
    camera, model_view = arguments.get("camera"), arguments.get("model_view")
    if "up_axis" in arguments and model_view is None:
        raise ValueError("up_axis requires model_view")
    if camera is None and model_view is None:
        return None
    if sum(item is not None for item in (camera, model_view, view)) > 1:
        raise ValueError("camera, model_view and view are mutually exclusive")
    if fit:
        raise ValueError("fit cannot be combined with camera or model_view; model_view already frames the visible bodies")
    if camera is not None:
        return {"pose": _parse_capture_camera(camera)}
    up_axis = arguments.get("up_axis", "z")
    if not isinstance(model_view, str) or model_view not in MODEL_VIEWS:
        raise ValueError(f"model_view must be one of: {', '.join(MODEL_VIEWS)}")
    if not isinstance(up_axis, str) or up_axis not in MODEL_AXES:
        raise ValueError("up_axis must be z or y")
    return {"model_view": model_view, "up_axis": up_axis}


def _aim_camera(vp, pose):
    camera = vp.camera
    camera.isSmoothTransition = False
    camera.isFitView = False
    if pose["projection"] is not None:
        camera.cameraType = getattr(adsk.core.CameraTypes, PROJECTIONS[pose["projection"]])
    if pose["extents"] is not None:
        camera.viewExtents = pose["extents"]
    camera.eye = adsk.core.Point3D.create(*pose["eye"])
    camera.target = adsk.core.Point3D.create(*pose["target"])
    camera.upVector = adsk.core.Vector3D.create(*pose["up"])
    vp.camera = camera


def _apply_capture_aim(vp, aim, aspect):
    """Aim the camera and return report fields, including the camera Fusion settled on."""
    notes = {}
    pose = aim.get("pose")
    if pose is None:
        root = getattr(adsk.core.Application.get().activeProduct, "rootComponent", None)
        if root is None:
            raise ValueError("model_view needs an active design")
        low, high, shown, hidden = _visible_bounds(root)
        pose = _frame_model(low, high, aim["model_view"], aim["up_axis"], aspect)
        notes["model_view"] = {"view": aim["model_view"], "up_axis": aim["up_axis"],
                               "visible_bodies": shown, "hidden_bodies_skipped": hidden}
    _aim_camera(vp, pose)
    notes["camera"] = _state(vp)["camera"]
    return notes


def _blank_warning(png, aimed):
    """Return a warning when the capture looks blank; this check never fails a capture."""
    try:
        coverage = png_image.blank_coverage(png)
    except Exception as exc:
        return f"blank-image check skipped: {exc}"
    if coverage is None:
        return None
    cause = "the camera may be aimed away from the model" if aimed else "the view may not include the model"
    return (f"image looks blank: only {coverage:.2%} of pixels differ from the background; "
            f"{cause}, or the active document just changed. Try fit: true.")


def capture(arguments):
    """Capture PNG; optional view/fit are temporary and always restored."""
    path = None
    vp = original = None
    result = None
    try:
        width = _dimension(arguments.get("width", 800), "width")
        height = _dimension(arguments.get("height", 600), "height")
        fit = _boolean(arguments.get("fit", False), "fit")
        anti_aliasing = _boolean(arguments.get("anti_aliasing", True), "anti_aliasing")
        view = arguments.get("view")
        if view is not None and view not in VIEW_NAMES:
            raise ValueError("Unknown standard view")
        aim = _capture_aim(arguments, view, fit)
        notes = {}
        background = arguments.get("background", "viewport")
        if not isinstance(background, str) or (background not in ("viewport", "transparent") and not re.fullmatch(r"#[0-9a-fA-F]{6}", background)):
            raise ValueError("background must be viewport, transparent, or #RRGGBB")
        rgb = tuple(int(background[i:i + 2], 16) for i in (1, 3, 5)) if background.startswith("#") else None
        vp = _viewport()
        rendered_width, rendered_height = width or vp.width, height or vp.height
        if rendered_width * rendered_height > png_image.MAX_PIXELS:
            raise ValueError("Image exceeds 16 megapixels; reduce width/height")
        crop = arguments.get("crop")
        if crop is not None:
            _object(crop, "crop", ("x", "y", "width", "height"), ("x", "y", "width", "height"))
            crop = tuple(_dimension(crop[k], f"crop.{k}") for k in ("x", "y", "width", "height"))
            x, y, w, h = crop
            if min(w, h) < 1 or x + w > rendered_width or y + h > rendered_height:
                raise ValueError("Crop must fit inside the rendered image; origin is top-left")
        if view is not None or fit:
            original = vp.camera
            original.isSmoothTransition = False
            controls = {"fit": fit}
            if view is not None:
                controls["view"] = view
            _apply_controls(vp, controls)
        if aim is not None:
            original = vp.camera
            original.isSmoothTransition = False
            notes.update(_apply_capture_aim(vp, aim, rendered_width / max(rendered_height, 1)))
        vp.refresh()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            path = tmp.name
        if background != "viewport" or not anti_aliasing:
            options = adsk.core.SaveImageFileOptions.create(path)
            options.width, options.height = width, height
            options.isBackgroundTransparent = background != "viewport"
            options.isAntiAliased = anti_aliasing
            saved = vp.saveAsImageFileWithOptions(options)
        else:
            saved = vp.saveAsImageFile(path, width, height)
        if not saved:
            raise ValueError("Fusion failed to capture viewport image")
        with open(path, "rb") as fh:
            png = fh.read()
        # Check the full render: a crop of empty space is not a blank capture.
        blank = _blank_warning(png, aim is not None)
        if blank:
            notes["warnings"] = [blank]
        if crop is not None or rgb is not None:
            png = png_image.transform(png, crop=crop, background=rgb)
        result = {"content": [{"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"}], "isError": False}
        if notes:
            result["content"].append({"type": "text", "text": json.dumps(notes, indent=2)})
    except Exception as exc:
        result = _fail(f"Error capturing viewport: {exc}")
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        if original is not None:
            try:
                vp.camera = original
                vp.refresh()
            except Exception as exc:
                result = _fail(f"Capture finished but restoring the camera failed: {exc}")
    return result
