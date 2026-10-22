import unittest

from radar_zone_learner.core.types import CandidateView, Label, Suggestion
from radar_zone_learner.shell import render as R
from radar_zone_learner.shell.png import encode_png
from tests.fakes import empty_debug_map

NX = 70


def cell(i, j):
    return j * NX + i


def block(i0, j0, w, h):
    return frozenset(cell(i, j) for i in range(i0, i0 + w) for j in range(j0, j0 + h))


def suggestion(cells, type_="dwell_zone", kind="dwell", state="shadow", sid="s0005-1"):
    return Suggestion(id=sid, rev=1, type=type_, kind=kind, name_hint=None, state=state, first_night=5,
                      last_night=5, cx_m=0.0, cy_m=0.0, cells=cells, poly_cm=(), conf=0.7, evidence={},
                      variants=(), variant_evidence_split=False, variant_share_split=False,
                      variant_suppressed=False, frozen=False)


def candidate(cells, outcome="failed", cid="c0005-1"):
    return CandidateView(cid=cid, cls="dwell", cells=cells, cx_m=0.0, cy_m=0.0, n=3, core_days=1, minutes=5.0,
                         median_s=60.0, ld_share=1.0, ghost_share=0.0, covis=0.0, covis_src=None, streak=1,
                         conf=0.1, gates={}, outcome=outcome)


class RenderTest(unittest.TestCase):
    def px(self, image, col, row):
        width, _, _, pixels = image
        return pixels[row * width + col]

    def test_size_and_palette(self):
        w, h, palette, pixels = R.render(empty_debug_map())
        self.assertEqual((w, h), (280, 160))
        self.assertEqual(len(pixels), 280 * 160)
        self.assertLessEqual(len(palette), 32)
        self.assertEqual(palette[0], (0x1e, 0x1e, 0x1e))
        self.assertEqual(palette[R.PLACE], (0x00, 0xc8, 0xff))
        self.assertEqual(palette[R.RADAR], (0xc0, 0xc0, 0xc0))
        self.assertEqual(R.SCALE, 4)
        png = encode_png(w, h, palette, pixels)
        self.assertLess(len(png), 8192)

    def test_radar_marker_bottom_centre_and_fov_lines(self):
        image = R.render(empty_debug_map())
        self.assertEqual(self.px(image, 140, 159), R.RADAR)
        self.assertEqual(self.px(image, 138, 157), R.RADAR)
        # the +-60 degree lines reach the side edges at y = 7 / tan(60) = 4.04 m
        row_at_edge = 159 - int(4.04 / 0.2 * 4)
        left = [r for r in range(160) if self.px(image, 0, r) == R.RADAR]
        right = [r for r in range(160) if self.px(image, 279, r) == R.RADAR]
        self.assertTrue(left and right)
        self.assertLessEqual(abs(left[0] - row_at_edge), 2)
        self.assertLessEqual(abs(right[0] - row_at_edge), 2)
        # nothing straight ahead of the radar in an empty map
        self.assertEqual(self.px(image, 140, 40), R.BACKGROUND)

    def test_heat_top_rows(self):
        rates = [0.0] * 2800
        rates[cell(10, 39)] = 5.0                  # y = 7.8-8.0 m: the top cell row
        rates[cell(11, 39)] = 0.5
        rates[cell(12, 39)] = 1e-6                 # a decayed remainder: not drawn
        image = R.render(empty_debug_map(dwell_rate=tuple(rates)))
        self.assertEqual(self.px(image, 41, 0), R.HEAT_FIRST + R.HEAT_LEVELS - 1)
        self.assertEqual(self.px(image, 41, 3), R.HEAT_FIRST + R.HEAT_LEVELS - 1)
        self.assertEqual(self.px(image, 41, 4), R.BACKGROUND)
        low = self.px(image, 45, 1)
        self.assertTrue(R.HEAT_FIRST <= low < R.HEAT_FIRST + R.HEAT_LEVELS - 1)
        self.assertEqual(self.px(image, 49, 1), R.BACKGROUND)
        self.assertEqual(self.px(image, 53, 1), R.BACKGROUND)

    def test_y_7_9_is_in_the_top_rows(self):
        lab = Label(id="l0001", created_s=0, x_m=0.0, y_m=7.9, name="Desk", kind="manual", cells=frozenset())
        image = R.render(empty_debug_map(labels=(lab,)))
        rows = [r for r in range(160) if self.px(image, 140, r) == R.LABEL_MANUAL]
        self.assertTrue(rows)
        self.assertLessEqual(max(rows), 5)

    def test_outline_colours(self):
        place = block(5, 10, 3, 3)
        bed = block(20, 10, 3, 3)
        refl = block(35, 10, 3, 3)
        ignore = block(50, 10, 3, 3)
        stale = block(60, 10, 3, 3)
        failed = block(5, 25, 3, 3)
        data = empty_debug_map(
            suggestions=(suggestion(place, sid="s1"), suggestion(bed, kind="bed", sid="s2"),
                         suggestion(refl, type_="reflection", kind="reflection", sid="s3"),
                         suggestion(ignore, type_="ignore_zone", kind="ignore", sid="s4"),
                         suggestion(stale, state="stale", sid="s5")),
            candidates=(candidate(failed), candidate(place, outcome="suggested", cid="c2")),
            labels=(Label(id="l0001", created_s=0, x_m=-2.8, y_m=6.1, name="Bed", kind="bed", cells=frozenset()),))
        image = R.render(data)

        def top_left(c):
            i, j = c % NX, c // NX
            return i * 4, (40 - 1 - j) * 4

        for cells, colour in ((place, R.PLACE), (bed, R.BED), (refl, R.REFLECTION), (ignore, R.IGNORE),
                              (stale, R.FAILED_CANDIDATE), (failed, R.FAILED_CANDIDATE)):
            with self.subTest(colour=colour):
                corner = max(cells, key=lambda c: (c // NX, -(c % NX)))      # top-left cell of the block
                col, row = top_left(corner)
                self.assertEqual(self.px(image, col, row), colour)          # outline on the block edge
                inner = cell(corner % NX + 1, corner // NX - 1)              # the centre cell
                col, row = top_left(inner)
                self.assertNotEqual(self.px(image, col + 1, row + 1), colour)  # inside stays unfilled
        col, row = 4 * int((-2.8 + 7.0) / 0.2), 159 - int(6.1 / 0.2 * 4)
        self.assertEqual(self.px(image, col, row), R.LABEL_BED)

    def test_doors_and_envelope(self):
        env = block(10, 5, 20, 20)
        data = empty_debug_map(envelope=env, doors=((-4.0, 5.0),))
        image = R.render(data)
        self.assertEqual(self.px(image, 40, (39 - 24) * 4), R.ENVELOPE_EDGE)   # top edge of the envelope
        self.assertEqual(self.px(image, 4 * 15, 159 - 100), R.DOOR)

    def test_out_of_range_cells_ignored(self):
        data = empty_debug_map(envelope=frozenset({-1, 2800, 99999}), dwell_rate=tuple([float("nan")] * 2800))
        w, h, _, pixels = R.render(data)
        self.assertEqual(len(pixels), w * h)


if __name__ == "__main__":
    unittest.main()
