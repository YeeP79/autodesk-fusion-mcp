"""Explicit camera controls and PNG capture for the active Fusion viewport."""

import base64
import json
import math
import os
import re
import tempfile

import adsk.core

from ..lib import png_image
from . import clean_view as clean
from .common.results import error_result, ok_result

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
        return ok_result(_state(_viewport()))
    except Exception as exc:
        return error_result(f"Error reading viewport: {exc}")


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
        return ok_result(_state(vp))
    except Exception as exc:
        if vp is not None and original is not None:
            try:
                vp.camera = original
                vp.refresh()
            except Exception as restore_error:
                return error_result(f"Error changing viewport: {exc}; restoring the camera also failed: {restore_error}")
        return error_result(f"Error changing viewport: {exc}")


def _dimension(value, name):
    if type(value) not in (int, float) or type(value) is bool:
        raise ValueError(f"{name} must be an integer from 0 to 8192")
    _number(value, name, 0, 8192)
    if value != int(value):
        raise ValueError(f"{name} must be an integer from 0 to 8192")
    return int(value)


def capture(arguments):
    """Capture PNG; optional view/fit/clean_view are temporary and always restored."""
    path = None
    vp = original = None
    cleaner = None
    result = None
    try:
        width = _dimension(arguments.get("width", 800), "width")
        height = _dimension(arguments.get("height", 600), "height")
        fit = _boolean(arguments.get("fit", False), "fit")
        anti_aliasing = _boolean(arguments.get("anti_aliasing", True), "anti_aliasing")
        view = arguments.get("view")
        if view is not None and view not in VIEW_NAMES:
            raise ValueError("Unknown standard view")
        background = arguments.get("background", "viewport")
        if not isinstance(background, str) or (background not in ("viewport", "transparent") and not re.fullmatch(r"#[0-9a-fA-F]{6}", background)):
            raise ValueError("background must be viewport, transparent, or #RRGGBB")
        rgb = tuple(int(background[i:i + 2], 16) for i in (1, 3, 5)) if background.startswith("#") else None
        clean_view = _boolean(arguments.get("clean_view", False), "clean_view")
        keep = clean.validate_keep(arguments.get("clean_view_keep", []))
        if "clean_view_keep" in arguments and not clean_view:
            raise ValueError("clean_view_keep requires clean_view: true")
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
        if clean_view:
            # Hide before fit so fit frames the remaining geometry.
            cleaner = clean.CleanView(clean.plan(adsk.core.Application.get().activeProduct, keep), keep)
            cleaner.hide()
        if view is not None or fit:
            original = vp.camera
            original.isSmoothTransition = False
            controls = {"fit": fit}
            if view is not None:
                controls["view"] = view
            _apply_controls(vp, controls)
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
        if crop is not None or rgb is not None:
            png = png_image.transform(png, crop=crop, background=rgb)
        result = {"content": [{"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"}], "isError": False}
    except Exception as exc:
        result = error_result(f"Error capturing viewport: {exc}")
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        if cleaner is not None:
            cleaner.restore()
            if original is None:
                try:
                    vp.refresh()
                except Exception:
                    pass  # Display refresh only; the flags are already restored.
        if original is not None:
            try:
                vp.camera = original
                vp.refresh()
            except Exception as exc:
                result = error_result(f"Capture finished but restoring the camera failed: {exc}")
        if cleaner is not None:
            result["content"].append({"type": "text", "text": json.dumps({"clean_view": cleaner.report()}, indent=2)})
    return result
