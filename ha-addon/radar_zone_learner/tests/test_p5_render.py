"""The review map (spec 2.9): the P5 palette and layers, live markers, the old render unchanged, the publisher
(cache, 1 s debounce, no identical bytes twice, the push map, enabled False)."""
import dataclasses
import hashlib
import unittest

from radar_zone_learner.core.types import Label
from radar_zone_learner.shell import render as R
from radar_zone_learner.shell.png import encode_png
from radar_zone_learner.shell.reviewmap import DEBOUNCE_S, ReviewMapPublisher
from radar_zone_learner.shell.topics_p5 import build_topics_p5
from tests.fakes import empty_debug_map
from tests.test_p5_topics import settings
from tests.test_render import block, candidate, cell, suggestion

# sha256 of the pixels of the P4 render of the data of ``old_data()`` (made from the P4 code at 58804fa)
OLD_PIXELS_SHA256 = "c995a7915d8909c5b83a6f1e32cf53039517bf4d85ebe08a45912bde2145f453"
NX = 70


def old_data():
    rates = [0.0] * 2800
    for i in range(30):
        rates[1000 + i * 7 % 800] = (i % 9) + 0.5
    return empty_debug_map(
        dwell_rate=tuple(rates), envelope=block(10, 5, 20, 20), doors=((-4.0, 5.0),),
        suggestions=(suggestion(block(5, 10, 3, 3), sid="s1"), suggestion(block(20, 10, 3, 3), kind="bed", sid="s2"),
                     suggestion(block(35, 10, 3, 3), type_="reflection", kind="reflection", sid="s3")),
        candidates=(candidate(block(5, 25, 3, 3)),),
        labels=(Label(id="l0001", created_s=0, x_m=-2.8, y_m=6.1, name="Bed", kind="bed", cells=frozenset()),))


def px(image, col, row):
    return image[3][row * image[0] + col]


def top_left(c):
    i, j = c % NX, c // NX
    return i * 4, (40 - 1 - j) * 4


