import random
import struct
import unittest
import zlib

from radar_zone_learner.shell.png import encode_png


def read_png(data: bytes):
    """Minimal reader for 8-bit indexed PNGs with filter 0 rows; checks every chunk CRC."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "signature"
    pos, chunks = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, f"CRC of {kind!r}"
        chunks.append((kind, body))
        pos += 12 + length
    assert pos == len(data), "trailing bytes"
    kinds = [k for k, _ in chunks]
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND" and b"PLTE" in kinds, kinds
    assert kinds.index(b"PLTE") < kinds.index(b"IDAT"), "PLTE before IDAT"
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    plte = dict(chunks)[b"PLTE"]
    palette = [tuple(plte[i:i + 3]) for i in range(0, len(plte), 3)]
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    assert len(raw) == height * (width + 1), "row count"
    pixels = bytearray()
    for row in range(height):
        line = raw[row * (width + 1):(row + 1) * (width + 1)]
        assert line[0] == 0, "filter type 0"
        pixels += line[1:]
    return {"width": width, "height": height, "depth": depth, "ctype": ctype, "comp": comp, "filter": filt,
            "interlace": interlace, "palette": palette, "pixels": bytes(pixels), "chunks": kinds}


class PngTest(unittest.TestCase):
    def test_one_pixel(self):
        png = encode_png(1, 1, [(1, 2, 3)], b"\x00")
        info = read_png(png)
        self.assertEqual((info["width"], info["height"], info["depth"], info["ctype"]), (1, 1, 8, 3))
        self.assertEqual((info["comp"], info["filter"], info["interlace"]), (0, 0, 0))
        self.assertEqual(info["palette"], [(1, 2, 3)])
        self.assertEqual(info["pixels"], b"\x00")
        self.assertEqual(info["chunks"], [b"IHDR", b"PLTE", b"IDAT", b"IEND"])

    def test_280_by_160(self):
        rng = random.Random(7)
        palette = [(i * 9 % 256, i * 37 % 256, i * 71 % 256) for i in range(27)]
        pixels = bytearray(280 * 160)
        for i in range(0, len(pixels), 3):
            pixels[i] = rng.randrange(27)
        png = encode_png(280, 160, palette, bytes(pixels))
        info = read_png(png)
        self.assertEqual((info["width"], info["height"]), (280, 160))
        self.assertEqual(info["palette"], palette)
        self.assertEqual(info["pixels"], bytes(pixels))

    def test_rows_are_not_mixed(self):
        pixels = bytes([0, 1, 2, 3, 4, 5])
        info = read_png(encode_png(3, 2, [(0, 0, 0)] * 6, pixels))
        self.assertEqual(info["pixels"], pixels)

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            encode_png(2, 2, [(0, 0, 0)], b"\x00\x00\x00")         # too few pixels
        with self.assertRaises(ValueError):
            encode_png(1, 1, [(0, 0, 0)], b"\x01")                 # index outside the palette
        with self.assertRaises(ValueError):
            encode_png(0, 1, [(0, 0, 0)], b"")
        with self.assertRaises(ValueError):
            encode_png(1, 1, [], b"\x00")


if __name__ == "__main__":
    unittest.main()
