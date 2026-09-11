"""Render the exe icon (build/icon.ico) with the standard library only.

The design is the app's favicon (FAVICON_SVG in server/app.py): a dark rounded square with a blue
double note. Drawing it here with signed-distance functions instead of committing a binary .ico
keeps the repository text-only and needs neither Pillow nor ImageMagick. The output is
deterministic, so rebuilding never changes the exe for no reason.
"""

from __future__ import annotations

import logging
import math
import struct
from pathlib import Path

log = logging.getLogger(__name__)

SIZES = (16, 32, 48, 256)
BACKGROUND = (0x0D, 0x0F, 0x14)  # #0d0f14
INK = (0x5D, 0x8D, 0xFF)  # #5d8dff
VIEW = 24.0  # the SVG viewBox is 24 x 24 units
CORNER_RADIUS = 6.0
STROKE = 2.0
# M9 18 V5 L21 3 V16 : the note's stem, beam and second stem.
SEGMENTS = (((9.0, 18.0), (9.0, 5.0)), ((9.0, 5.0), (21.0, 3.0)), ((21.0, 3.0), (21.0, 16.0)))
# The two note heads are stroked circles (fill none), centre and radius.
RINGS = (((6.0, 18.0), 3.0), ((18.0, 16.0), 3.0))


def _segment_distance(x: float, y: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    wx, wy = x - a[0], y - a[1]
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
    return math.hypot(wx - t * vx, wy - t * vy)


def _rounded_square_distance(x: float, y: float) -> float:
    """Signed distance to the rounded 24x24 square (negative inside)."""
    half = VIEW / 2
    qx = abs(x - half) - half + CORNER_RADIUS
    qy = abs(y - half) - half + CORNER_RADIUS
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - CORNER_RADIUS


def _note_distance(x: float, y: float) -> float:
    """Signed distance to the stroked note (negative inside the stroke)."""
    d = min(_segment_distance(x, y, a, b) for a, b in SEGMENTS)
    for (cx, cy), r in RINGS:
        d = min(d, abs(math.hypot(x - cx, y - cy) - r))
    return d - STROKE / 2


def _coverage(distance: float, pixel: float) -> float:
    """Anti-aliased coverage of a pixel of size `pixel` at signed distance `distance`."""
    return max(0.0, min(1.0, 0.5 - distance / pixel))


def render_bgra(size: int) -> tuple[bytes, bytes]:
    """Return (32-bit BGRA rows bottom-up, 1-bit AND mask bottom-up) for one ICO image."""
    pixel = VIEW / size
    xor_rows: list[bytes] = []
    and_rows: list[bytes] = []
    mask_stride = ((size + 31) // 32) * 4  # mask rows are padded to 32 bits
    for j in range(size):
        y = (j + 0.5) * pixel
        row = bytearray()
        mask = bytearray(mask_stride)
        for i in range(size):
            x = (i + 0.5) * pixel
            alpha = _coverage(_rounded_square_distance(x, y), pixel)
            ink = _coverage(_note_distance(x, y), pixel)
            r, g, b = (round(BACKGROUND[c] * (1 - ink) + INK[c] * ink) for c in range(3))
            a = round(alpha * 255)
            row += bytes((b, g, r, a))
            if a == 0:
                mask[i // 8] |= 0x80 >> (i % 8)
        xor_rows.append(bytes(row))
        and_rows.append(bytes(mask))
    # DIB rows are stored bottom-up.
    return b"".join(reversed(xor_rows)), b"".join(reversed(and_rows))


def build_ico(sizes: tuple[int, ...] = SIZES) -> bytes:
    images: list[bytes] = []
    for size in sizes:
        xor, mask = render_bgra(size)
        header = struct.pack(
            "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0
        )
        images.append(header + xor + mask)
    directory = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(directory) + 16 * len(sizes)
    entries = bytearray()
    for size, image in zip(sizes, images, strict=True):
        wh = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return directory + bytes(entries) + b"".join(images)


def make_icon(path: Path) -> str:
    """Write the icon to `path` (only if it changed) and return the path as a string."""
    data = build_ico()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_bytes() != data:
        path.write_bytes(data)
        log.info("Wrote %s (%d bytes)", path, len(data))
    return str(path)


if __name__ == "__main__":  # pragma: no cover - manual check: python packaging/make_icon.py out.ico
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    make_icon(Path(sys.argv[1] if len(sys.argv) > 1 else "icon.ico"))
