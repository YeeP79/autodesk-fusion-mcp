"""Crop/composite Fusion's non-interlaced 8-bit RGB/RGBA PNGs using stdlib.

This intentionally handles the PNG format Fusion exports, not arbitrary image
formats. Decode allocation is bounded and unsupported formats fail explicitly.
"""

import collections
import math
import struct
import zlib

MAX_PIXELS = 16_777_216
SIGNATURE = b"\x89PNG\r\n\x1a\n"

# A file larger than raw/25 holds too much detail to be blank, so it skips the
# pure-Python decode (which runs on Fusion's main thread).
BLANK_MIN_COMPRESSION = 25
# Channel difference (0-255) that counts as a different color, not dither.
BLANK_TOLERANCE = 8
# Share of pixels off the background at or below which an image reads as blank.
# A lone marker dot is about 0.05% of an 800x600 capture.
BLANK_MAX_COVERAGE = 0.002
BLANK_SAMPLES = 100_000


def _chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _decode(png, crop=None, background=None):
    """Return (color type, width, height, color chunks, unfiltered output rows)."""
    if not png.startswith(SIGNATURE):
        raise ValueError("Capture did not produce a PNG")
    position, header, compressed = 8, None, bytearray()
    color_chunks = bytearray()
    while position < len(png):
        if position + 12 > len(png):
            raise ValueError("Truncated PNG chunk")
        size = struct.unpack_from(">I", png, position)[0]
        kind = png[position + 4:position + 8]
        end = position + 8 + size
        if end + 4 > len(png):
            raise ValueError("Truncated PNG data")
        data = png[position + 8:end]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != struct.unpack_from(">I", png, end)[0]:
            raise ValueError("Invalid PNG checksum")
        if kind == b"IHDR":
            if header is not None or position != 8 or size != 13:
                raise ValueError("Invalid PNG header")
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
        elif kind in (b"sRGB", b"gAMA", b"cHRM", b"iCCP"):
            # Cropping does not change the pixel color space. Keep the export's
            # profile so viewers interpret the resulting colors consistently.
            color_chunks.extend(png[position:end + 4])
        elif kind in (b"PLTE", b"tRNS") or not kind[0] & 32:
            raise ValueError("Unsupported PNG chunk")
        position = end + 4
    else:
        raise ValueError("PNG has no end marker")
    if header is None:
        raise ValueError("PNG has no header")
    width, height, depth, color, compression, filtering, interlace = header
    if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
        raise ValueError("Expected Fusion's non-interlaced 8-bit RGB/RGBA PNG")
    if width < 1 or height < 1 or width * height > MAX_PIXELS:
        raise ValueError("PNG exceeds the image size limit")
    channels = 4 if color == 6 else 3
    stride = width * channels
    limit = (stride + 1) * height
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, limit + 1)
    if len(raw) != limit or not decoder.eof or decoder.unused_data:
        raise ValueError("Invalid PNG pixel data size")
    x, y, out_width, out_height = (0, 0, width, height) if crop is None else crop
    if (any(type(v) is not int for v in (x, y, out_width, out_height))
            or min(x, y) < 0 or min(out_width, out_height) < 1
            or x + out_width > width or y + out_height > height):
        raise ValueError("Crop must fit inside the rendered image")
    if background is not None and (
        len(background) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in background)
    ):
        raise ValueError("Background must contain three RGB bytes")
    output = bytearray()
    previous = bytearray(stride)
    for row_index in range(height):
        offset = row_index * (stride + 1)
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1:offset + 1 + stride])
        if filter_type > 4:
            raise ValueError("Unsupported PNG row filter")
        if filter_type:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                above = previous[i]
                upper_left = previous[i - channels] if i >= channels else 0
                prediction = (left if filter_type == 1 else above if filter_type == 2
                              else (left + above) // 2 if filter_type == 3
                              else _paeth(left, above, upper_left))
                row[i] = (row[i] + prediction) & 255
        previous = row
        if y <= row_index < y + out_height:
            cropped = row[x * channels:(x + out_width) * channels]
            if background is not None and channels == 4:
                for i in range(0, len(cropped), 4):
                    alpha = cropped[i + 3]
                    for component in range(3):
                        cropped[i + component] = (
                            cropped[i + component] * alpha + background[component] * (255 - alpha) + 127
                        ) // 255
                    cropped[i + 3] = 255
            output.append(0)  # Emit unfiltered rows; zlib still compresses them.
            output.extend(cropped)
    return color, out_width, out_height, color_chunks, output


def transform(png, crop=None, background=None):
    """Return a PNG cropped in rendered pixels, optionally over an RGB tuple."""
    color, out_width, out_height, color_chunks, output = _decode(png, crop, background)
    header = struct.pack(">IIBBBBB", out_width, out_height, 8, color, 0, 0, 0)
    return (SIGNATURE + _chunk(b"IHDR", header) + color_chunks
            + _chunk(b"IDAT", zlib.compress(output)) + _chunk(b"IEND", b""))


def _color_distance(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


def blank_coverage(png):
    """Return the share of pixels off the background if the image looks blank, else None.

    The background is each row's most common color, so Fusion's vertical
    gradient reads as blank; a jump between rows is an edge, so it does not.
    Fully transparent pixels are one color whatever RGB they carry.
    """
    if not png.startswith(SIGNATURE) or len(png) < 33:
        raise ValueError("Capture did not produce a PNG")
    width, height = struct.unpack_from(">II", png, 16)
    channels = 4 if png[25] == 6 else 3
    if len(png) * BLANK_MIN_COMPRESSION > width * height * channels:
        return None
    _, width, height, _, rows = _decode(png)
    stride = width * channels + 1
    step = math.isqrt(width * height // BLANK_SAMPLES) + 1
    sampled = off_background = 0
    previous = None
    for y in range(0, height, step):
        start = y * stride + 1
        counts = collections.Counter()
        for i in range(start, start + width * channels, channels * step):
            pixel = bytes(rows[i:i + channels])
            counts[bytes(4) if channels == 4 and not pixel[3] else pixel] += 1
        background = counts.most_common(1)[0][0]
        if previous is not None and _color_distance(background, previous) > BLANK_TOLERANCE:
            return None
        previous = background
        sampled += sum(counts.values())
        off_background += sum(n for pixel, n in counts.items()
                              if _color_distance(pixel, background) > BLANK_TOLERANCE)
    coverage = off_background / sampled
    return coverage if coverage <= BLANK_MAX_COVERAGE else None
