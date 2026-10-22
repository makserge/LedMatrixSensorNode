"""Standard-library PNG encoder: 8-bit indexed colour, one IDAT (spec 10.6)."""
from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def encode_png(width: int, height: int, palette: list, pixels: bytes) -> bytes:
    """PNG of colour type 3 (indexed), bit depth 8, one palette index per pixel, rows top to bottom.

    Each row is stored with filter type 0; the image data is one ``zlib.compress(raw, 9)`` IDAT chunk.
    """
    if not (isinstance(width, int) and isinstance(height, int)) or width < 1 or height < 1:
        raise ValueError("width and height must be positive integers")
    if not 1 <= len(palette) <= 256:
        raise ValueError("the palette needs 1..256 entries")
    pixels = bytes(pixels)
    if len(pixels) != width * height:
        raise ValueError("pixels must hold width * height palette indices")
    if max(pixels) >= len(palette):
        raise ValueError("a pixel refers to a palette entry that does not exist")
    plte = bytearray()
    for entry in palette:
        r, g, b = entry
        plte += bytes((r, g, b))
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw += pixels[row * width:(row + 1) * width]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    return (PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"PLTE", bytes(plte))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))
