"""Publisher of the two P5 maps (P5 spec 2.8, 2.9): the review map and the push map. No learner access, no clock of its own.

- **The review map** (``radar_zones/review/map``) follows what the user selects. The layer gives this object the
  learner's ``DebugMapData`` for the selected item; the base image (heat, outlines, zones, highlight, radar) is rendered
  once and its PNG cached per map revision. A live frame copies the base pixels, draws the markers and encodes: the
  blur of the heat map is not repeated (``draw_live``). Identical PNG bytes are not published twice in a row, except by
  ``republish`` (after a discovery burst and 10 s later: Home Assistant subscribes to an image topic only after it has
  created the entity, so a first copy can be lost). At most one render per second (``DEBOUNCE_S``) outside the live view.
- **The push map** (``radar_zones/review/push_map``) is the map of the head item of the push request. It is rendered once
  per ``seq`` and its bytes are kept, so the phone finds the image of the head the notification names, however late it
  fetches it; ``republish`` sends the kept bytes again.

With ``enabled`` False (the add-on option ``debug_map`` is false) nothing is rendered or published.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .png import encode_png
from .render import draw_live, render_review

DEBOUNCE_S = 1.0

log = logging.getLogger("radar_zone_learner.maps")


class ReviewMapPublisher:
    def __init__(self, publish: Callable[..., bool], topics, render_fn: Callable = render_review,
                 frame_fn: Callable = draw_live, encode_fn: Callable = encode_png,
                 monotonic: Callable[[], float] = time.monotonic, enabled: bool = True) -> None:
        self._publish = publish
        self.topics = topics
        self._render = render_fn
        self._frame = frame_fn
        self._encode = encode_fn
        self._mono = monotonic
        self.enabled = enabled
        self.dirty = True                    # the base changed (or was never made): a new one is due
        self._data = None                    # the DebugMapData the base was made from (its geometry draws the markers)
        self._base: Optional[tuple] = None   # (width, height, palette, pixels) without live markers
        self._static_png: Optional[bytes] = None
        self._last_png: Optional[bytes] = None       # what the review map topic last got
        self._last_at: Optional[float] = None
        self._push_png: Optional[bytes] = None
        self._push_last: Optional[bytes] = None
        self.renders = 0
        self.published = 0
        self.push_published = 0
        self.errors = 0                      # renders or encodes that raised: logged by class, never retried in a loop

    def _failed(self, what: str, exc: Exception) -> None:
        self.errors += 1
        log.error("%s failed: %s", what, exc.__class__.__name__)

    # ------------------------------------------------------------ the review map
    @property
    def has_base(self) -> bool:
        return self._base is not None

    def mark_dirty(self) -> None:
        """A ``ReviewChanged``, ``AdvancedChanged`` or ``DebugMapChanged`` made the base out of date."""
        self.dirty = True

    def resend_due(self, now: float) -> bool:
        """True when the base is out of date and the 1 s debounce has passed: a publish is due."""
        return self.enabled and self.dirty and (self._last_at is None or now - self._last_at >= DEBOUNCE_S)

    def set_base(self, data) -> bool:
        """Renders the base image of ``data`` (the learner's map for the selected item, no live markers) and caches it.
        False when the render raised: the old base stays and nothing is retried until the next change."""
        try:
            base = self._render(data)
        except Exception as exc:
            self._failed("rendering the review map", exc)
            self.dirty = False
            return False
        self._base = base
        self._data = data
        self._static_png = None
        self.dirty = False
        self.renders += 1
        return True

    def publish_selected(self, data, now: float, force: bool = False) -> bool:
        """Renders ``data`` as the new base and publishes its static PNG. True when a message went out."""
        if not self.enabled:
            return False
        if not self.set_base(data):
            return False
        self._last_at = now
        png = self._static()
        return png is not None and self._send(png, force)

    def publish_frame(self, points, now: float) -> bool:
        """Publishes the cached base with the live markers ``((x_m, y_m, stay), ...)`` drawn on a copy; nothing when the
        bytes equal the last ones. Needs a base."""
        if not self.enabled or self._base is None:
            return False
        try:
            w, h, palette, pixels = self._frame(self._base, self._data, tuple(points))
            png = self._encode(w, h, palette, pixels)
        except Exception as exc:
            self._failed("drawing a live frame", exc)
            return False
        self._last_at = now
        return self._send(png, False)

    def _static(self) -> Optional[bytes]:
        if self._static_png is None and self._base is not None:
            try:
                w, h, palette, pixels = self._base
                self._static_png = self._encode(w, h, palette, pixels)
            except Exception as exc:
                self._failed("encoding the review map", exc)
        return self._static_png

    def _send(self, png: bytes, force: bool) -> bool:
        if not force and png == self._last_png:
            return False
        if self._publish(self.topics.review_map, png, qos=0, retain=False):
            self._last_png = png
            self.published += 1
            return True
        return False

    # ------------------------------------------------------------ the push map
    @property
    def push_png(self) -> Optional[bytes]:
        return self._push_png

    def keep_push(self, data) -> None:
        """Renders ``data`` (the map with the head item highlighted, no markers) as the push map and keeps its bytes,
        without publishing them (after an add-on restart: ``republish`` sends them)."""
        if not self.enabled:
            return
        try:
            w, h, palette, pixels = self._render(data)
            self._push_png = self._encode(w, h, palette, pixels)
        except Exception as exc:
            self._failed("rendering the push map", exc)
            return
        self.renders += 1

    def publish_push(self, data, force: bool = False) -> bool:
        """``keep_push`` and publish. Called once per ``seq``."""
        if not self.enabled:
            return False
        self.keep_push(data)
        return self._send_push(force)

    def _send_push(self, force: bool) -> bool:
        if self._push_png is None:
            return False
        if not force and self._push_png == self._push_last:
            return False
        if self._publish(self.topics.review_push_map, self._push_png, qos=0, retain=False):
            self._push_last = self._push_png
            self.push_published += 1
            return True
        return False

    # ------------------------------------------------------------ after a discovery burst, and on Forget all
    def republish_selected(self, live: bool = False) -> None:
        """Sends the review map again, whatever was sent before: the static PNG of the current base, or while the live view
        runs (``live``) the last frame, which has the markers."""
        if not self.enabled:
            return
        png = self._last_png if live else None
        if png is None:
            png = self._static()
        if png is not None:
            self._send(png, True)

    def republish_push(self) -> None:
        """Sends the kept push map again, whatever was sent before."""
        if self.enabled:
            self._send_push(True)

    def republish(self, live: bool = False) -> None:
        """Both maps again (the review map first)."""
        self.republish_selected(live)
        self.republish_push()

    def forget(self) -> None:
        """Forget all: nothing of the old maps stays in RAM."""
        self.dirty = True
        self._data = self._base = self._static_png = None
        self._last_png = self._last_at = None
        self._push_png = self._push_last = None
