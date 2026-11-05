"""Camera geometry, reversible capture, validation, and PNG processing."""

import base64
import copy
import json
import math
import os
import struct
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401
from fusion_bridge import clean_view, viewport, tool_surface
from lib import png_image


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def make_png(width, height, raw, color=6, depth=8):
    return (png_image.SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def read_unfiltered(png):
    header = struct.unpack('>IIBBBBB', png[16:29])
    position, data = 8, bytearray()
    while position < len(png):
        size = struct.unpack_from('>I', png, position)[0]
        if png[position+4:position+8] == b'IDAT':
            data.extend(png[position+8:position+8+size])
        position += size + 12
    return header[:2], zlib.decompress(data)


class PNGTests(unittest.TestCase):
    def test_all_five_filters_decode_to_same_pixels(self):
        first = bytes([10, 20, 30, 40, 50, 60, 70, 80])
        second = bytes([20, 40, 60, 80, 100, 120, 140, 160])
        filtered = [second, bytes([20,40,60,80,80,80,80,80]),
                    bytes([10,20,30,40,50,60,70,80]),
                    bytes([15,30,45,60,65,70,75,80]),
                    bytes([10,20,30,40,50,60,70,80])]
        for kind, encoded in enumerate(filtered):
            with self.subTest(filter=kind):
                source = make_png(2, 2, b'\0' + first + bytes([kind]) + encoded)
                dims, decoded = read_unfiltered(png_image.transform(source))
                self.assertEqual(dims, (2, 2))
                self.assertEqual(decoded, b'\0' + first + b'\0' + second)
                dims, cropped = read_unfiltered(png_image.transform(source, (1, 1, 1, 1)))
                self.assertEqual((dims, cropped), ((1, 1), b'\0' + second[4:]))

    def test_transparency_composited_without_altering_opaque_pixels(self):
        source = make_png(3, 1, b'\0' + bytes([255,0,0,0, 200,100,0,128, 1,2,3,255]))
        _, raw = read_unfiltered(png_image.transform(source, background=(0, 0, 255)))
        self.assertEqual(raw, b'\0' + bytes([0,0,255,255, 100,50,127,255, 1,2,3,255]))

    def test_rgb_crop(self):
        source = make_png(2, 1, b'\0\x01\x02\x03\x04\x05\x06', color=2)
        self.assertEqual(read_unfiltered(png_image.transform(source, (1,0,1,1))), ((1,1), b'\0\x04\x05\x06'))

    def test_crop_preserves_color_metadata(self):
        source = make_png(2, 1, b'\0\x01\x02\x03\x04\x05\x06', color=2)
        metadata = chunk(b'sRGB', b'\0') + chunk(b'gAMA', struct.pack('>I', 45455))
        source = source[:33] + metadata + source[33:]
        result = png_image.transform(source, (1, 0, 1, 1))
        self.assertEqual(result[33:33 + len(metadata)], metadata)
        self.assertEqual(read_unfiltered(result), ((1, 1), b'\0\x04\x05\x06'))

    def test_rejects_corruption_oversize_and_bad_crop(self):
        source = make_png(1, 1, b'\0\x01\x02\x03\xff')
        for invalid in (source[:-2], b'garbage', source[:35]+b'broken'+source[41:],
                        make_png(100000,100000,b''), make_png(1,1,b'\0'*100),
                        make_png(1,1,b'\0'*9,depth=16)):
            with self.subTest(data=invalid[:30]):
                with self.assertRaises(ValueError):
                    png_image.transform(invalid)
        for crop in ((-1,0,1,1),(0,0,2,1),(0,0,0,1),(0.5,0,1,1)):
            with self.assertRaises(ValueError):
                png_image.transform(source,crop)


def solid_rows(width, height, pixel, dots=()):
    """Unfiltered raw rows of one color, with (x, y, pixel) overrides."""
    rows = [bytearray(pixel * width) for _ in range(height)]
    for x, y, value in dots:
        rows[y][x * len(pixel):(x + 1) * len(pixel)] = value
    return b''.join(b'\0' + bytes(row) for row in rows)


class BlankDetectionTests(unittest.TestCase):
    def test_uniform_image_is_blank(self):
        self.assertEqual(png_image.blank_coverage(make_png(400, 300, solid_rows(400, 300, b'\xc8\xc8\xc8\xff'))), 0)

    def test_marker_dot_on_background_is_blank(self):
        dot = [(x, y, b'\0\0\xff') for x in range(200, 206) for y in range(150, 156)]
        coverage = png_image.blank_coverage(make_png(400, 300, solid_rows(400, 300, b'\xc8\xc8\xc8', dot), color=2))
        self.assertIsNotNone(coverage)
        self.assertLessEqual(coverage, png_image.BLANK_MAX_COVERAGE)

    def test_vertical_gradient_background_is_blank(self):
        raw = b''.join(b'\0' + bytes([100 + y // 3, 120 + y // 3, 140, 255]) * 400 for y in range(300))
        self.assertEqual(png_image.blank_coverage(make_png(400, 300, raw)), 0)

    def test_transparent_capture_ignores_hidden_rgb(self):
        row = b'\0' + bytes(channel for x in range(400) for channel in ((x % 2) * 200, 0, 0, 0))
        self.assertEqual(png_image.blank_coverage(make_png(400, 300, row * 300)), 0)

    def test_model_on_background_is_not_blank(self):
        block = [(x, y, b'\x30\x60\x90') for x in range(100, 300) for y in range(100, 200)]
        self.assertIsNone(png_image.blank_coverage(make_png(400, 300, solid_rows(400, 300, b'\xc8\xc8\xc8', block), color=2)))

    def test_row_uniform_band_filling_the_width_is_not_blank(self):
        band = [(x, y, b'\x30\x60\x90') for x in range(400) for y in range(100, 200)]
        self.assertIsNone(png_image.blank_coverage(make_png(400, 300, solid_rows(400, 300, b'\xc8\xc8\xc8', band), color=2)))

    def test_detailed_image_skips_the_decode(self):
        raw = b''.join(b'\0' + os.urandom(1200) for _ in range(300))
        with patch.object(png_image, '_decode') as decode:
            self.assertIsNone(png_image.blank_coverage(make_png(400, 300, raw, color=2)))
        decode.assert_not_called()

    def test_tiny_images_are_never_called_blank(self):
        self.assertIsNone(png_image.blank_coverage(make_png(2, 1, b'\0' + bytes(8))))

    def test_transform_output_is_unchanged_by_the_decode_split(self):
        source = make_png(2, 1, b'\0\x01\x02\x03\x04\x05\x06', color=2)
        self.assertEqual(read_unfiltered(png_image.transform(source)), ((2, 1), b'\0\x01\x02\x03\x04\x05\x06'))

    def test_invalid_data_raises_for_the_caller_to_report(self):
        for invalid in (b'garbage', png_image.SIGNATURE):
            with self.assertRaises(ValueError):
                png_image.blank_coverage(invalid)


class Vec:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = x, y, z


class Camera:
    def __init__(self):
        self.eye, self.target, self.upVector = Vec(0,0,10), Vec(), Vec(0,1,0)
        self.cameraType = 0
        self.perspectiveAngle = math.pi / 4
        self.isSmoothTransition = False
        self.isFitView = False
        self.extents = (20,10)

    def getExtents(self):
        return True, *self.extents

    def setExtents(self, width, height):
        self.extents = (width, height)
        return True


class Viewport:
    def __init__(self):
        self._camera = Camera()
        self.width, self.height = 800, 600
        self.saved = []
        self.fail_save = False
        self.fail_refresh_once = False
        self.png = make_png(2, 1, b'\0' + bytes([255,0,0,255, 0,0,0,0]))
        self.captured_cameras = []

    @property
    def camera(self):
        return copy.deepcopy(self._camera)

    @camera.setter
    def camera(self, value):
        self._camera = copy.deepcopy(value)
        if value.isFitView:
            self._camera.extents = (10,5)
            self._camera.isFitView = False
        if hasattr(value, 'viewOrientation'):
            self._camera.eye = Vec(10,0,0)
            del self._camera.viewOrientation

    def refresh(self):
        if self.fail_refresh_once:
            self.fail_refresh_once = False
            raise RuntimeError('refresh failed')
        return True

    def saveAsImageFile(self, path, width, height):
        self.saved.append((path, width, height))
        self.captured_cameras.append(self.camera)
        if self.fail_save == 'exception':
            raise RuntimeError('export failed')
        if self.fail_save:
            return False
        with open(path,'wb') as handle:
            handle.write(self.png)
        return True

    def saveAsImageFileWithOptions(self, options):
        self.options = options
        return self.saveAsImageFile(options.filename, options.width, options.height)


class Box:
    def __init__(self, low, high):
        self.minPoint, self.maxPoint = Vec(*low), Vec(*high)


def body(low, high, visible=True):
    return SimpleNamespace(isVisible=visible, boundingBox=Box(low, high))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class CaptureCameraMathTests(unittest.TestCase):
    def assertVector(self, actual, expected):
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e)

    def test_z_up_views_match_the_viewcube_of_a_z_up_model(self):
        expected = {'front': ((0,-1,0),(0,0,1)), 'back': ((0,1,0),(0,0,1)),
                    'right': ((1,0,0),(0,0,1)), 'left': ((-1,0,0),(0,0,1)),
                    'top': ((0,0,1),(0,1,0)), 'bottom': ((0,0,-1),(0,-1,0))}
        for name, (back, up) in expected.items():
            with self.subTest(view=name):
                axes = viewport._model_view_axes(name, 'z')
                self.assertVector(axes[0], back)
                self.assertVector(axes[1], up)

    def test_y_up_views_match_fusion_standard_views(self):
        expected = {'front': ((0,0,1),(0,1,0)), 'right': ((1,0,0),(0,1,0)), 'top': ((0,1,0),(0,0,-1)), 'bottom': ((0,-1,0),(0,0,1))}
        for name, (back, up) in expected.items():
            with self.subTest(view=name):
                axes = viewport._model_view_axes(name, 'y')
                self.assertVector(axes[0], back)
                self.assertVector(axes[1], up)

    def test_model_x_runs_screen_right_in_front_top_and_iso_views(self):
        for up_axis in viewport.MODEL_AXES:
            for name in ('front', 'top', 'bottom', 'iso'):
                with self.subTest(up_axis=up_axis, view=name):
                    pose = viewport._frame_model((0,0,0), (1,1,1), name, up_axis, 1)
                    back = viewport._unit(viewport._sub(pose['eye'], pose['target']))
                    self.assertGreater(viewport._cross(pose['up'], back)[0], 0)

    def test_frame_centers_on_the_box_and_clears_it(self):
        pose = viewport._frame_model((-10,0,0), (30,10,20), 'front', 'z', 1)
        self.assertVector(pose['target'], (10,5,10))
        self.assertEqual(pose['projection'], 'orthographic')
        radius = math.hypot(20,5,10)
        self.assertGreater(math.dist(pose['eye'], pose['target']), radius)
        self.assertVector(pose['eye'][::2], (10,10))

    def test_extents_follow_what_the_view_sees_and_the_image_shape(self):
        low, high = (0,0,0), (40,10,20)  # half sizes 20, 5, 10
        margin = viewport.MODEL_VIEW_MARGIN
        square = viewport._frame_model(low, high, 'front', 'z', 1)['extents']
        self.assertAlmostEqual(square, 40 * margin)
        # Wide images fit the 40-wide model in a shorter window; height then limits.
        self.assertAlmostEqual(viewport._frame_model(low, high, 'front', 'z', 4)['extents'], 20 * margin)
        # Portrait images: the smaller side is the width.
        self.assertAlmostEqual(viewport._frame_model(low, high, 'front', 'z', .5)['extents'], 40 * margin)
        # Looking down X sees 10 wide, 20 tall.
        self.assertAlmostEqual(viewport._frame_model(low, high, 'right', 'z', 1)['extents'], 20 * margin)

    def test_iso_looks_from_front_right_top_with_perpendicular_up(self):
        pose = viewport._frame_model((-1,-1,-1), (1,1,1), 'iso', 'z', 1)
        self.assertGreater(pose['eye'][0], 0)
        self.assertLess(pose['eye'][1], 0)
        self.assertGreater(pose['eye'][2], 0)
        back = viewport._unit(pose['eye'])
        self.assertAlmostEqual(dot(pose['up'], back), 0)
        self.assertGreater(pose['up'][2], 0)

    def test_degenerate_box_refused(self):
        with self.assertRaises(ValueError):
            viewport._frame_model((1,1,1), (1,1,1), 'top', 'z', 1)

    def test_parse_converts_lengths_but_not_the_up_direction(self):
        for units, factor in (('in',2.54),('mm',.1),('cm',1)):
            with self.subTest(units=units):
                pose = viewport._parse_capture_camera({'eye':{'x':10,'y':0,'z':0},'target':{'x':0,'y':0,'z':0},
                                                       'up':{'x':0,'y':0,'z':5},'units':units,'extents':2})
                self.assertVector(pose['eye'], (10*factor,0,0))
                self.assertVector(pose['up'], (0,0,1))
                self.assertAlmostEqual(pose['extents'], 2*factor)
                self.assertIsNone(pose['projection'])

    def test_parse_defaults_to_cm_and_makes_up_perpendicular(self):
        pose = viewport._parse_capture_camera({'eye':{'x':0,'y':-1,'z':1},'target':{'x':0,'y':0,'z':0},'up':{'x':0,'y':0,'z':1}})
        self.assertVector(pose['eye'], (0,-1,1))
        self.assertIsNone(pose['extents'])
        self.assertAlmostEqual(dot(pose['up'], (0,-1,1)), 0)

    def test_parse_rejects_bad_cameras(self):
        point = {'x':0,'y':0,'z':0}
        good = {'eye':{'x':1,'y':0,'z':0},'target':point,'up':{'x':0,'y':0,'z':1}}
        bad = [None, 'top', {}, {'eye':good['eye']}, {**good,'extra':1}, {**good,'eye':point},
               {**good,'up':point}, {**good,'up':{'x':2,'y':0,'z':0}}, {**good,'up':{'x':0,'y':0,'z':float('nan')}},
               {**good,'eye':{'x':'1','y':0,'z':0}}, {**good,'units':'ft'}, {**good,'units':['in']},
               {**good,'extents':0}, {**good,'extents':-1}, {**good,'extents':True},
               {**good,'projection':'fisheye'}, {**good,'projection':['orthographic']}]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    viewport._parse_capture_camera(value)

    def test_bounds_cover_visible_root_and_occurrence_bodies_only(self):
        root = SimpleNamespace(
            bRepBodies=[body((0,0,0),(1,1,1)), body((-500,0,0),(0,1,1),visible=False)],
            allOccurrences=[SimpleNamespace(isVisible=True, bRepBodies=[body((5,-2,0),(6,0,9))]),
                            SimpleNamespace(isVisible=False, bRepBodies=[body((0,0,0),(900,1,1))])])
        self.assertEqual(viewport._visible_bounds(root), ((0,-2,0),(6,1,9),2,2))

    def test_bounds_refuse_a_design_with_nothing_visible(self):
        root = SimpleNamespace(bRepBodies=[body((0,0,0),(1,1,1),visible=False)], allOccurrences=[])
        with self.assertRaisesRegex(ValueError, '1 hidden'):
            viewport._visible_bounds(root)


class ViewportTests(unittest.TestCase):
    def setUp(self):
        self.vp = Viewport()
        self.app = SimpleNamespace(activeViewport=self.vp)
        values = {
            'CameraTypes': SimpleNamespace(OrthographicCameraType=0, PerspectiveCameraType=1, PerspectiveWithOrthoFacesCameraType=2),
            'ViewOrientations': SimpleNamespace(**{name:i for i,name in enumerate(viewport.VIEW_NAMES.values())}),
            'Point3D': SimpleNamespace(create=Vec), 'Vector3D': SimpleNamespace(create=Vec),
            'SaveImageFileOptions': SimpleNamespace(create=lambda path: SimpleNamespace(filename=path)),
        }
        for name,value in values.items():
            patcher=patch.object(viewport.adsk.core,name,value,create=True)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher=patch.object(viewport.adsk.core.Application,'get',return_value=self.app)
        patcher.start(); self.addCleanup(patcher.stop)

    def result(self, response):
        self.assertFalse(response.get('isError'),response)
        return json.loads(response['content'][0]['text'])

    def test_snapshot_round_trip_and_orthographic_zoom(self):
        before=self.result(viewport.get_viewport({}))
        self.assertEqual(before['units'],{'length':'cm','angle':'degrees'})
        after=self.result(viewport.set_viewport({'zoom':2}))
        self.assertEqual(after['camera']['extents'],{'width':10,'height':5})
        restored=self.result(viewport.set_viewport({'camera':before['camera']}))
        self.assertEqual(before,restored)

    def test_perspective_zoom_changes_distance_not_angle(self):
        self.vp._camera.cameraType=1
        after=self.result(viewport.set_viewport({'zoom':2}))['camera']
        self.assertEqual(after['eye'],{'x':0,'y':0,'z':5})
        self.assertEqual(after['perspective_angle'],45)
        self.assertNotIn('extents',after)
        self.assertEqual(self.result(viewport.set_viewport({'camera':after}))['camera'],after)

    def test_orbit_preserves_target_distance_and_orthogonal_up(self):
        after=self.result(viewport.set_viewport({'orbit':{'yaw':90,'pitch':30,'roll':45}}))['camera']
        eye=tuple(after['eye'].values()); up=tuple(after['up_vector'].values())
        self.assertAlmostEqual(math.hypot(*eye),10)
        self.assertAlmostEqual(math.hypot(*up),1)
        self.assertAlmostEqual(sum(x*y for x,y in zip(eye,up)),0)
        self.assertAlmostEqual(eye[0],10*math.cos(math.pi/6))
        self.assertAlmostEqual(eye[1],-5)
        self.assertEqual(after['target'],{'x':0,'y':0,'z':0})

    def test_pan_and_fit_then_zoom(self):
        after=self.result(viewport.set_viewport({'fit':True,'pan':{'x':3,'y':4},'zoom':2}))['camera']
        self.assertEqual(after['target'],{'x':3,'y':4,'z':0})
        self.assertEqual(after['eye'],{'x':3,'y':4,'z':10})
        self.assertEqual(after['extents'],{'width':5,'height':2.5})

    def test_invalid_controls_never_change_camera(self):
        before=self.result(viewport.get_viewport({}))
        invalid=[{'zoom':0},{'zoom':True},{'orbit':{'yaw':float('nan')}}, {'pan':{'x':float('inf')}},
                 {'view':'invalid'},{'projection':'invalid'},{'fit':'yes'},
                 {'camera':before['camera'],'zoom':2}]
        pose=copy.deepcopy(before['camera']);pose['eye']=pose['target'];invalid.append({'camera':pose})
        pose=copy.deepcopy(before['camera']);pose['up_vector']={'x':0,'y':0,'z':1};invalid.append({'camera':pose})
        for args in invalid:
            with self.subTest(args=args):
                self.assertTrue(viewport.set_viewport(args)['isError'])
                self.assertEqual(self.result(viewport.get_viewport({})),before)

    def test_camera_restored_after_failed_change(self):
        before=self.result(viewport.get_viewport({}))
        self.vp.fail_refresh_once=True
        self.assertTrue(viewport.set_viewport({'zoom':2})['isError'])
        self.assertEqual(self.result(viewport.get_viewport({})),before)

    def test_original_capture_defaults_and_native_size_supported(self):
        for args,dims in (({},(800,600)),({'width':0,'height':0},(0,0))):
            result=viewport.capture(args)
            self.assertFalse(result['isError'],result)
            self.assertEqual(base64.b64decode(result['content'][0]['data']),self.vp.png)
            path,width,height=self.vp.saved[-1]
            self.assertEqual((width,height),dims)
            self.assertFalse(os.path.exists(path))

    def test_capture_restores_camera_and_cleans_file_on_all_export_outcomes(self):
        for fail in (False,True,'exception'):
            with self.subTest(failure=fail):
                before=self.result(viewport.get_viewport({}))
                self.vp.fail_save=fail
                result=viewport.capture({'view':'top','fit':True})
                self.assertEqual(result['isError'],bool(fail))
                self.assertEqual(self.result(viewport.get_viewport({})),before)
                self.assertFalse(os.path.exists(self.vp.saved[-1][0]))

    def test_background_crop_and_antialias_options(self):
        result=viewport.capture({'width':2,'height':1,'background':'#123456','anti_aliasing':False,'crop':{'x':1,'y':0,'width':1,'height':1}})
        self.assertFalse(result['isError'],result)
        self.assertTrue(self.vp.options.isBackgroundTransparent)
        self.assertFalse(self.vp.options.isAntiAliased)
        png=base64.b64decode(result['content'][0]['data'])
        self.assertEqual(read_unfiltered(png),((1,1),b'\0\x12\x34\x56\xff'))
        result=viewport.capture({'background':'transparent'})
        self.assertEqual(base64.b64decode(result['content'][0]['data']),self.vp.png)

    def test_invalid_capture_arguments_do_not_render(self):
        for args in ({'width':-1},{'height':1.5},{'width':True},{'width':8192,'height':8192},
                     {'background':'red'},{'anti_aliasing':'no'},{'crop':{'x':0,'y':0,'width':900,'height':600}}):
            with self.subTest(args=args):
                self.assertTrue(viewport.capture(args)['isError'])
        self.assertEqual(self.vp.saved,[])

    def set_design(self, *bodies):
        self.app.activeProduct=SimpleNamespace(rootComponent=SimpleNamespace(bRepBodies=list(bodies),allOccurrences=[]))

    def notes(self, result):
        self.assertFalse(result['isError'],result)
        self.assertEqual(result['content'][0]['type'],'image')
        return json.loads(result['content'][1]['text'])

    def test_camera_applies_in_cm_for_the_capture_then_restores(self):
        before=self.result(viewport.get_viewport({}))
        result=viewport.capture({'camera':{'eye':{'x':10,'y':0,'z':0},'target':{'x':0,'y':0,'z':0},'up':{'x':0,'y':0,'z':1},
                                           'units':'in','extents':5,'projection':'perspective'}})
        camera=self.vp.captured_cameras[-1]
        self.assertAlmostEqual(camera.eye.x,25.4)
        self.assertEqual((camera.upVector.x,camera.upVector.y,camera.upVector.z),(0,0,1))
        self.assertAlmostEqual(camera.viewExtents,12.7)
        self.assertEqual(camera.cameraType,1)
        self.assertFalse(camera.isFitView)
        self.assertAlmostEqual(self.notes(result)['camera']['eye']['x'],25.4)
        self.assertEqual(self.result(viewport.get_viewport({})),before)

    def test_omitted_extents_and_projection_keep_the_current_camera(self):
        viewport.capture({'camera':{'eye':{'x':1,'y':0,'z':0},'target':{'x':0,'y':0,'z':0},'up':{'x':0,'y':0,'z':1}}})
        camera=self.vp.captured_cameras[-1]
        self.assertFalse(hasattr(camera,'viewExtents'))
        self.assertEqual(camera.cameraType,0)

    def test_camera_restored_on_every_failure(self):
        self.set_design(body((0,0,0),(10,10,10)))
        pose={'eye':{'x':1,'y':0,'z':0},'target':{'x':0,'y':0,'z':0},'up':{'x':0,'y':0,'z':1},'projection':'perspective'}
        for args in ({'camera':pose},{'model_view':'top'}):
            for fail in (True,'exception'):
                with self.subTest(args=args,failure=fail):
                    before=self.result(viewport.get_viewport({}))
                    self.vp.fail_save=fail
                    self.assertTrue(viewport.capture(args)['isError'])
                    self.assertEqual(self.result(viewport.get_viewport({})),before)

    def test_model_view_frames_visible_bodies_and_reports(self):
        self.set_design(body((0,0,0),(20,10,4)),body((-900,0,0),(0,1,1),visible=False))
        before=self.result(viewport.get_viewport({}))
        notes=self.notes(viewport.capture({'model_view':'front','width':400,'height':400}))
        camera=self.vp.captured_cameras[-1]
        self.assertEqual((camera.target.x,camera.target.y,camera.target.z),(10,5,2))
        self.assertLess(camera.eye.y,0)
        self.assertEqual(camera.upVector.z,1)
        self.assertAlmostEqual(camera.viewExtents,20*viewport.MODEL_VIEW_MARGIN)
        self.assertEqual(camera.cameraType,0)
        self.assertEqual(notes['model_view'],{'view':'front','up_axis':'z','visible_bodies':1,'hidden_bodies_skipped':1})
        self.assertEqual(self.result(viewport.get_viewport({})),before)

    def test_model_view_up_axis_y_and_native_size(self):
        self.set_design(body((0,0,0),(1,1,1)))
        result=viewport.capture({'model_view':'front','up_axis':'y','width':0,'height':0})
        self.assertEqual(self.notes(result)['model_view']['up_axis'],'y')
        self.assertGreater(self.vp.captured_cameras[-1].eye.z,1)

    def test_model_view_without_a_usable_design_fails_and_keeps_the_camera(self):
        before=self.result(viewport.get_viewport({}))
        self.app.activeProduct=SimpleNamespace()
        self.assertIn('active design',viewport.capture({'model_view':'top'})['content'][0]['text'])
        self.set_design(body((0,0,0),(1,1,1),visible=False))
        self.assertIn('visible body',viewport.capture({'model_view':'top'})['content'][0]['text'])
        self.assertEqual(self.vp.saved,[])
        self.assertEqual(self.result(viewport.get_viewport({})),before)

    def test_camera_argument_conflicts_do_not_render(self):
        self.set_design(body((0,0,0),(1,1,1)))
        pose={'eye':{'x':1,'y':0,'z':0},'target':{'x':0,'y':0,'z':0},'up':{'x':0,'y':0,'z':1}}
        for args in ({'camera':pose,'view':'top'},{'camera':pose,'model_view':'top'},{'model_view':'top','view':'top'},
                     {'camera':pose,'fit':True},{'model_view':'top','fit':True},{'up_axis':'z'},{'view':'top','up_axis':'z'},
                     {'camera':pose,'up_axis':'z'},{'model_view':'isometric'},{'model_view':'top','up_axis':'x'},
                     {'model_view':['top']},{'model_view':'top','up_axis':['z']},{'camera':'top'}):
            with self.subTest(args=args):
                self.assertTrue(viewport.capture(args)['isError'])
        self.assertEqual(self.vp.saved,[])

    def test_blank_capture_warns_without_failing(self):
        self.vp.png=make_png(400,300,solid_rows(400,300,b'\xc8\xc8\xc8\xff'))
        warning=self.notes(viewport.capture({}))['warnings'][0]
        self.assertTrue(warning.startswith('image looks blank:'),warning)
        self.assertIn('active document just changed',warning)
        self.assertIn('fit',warning)
        pose={'eye':{'x':1,'y':0,'z':0},'target':{'x':0,'y':0,'z':0},'up':{'x':0,'y':0,'z':1}}
        notes=self.notes(viewport.capture({'camera':pose}))
        self.assertIn('aimed away',notes['warnings'][0])
        self.assertIn('camera',notes)

    def test_blank_check_reads_the_full_render_not_the_crop(self):
        block=[(x,y,b'\x30\x60\x90\xff') for x in range(100,300) for y in range(100,200)]
        self.vp.png=make_png(400,300,solid_rows(400,300,b'\xc8\xc8\xc8\xff',block))
        result=viewport.capture({'width':400,'height':300,'crop':{'x':0,'y':0,'width':50,'height':50}})
        self.assertEqual([item['type'] for item in result['content']],['image'])

    def test_normal_capture_has_no_extra_content(self):
        self.assertEqual([item['type'] for item in viewport.capture({})['content']],['image'])

    def test_failed_blank_check_is_a_warning_not_an_error(self):
        with patch.object(png_image,'blank_coverage',side_effect=ValueError('odd PNG')):
            notes=self.notes(viewport.capture({}))
        self.assertEqual(notes['warnings'],['blank-image check skipped: odd PNG'])

    def test_absent_viewport_returns_tool_errors(self):
        self.app.activeViewport=None
        for function in (viewport.get_viewport,viewport.set_viewport,viewport.capture):
            self.assertTrue(function({})['isError'])

    def test_schema_and_implementation_have_same_views_and_projections(self):
        self.assertEqual(set(tool_surface.STANDARD_VIEWS),set(viewport.VIEW_NAMES))
        self.assertEqual(set(tool_surface.PROJECTIONS),set(viewport.PROJECTIONS))

    def test_schema_matches_capture_camera_options(self):
        schema=next(t for t in tool_surface.TOOL_DEFINITIONS if t['name']==tool_surface.CAPTURE_VIEWPORT)['inputSchema']['properties']
        self.assertEqual(set(schema['model_view']['enum']),set(viewport.MODEL_VIEWS))
        self.assertEqual(set(schema['up_axis']['enum']),set(viewport.MODEL_AXES))
        camera=schema['camera']
        self.assertEqual(set(camera['properties']['units']['enum']),set(viewport.CAPTURE_UNITS))
        self.assertEqual(set(camera['required']),{'eye','target','up'})
        self.assertFalse(camera['additionalProperties'])


class Flags:
    """Fake Fusion object holding boolean visibility flags; optional failing attributes."""
    def __init__(self, name='', fail_set=(), fail_restore=(), **flags):
        object.__setattr__(self, '_flags', dict(flags))
        object.__setattr__(self, '_fail_set', set(fail_set))
        object.__setattr__(self, '_fail_restore', set(fail_restore))
        object.__setattr__(self, 'name', name)
        object.__setattr__(self, 'writes', [])

    def __getattr__(self, attribute):
        flags = object.__getattribute__(self, '_flags')
        if attribute in flags:
            return flags[attribute]
        raise AttributeError(attribute)

    def __setattr__(self, attribute, value):
        if attribute in self._fail_set and value is False:
            raise RuntimeError('referenced component is read-only')
        if attribute in self._fail_restore and value is True:
            raise RuntimeError('restore refused')
        self.writes.append((attribute, value))
        self._flags[attribute] = value


def visibility(**overrides):
    flags = {a: True for attrs in clean_view.DESIGN_FLAGS.values() for a in attrs}
    flags.update(overrides)
    return Flags(**flags)


def component(name, **overrides):
    flags = {a: True for attrs in clean_view.FOLDER_FLAGS.values() for a in attrs}
    flags.update(overrides)
    return Flags(name, **flags)


class FakeDesign:
    def __init__(self, visibility, root, components=(), linked=()):
        self.objectVisibility = visibility
        self.rootComponent = root
        self.allComponents = [root, *components]
        object.__setattr__(root, 'allOccurrences', [SimpleNamespace(component=c) for c in (*components, *components, *linked)])


def snapshot(*objects):
    return [dict(o._flags) for o in objects]


class CleanViewPlanTests(unittest.TestCase):
    def setUp(self):
        self.vis = visibility(isOriginPointsVisible=False)
        self.root, self.sub, self.linked = component('Root'), component('Sub', isDecalFolderLightBulbOn=False), component('Linked')
        self.design = FakeDesign(self.vis, self.root, [self.sub], [self.linked])

    def test_plan_uses_object_visibility_and_component_folders_without_bodies(self):
        toggles = clean_view.plan(self.design)
        design_attrs = {a for _, _, t, a in toggles if t is self.vis}
        self.assertEqual(design_attrs, {a for attrs in clean_view.DESIGN_FLAGS.values() for a in attrs})
        folder = [(t.name, a) for _, _, t, a in toggles if t is not self.vis]
        # Components deduplicated; linked component reached via occurrences; only canvases/decals.
        self.assertEqual(sorted(folder), sorted((n, a) for n in ('Root', 'Sub', 'Linked')
                                                for a in ('isCanvasFolderLightBulbOn', 'isDecalFolderLightBulbOn')))
        self.assertFalse(any('Bodies' in a for *_, a in toggles))

    def test_folder_fallback_without_object_visibility(self):
        self.design.objectVisibility = None
        attrs = {a for _, _, t, a in clean_view.plan(self.design, ('canvases',)) if t is self.root}
        self.assertEqual(attrs, {a for c, attrs in clean_view.FOLDER_FLAGS.items() if c != 'canvases' for a in attrs})

    def test_keep_and_validation(self):
        categories = {c for c, *_ in clean_view.plan(self.design, ('sketches', 'joints'))}
        self.assertEqual(categories, {'construction', 'origin', 'canvases', 'decals'})
        self.assertEqual(clean_view.validate_keep(['joints', 'sketches']), ('sketches', 'joints'))
        for bad in ('sketches', ['bodies'], [1], None):
            with self.subTest(keep=bad):
                with self.assertRaises(ValueError):
                    clean_view.validate_keep(bad)
        with self.assertRaises(ValueError):
            clean_view.plan(None)

    def test_hide_then_restore_returns_exact_prior_state(self):
        before = snapshot(self.vis, self.root, self.sub, self.linked)
        cleaner = clean_view.CleanView(clean_view.plan(self.design))
        cleaner.hide()
        self.assertTrue(all(v is False for v in self.vis._flags.values()))
        self.assertTrue(self.root.isSketchFolderLightBulbOn)  # Object Visibility covers sketches; folders untouched.
        self.assertFalse(self.vis.isSketchesVisible)
        self.assertFalse(self.linked.isCanvasFolderLightBulbOn)
        # Already-hidden flags are never written.
        self.assertNotIn(('isOriginPointsVisible', False), self.vis.writes)
        self.assertNotIn(('isDecalFolderLightBulbOn', False), self.sub.writes)
        self.assertEqual(cleaner.restore(), [])
        self.assertEqual(snapshot(self.vis, self.root, self.sub, self.linked), before)
        report = cleaner.report()
        self.assertEqual(report['hidden'], list(clean_view.CATEGORIES))
        self.assertTrue(report['restored'])
        self.assertNotIn('warnings', report)

    def test_unwritable_flag_is_skipped_with_warning_and_failed_restore_is_named(self):
        locked = Flags('Locked', fail_set={'isCanvasFolderLightBulbOn'}, fail_restore={'isDecalFolderLightBulbOn'},
                       isCanvasFolderLightBulbOn=True, isDecalFolderLightBulbOn=True)
        design = FakeDesign(visibility(), component('Root'), linked=[locked])
        cleaner = clean_view.CleanView(clean_view.plan(design, ('sketches',)), ('sketches',))
        cleaner.hide()
        self.assertTrue(locked.isCanvasFolderLightBulbOn)
        self.assertEqual(cleaner.restore(), ["component 'Locked'.isDecalFolderLightBulbOn: restore refused"])
        report = cleaner.report()
        self.assertFalse(report['restored'])
        self.assertEqual(report['kept'], ['sketches'])
        self.assertIn("could not hide component 'Locked'.isCanvasFolderLightBulbOn", report['warnings'][0])
        self.assertIn('RESTORE FAILED', report['warnings'][-1])
        self.assertIn('isDecalFolderLightBulbOn', report['warnings'][-1])


class CleanViewCaptureTests(ViewportTests):
    def setUp(self):
        super().setUp()
        self.vis, self.root, self.sub = visibility(), component('Root'), component('Sub', isCanvasFolderLightBulbOn=False)
        self.app.activeProduct = FakeDesign(self.vis, self.root, [self.sub])
        self.seen = []
        save = self.vp.saveAsImageFile

        def spy(path, width, height):
            self.seen.append(snapshot(self.vis, self.root, self.sub))
            return save(path, width, height)
        self.vp.saveAsImageFile = spy

    def test_clean_capture_hides_during_save_and_restores_after_every_outcome(self):
        for fail in (False, True, 'exception'):
            with self.subTest(failure=fail):
                before = snapshot(self.vis, self.root, self.sub)
                camera = self.result(viewport.get_viewport({}))
                self.vp.fail_save = fail
                result = viewport.capture({'clean_view': True, 'clean_view_keep': ['joints'], 'fit': True})
                self.assertEqual(result['isError'], bool(fail))
                during = self.seen[-1]
                self.assertFalse(during[0]['isSketchesVisible'])
                self.assertTrue(during[0]['isJointsVisible'])
                self.assertFalse(during[1]['isCanvasFolderLightBulbOn'])
                self.assertEqual(snapshot(self.vis, self.root, self.sub), before)
                self.assertEqual(self.result(viewport.get_viewport({})), camera)
                report = json.loads(result['content'][-1]['text'])['clean_view']
                self.assertEqual(report['hidden'], ['sketches', 'construction', 'origin', 'canvases', 'decals'])
                self.assertEqual(report['kept'], ['joints'])
                self.assertTrue(report['restored'])
                if not fail:
                    self.assertEqual(result['content'][0]['type'], 'image')

    def test_plain_capture_leaves_flags_alone_and_adds_no_report(self):
        result = viewport.capture({})
        self.assertEqual(len(result['content']), 1)
        self.assertEqual(self.vis.writes + self.root.writes, [])

    def test_invalid_clean_view_arguments_touch_nothing(self):
        for args in ({'clean_view': 'yes'}, {'clean_view': True, 'clean_view_keep': ['bodies']},
                     {'clean_view_keep': ['sketches']}, {'clean_view': True, 'clean_view_keep': 'sketches'}):
            with self.subTest(args=args):
                self.assertTrue(viewport.capture(args)['isError'])
        self.assertEqual(self.vis.writes + self.root.writes, [])
        self.app.activeProduct = SimpleNamespace()  # e.g. a drawing, not a design
        self.assertIn('active Fusion design', viewport.capture({'clean_view': True})['content'][0]['text'])
        self.assertEqual(self.vp.saved, [])

    def test_failed_restore_keeps_image_and_names_flag(self):
        self.vis._fail_restore.add('isSketchesVisible')
        result = viewport.capture({'clean_view': True})
        self.assertFalse(result['isError'])
        report = json.loads(result['content'][-1]['text'])['clean_view']
        self.assertFalse(report['restored'])
        self.assertIn('objectVisibility.isSketchesVisible', report['warnings'][-1])
        self.assertTrue(self.vis.isUserWorkPlanesVisible)

    def test_schema_lists_clean_view_categories(self):
        props = next(t for t in tool_surface.TOOL_DEFINITIONS if t['name'] == 'capture_viewport')['inputSchema']['properties']
        self.assertEqual(props['clean_view']['type'], 'boolean')
        self.assertEqual(tuple(props['clean_view_keep']['items']['enum']), clean_view.CATEGORIES)


if __name__=='__main__':
    unittest.main()