def corner(cells):
    return max(cells, key=lambda c: (c // NX, -(c % NX)))            # the top-left cell of a block


class PaletteTest(unittest.TestCase):
    def test_thirty_two_colours_and_the_old_ones_do_not_move(self):
        w, h, palette, pixels = R.render(empty_debug_map())
        self.assertEqual(len(palette), 32)
        self.assertEqual(len(R.PALETTE), 32)
        self.assertEqual((R.PLACE, R.BED, R.RADAR), (19, 20, 26))
        self.assertEqual(R.ZONE, 27)
        self.assertEqual(palette[R.ZONE], (0x32, 0xcd, 0x32))
        self.assertEqual(palette[R.HIGHLIGHT], (0xff, 0xff, 0x00))
        self.assertEqual(palette[R.LIVE], (0xff, 0x2a, 0x2a))
        self.assertEqual(palette[R.LIVE_STAY], (0xff, 0x9f, 0x1a))
        self.assertEqual(palette[R.ITEM_OTHER], (0x6b, 0x8c, 0xae))
        self.assertEqual((R.HIGHLIGHT, R.LIVE, R.LIVE_STAY, R.ITEM_OTHER), (28, 29, 30, 31))

    def test_a_p4_render_is_unchanged_pixel_for_pixel(self):
        image = R.render(old_data())
        self.assertEqual(hashlib.sha256(image[3]).hexdigest(), OLD_PIXELS_SHA256)
        self.assertEqual(R.render_review(old_data()), image)

    def test_the_png_stays_small(self):
        data = dataclasses.replace(old_data(), zones=((block(40, 20, 4, 4), False),), others=(block(50, 20, 3, 3),),
                                   highlight=block(5, 10, 3, 3), focus=(-5.6, 2.2), live=((0.0, 3.0, True),))
        w, h, palette, pixels = R.render_review(data)
        self.assertLess(len(encode_png(w, h, palette, pixels)), 8192)


class LayersTest(unittest.TestCase):
    def outline_colour(self, cells, **fields):
        image = R.render_review(empty_debug_map(**fields))
        col, row = top_left(corner(cells))
        return px(image, col, row), image

    def test_zones_others_and_the_highlight(self):
        a, b, c, d = block(5, 10, 3, 3), block(20, 10, 3, 3), block(35, 10, 3, 3), block(50, 10, 3, 3)
        data = empty_debug_map(zones=((a, False), (b, True)), others=(c,), highlight=d, focus=(2.0, 3.0))
        image = R.render_review(data)
        for cells, colour in ((a, R.ZONE), (b, R.BED), (c, R.ITEM_OTHER), (d, R.HIGHLIGHT)):
            with self.subTest(colour=colour):
                col, row = top_left(corner(cells))
                self.assertEqual(px(image, col, row), colour)
                inner = top_left(cell(corner(cells) % NX + 1, corner(cells) // NX - 1))
                self.assertNotEqual(px(image, inner[0] + 1, inner[1] + 1), colour)        # outlines only

    def test_focus_marker_is_five_by_five(self):
        image = R.render_review(empty_debug_map(highlight=block(60, 30, 3, 3), focus=(-3.0, 4.0)))
        col, row = 4 * int((-3.0 + 7.0) / 0.2), 159 - int(4.0 / 0.2 * 4)
        near = {(q, r) for r in range(row - 8, row + 9) for q in range(col - 8, col + 9)
                if px(image, q, r) == R.HIGHLIGHT}
        self.assertEqual(near, {(q, r) for q in range(col - 2, col + 3) for r in range(row - 2, row + 3)})

    def test_draw_order(self):
        # a zone drawn over a suggestion; the highlight over a zone; labels over the highlight
        both = block(5, 10, 3, 3)
        data = empty_debug_map(suggestions=(suggestion(both),), zones=((both, False),), highlight=both)
        col, row = top_left(corner(both))
        self.assertEqual(px(R.render_review(data), col, row), R.HIGHLIGHT)
        data = empty_debug_map(suggestions=(suggestion(both),), zones=((both, False),))
        self.assertEqual(px(R.render_review(data), col, row), R.ZONE)
        data = empty_debug_map(suggestions=(suggestion(both),))
        self.assertEqual(px(R.render_review(data), col, row), R.PLACE)

    def test_labels_and_the_radar_stay_over_the_highlight(self):
        lab = Label(id="l0001", created_s=0, x_m=-5.6, y_m=2.2, name="", kind="manual", cells=frozenset())
        data = empty_debug_map(labels=(lab,), highlight=block(5, 10, 3, 3), focus=(-5.6, 2.2))
        col, row = 4 * int((-5.6 + 7.0) / 0.2), 159 - int(2.2 / 0.2 * 4)
        self.assertEqual(px(R.render_review(data), col, row), R.LABEL_MANUAL)
        data = empty_debug_map(highlight=frozenset(range(2800)), focus=(0.0, 0.0))
        self.assertEqual(px(R.render_review(data), 140, 159), R.RADAR)

    def test_nothing_is_drawn_for_empty_new_fields(self):
        self.assertEqual(R.render_review(empty_debug_map()), R.render(empty_debug_map()))
        plain = R.render(empty_debug_map())
        self.assertEqual(R.render(empty_debug_map(zones=(), others=(), highlight=frozenset(), focus=None, live=())), plain)

    def test_out_of_range_cells_are_ignored(self):
        data = empty_debug_map(zones=((frozenset({-1, 2800, 99999}), False),), others=(frozenset({5000}),),
                               highlight=frozenset({-5}), focus=(float("nan"), 1.0))
        w, h, _, pixels = R.render_review(data)
        self.assertEqual(len(pixels), w * h)


class LiveMarkerTest(unittest.TestCase):
    def test_a_marker_is_seven_by_seven(self):
        image = R.render_review(empty_debug_map(live=((-3.0, 4.0, False),)))
        col, row = 4 * int((-3.0 + 7.0) / 0.2), 159 - int(4.0 / 0.2 * 4)
        square = {(q, r) for q in range(col - 3, col + 4) for r in range(row - 3, row + 4)}
        pixels = {(q, r) for r in range(160) for q in range(280) if px(image, q, r) == R.LIVE}
        self.assertEqual(pixels, square)
        self.assertEqual(len(pixels), 49)

    def test_a_stay_is_orange(self):
        image = R.render_review(empty_debug_map(live=((-3.0, 4.0, True), (3.0, 4.0, False))))
        orange = [1 for r in range(160) for q in range(280) if px(image, q, r) == R.LIVE_STAY]
        red = [1 for r in range(160) for q in range(280) if px(image, q, r) == R.LIVE]
        self.assertEqual((len(orange), len(red)), (49, 49))

    def test_live_markers_are_on_top_of_everything(self):
        data = empty_debug_map(zones=((block(35, 10, 8, 8), False),), highlight=block(35, 10, 8, 8),
                               live=((0.0, 0.0, False),))                    # on the radar marker
        image = R.render_review(data)
        self.assertEqual(px(image, 140, 159), R.LIVE)
        self.assertEqual(px(image, 140, 156), R.LIVE)

    def test_draw_live_equals_a_render_with_the_markers(self):
        base_data = dataclasses.replace(old_data(), highlight=block(5, 10, 3, 3), focus=(-5.6, 2.2))
        points = ((-3.0, 4.0, False), (1.5, 2.0, True), (0.0, 7.9, False))
        base = R.render_review(base_data)
        with_markers = R.render_review(dataclasses.replace(base_data, live=points))
        self.assertEqual(R.draw_live(base, base_data, points), with_markers)
        self.assertEqual(R.draw_live(base, base_data, ()), base)
        self.assertEqual(R.draw_live(base, base_data, ())[3], base[3])
        self.assertNotEqual(R.draw_live(base, base_data, points)[3], base[3])

    def test_draw_live_leaves_the_base_alone(self):
        data = empty_debug_map()
        base = R.render_review(data)
        before = bytes(base[3])
        R.draw_live(base, data, ((0.0, 3.0, False),))
        self.assertEqual(base[3], before)

    def test_a_base_of_another_size_is_refused(self):
        with self.assertRaises(ValueError):
            R.draw_live((10, 10, [(0, 0, 0)], bytes(100)), empty_debug_map(), ())

    def test_non_finite_points_and_points_outside_are_harmless(self):
        data = empty_debug_map(live=((float("nan"), 1.0, False), (99.0, 99.0, False), (-99.0, -99.0, True)))
        w, h, _, pixels = R.render_review(data)
        self.assertEqual(len(pixels), w * h)


class FakePublish:
    def __init__(self):
        self.sent = []
        self.ok = True

    def __call__(self, topic, payload, qos=1, retain=False):
        if self.ok:
            self.sent.append((topic, payload, qos, retain))
        return self.ok


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.topics = build_topics_p5(settings())
        self.pub = FakePublish()
        self.m = ReviewMapPublisher(self.pub, self.topics, enabled=True)
        self.data = dataclasses.replace(old_data(), highlight=block(5, 10, 3, 3), focus=(-5.6, 2.2))

    def topics_sent(self):
        return [s[0] for s in self.pub.sent]

    def test_the_review_map_is_not_retained_qos_zero(self):
        self.assertTrue(self.m.publish_selected(self.data, 10.0))
        topic, payload, qos, retain = self.pub.sent[0]
        self.assertEqual((topic, qos, retain), ("radar_zones/review/map", 0, False))
        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertLess(len(payload), 8192)

    def test_identical_bytes_are_not_published_twice(self):
        self.assertTrue(self.m.publish_selected(self.data, 10.0))
        self.assertFalse(self.m.publish_selected(self.data, 12.0))
        self.assertEqual(len(self.pub.sent), 1)
        self.assertTrue(self.m.publish_selected(dataclasses.replace(self.data, highlight=frozenset()), 14.0))
        self.assertEqual(len(self.pub.sent), 2)

    def test_the_debounce(self):
        self.assertTrue(self.m.dirty)
        self.assertTrue(self.m.resend_due(0.0))                    # never published: due at once
        self.m.publish_selected(self.data, 10.0)
        self.assertFalse(self.m.dirty)
        self.assertFalse(self.m.resend_due(11.0))                  # nothing changed
        self.m.mark_dirty()
        self.assertFalse(self.m.resend_due(10.0 + DEBOUNCE_S - 0.1))
        self.assertTrue(self.m.resend_due(10.0 + DEBOUNCE_S))
        self.assertEqual(DEBOUNCE_S, 1.0)

    def test_the_base_is_rendered_once_per_revision(self):
        renders = []

        def render_fn(data):
            renders.append(1)
            return R.render_review(data)
        m = ReviewMapPublisher(self.pub, self.topics, render_fn=render_fn)
        m.publish_selected(self.data, 1.0)
        for k in range(5):
            m.publish_frame(((k * 0.5 - 2.0, 3.0, False),), 2.0 + k)
        self.assertEqual(len(renders), 1)                          # the five frames cost markers and the encode only
        self.assertEqual(len(self.pub.sent), 6)
        m.set_base(self.data)
        self.assertEqual(len(renders), 2)

    def test_a_frame_with_the_same_markers_is_not_published_again(self):
        self.m.publish_selected(self.data, 1.0)
        self.assertTrue(self.m.publish_frame(((1.0, 3.0, False),), 2.0))
        self.assertFalse(self.m.publish_frame(((1.0, 3.0, False),), 4.0))
        self.assertTrue(self.m.publish_frame(((1.5, 3.0, False),), 6.0))
        self.assertEqual(len(self.pub.sent), 3)

    def test_a_frame_needs_a_base(self):
        self.assertFalse(self.m.has_base)
        self.assertFalse(self.m.publish_frame(((1.0, 3.0, False),), 1.0))
        self.assertEqual(self.pub.sent, [])

    def test_the_final_static_map_after_the_live_view_differs_from_the_last_frame(self):
        self.m.publish_selected(self.data, 1.0)
        static = self.pub.sent[0][1]
        self.m.publish_frame(((1.0, 3.0, False),), 2.0)
        self.assertNotEqual(self.pub.sent[-1][1], static)
        self.assertTrue(self.m.publish_selected(self.data, 3.0))
        self.assertEqual(self.pub.sent[-1][1], static)

    def test_the_push_map_is_kept_and_sent_again_by_republish(self):
        self.assertTrue(self.m.publish_push(self.data))
        topic, payload, qos, retain = self.pub.sent[0]
        self.assertEqual((topic, qos, retain), ("radar_zones/review/push_map", 0, False))
        self.assertEqual(self.m.push_png, payload)
        self.assertFalse(self.m.publish_push(self.data))             # the same bytes
        self.m.publish_selected(self.data, 1.0)
        self.pub.sent.clear()
        self.m.republish()
        self.assertEqual(self.topics_sent(), ["radar_zones/review/map", "radar_zones/review/push_map"])
        self.pub.sent.clear()
        self.m.republish()                                          # whatever was sent before: again
        self.assertEqual(len(self.pub.sent), 2)

    def test_keep_push_publishes_nothing(self):
        self.m.keep_push(self.data)
        self.assertEqual(self.pub.sent, [])
        self.assertIsNotNone(self.m.push_png)
        self.m.republish()
        self.assertEqual(self.topics_sent(), ["radar_zones/review/push_map"])

    def test_republish_during_the_live_view_sends_the_last_frame(self):
        self.m.publish_selected(self.data, 1.0)
        self.m.publish_frame(((1.0, 3.0, False),), 2.0)
        frame = self.pub.sent[-1][1]
        self.pub.sent.clear()
        self.m.republish(live=True)
        self.assertEqual(self.pub.sent[0][1], frame)
        self.pub.sent.clear()
        self.m.republish(live=False)
        self.assertNotEqual(self.pub.sent[0][1], frame)

    def test_a_failed_publish_is_retried_not_forgotten(self):
        self.pub.ok = False
        self.assertFalse(self.m.publish_selected(self.data, 1.0))
        self.pub.ok = True
        self.assertTrue(self.m.publish_selected(self.data, 2.0))
        self.assertEqual(len(self.pub.sent), 1)

    def test_a_render_error_is_counted_and_not_retried_until_the_next_change(self):
        def boom(data):
            raise ValueError("secret position 3.2, 4.1")
        m = ReviewMapPublisher(self.pub, self.topics, render_fn=boom)
        with self.assertLogs("radar_zone_learner.maps", level="ERROR") as logs:
            self.assertFalse(m.publish_selected(self.data, 1.0))
        self.assertEqual(m.errors, 1)
        self.assertFalse(m.dirty)                                  # no retry in a loop
        self.assertFalse(m.resend_due(100.0))
        self.assertNotIn("3.2", " ".join(logs.output))             # the class, never the message
        self.assertIn("ValueError", " ".join(logs.output))
        m.mark_dirty()
        self.assertTrue(m.resend_due(100.0))
        self.assertEqual(self.pub.sent, [])
        with self.assertLogs("radar_zone_learner.maps", level="ERROR"):
            self.assertFalse(m.publish_push(self.data))
        self.assertEqual(m.errors, 2)

    def test_an_encode_error_in_a_frame_is_counted(self):
        m = ReviewMapPublisher(self.pub, self.topics)
        m.publish_selected(self.data, 1.0)

        def boom(*args):
            raise OSError("x")
        m._encode = boom
        with self.assertLogs("radar_zone_learner.maps", level="ERROR"):
            self.assertFalse(m.publish_frame(((1.0, 3.0, False),), 2.0))
        self.assertEqual(m.errors, 1)

    def test_disabled_renders_and_publishes_nothing(self):
        m = ReviewMapPublisher(self.pub, self.topics, enabled=False)
        self.assertFalse(m.publish_selected(self.data, 1.0))
        self.assertFalse(m.publish_push(self.data))
        self.assertFalse(m.publish_frame(((1.0, 3.0, False),), 1.0))
        self.assertFalse(m.resend_due(100.0))
        m.keep_push(self.data)
        m.republish()
        self.assertEqual(self.pub.sent, [])
        self.assertEqual(m.renders, 0)

    def test_forget_clears_everything_in_ram(self):
        self.m.publish_selected(self.data, 1.0)
        self.m.publish_push(self.data)
        self.m.forget()
        self.assertFalse(self.m.has_base)
        self.assertIsNone(self.m.push_png)
        self.assertTrue(self.m.dirty)
        self.pub.sent.clear()
        self.m.republish()
        self.assertEqual(self.pub.sent, [])


if __name__ == "__main__":
    unittest.main()
