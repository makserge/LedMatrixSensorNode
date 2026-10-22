"""Decode and validate one frames payload (P3 spec 6.2).

The field checks are the project's own P1 validator (tools/frames_checker.py, validate_frames with
phase "any"), ported rule by rule. Error reasons are short fixed strings without payload content.

On top of the schema, three range limits reject values that no node can send (the broker has no ACLs,
so any client may publish to the frames topic): t <= MAX_T_MS, n <= MAX_N and a0 <= MAX_AGE_MS. The P1
node stays far inside them (a batch spans at most about 5 s; an MQTT stall costs about 10 s).
"""
from __future__ import annotations

import json
import re

from .types import FramesBatch, ListedFrame, PayloadError, Target

MAX_PAYLOAD_BYTES = 4096
MAX_LISTED = 20
MAX_T_MS = 1 << 48               # device ms since boot: 8,900 years
MAX_N = 5000                     # frames in one batch (120 s at 20 Hz is 2,400)
MAX_AGE_MS = 120000              # a0, the age of the oldest frame of a batch
FRAME_KEYS = frozenset(("v", "b", "t", "s", "n", "a0", "a1", "mt", "p2", "pw", "sm", "f"))
REQUIRED_KEYS = ("v", "b", "t", "s", "n", "pw", "sm")
SM_CODES = frozenset(("A", "N", "F", "X", "S"))
BOOT_ID_RE = re.compile(r"^[0-9a-f]{8}$")
U32 = 0xFFFFFFFF
XYV_MAX = 32767
_last_boot = [None]          # memo of the last boot id that passed the regex (a pure cache)


def _is_int(value) -> bool:
    # an int and not a bool; json.loads produces no other int subclass
    return type(value) is int


def _check_header(obj: dict) -> None:
    if not FRAME_KEYS.issuperset(obj):
        raise PayloadError("unknown key")
    for key in REQUIRED_KEYS:
        if key not in obj:
            raise PayloadError("missing " + key)
    v = obj["v"]
    if type(v) is not int or v != 1:
        raise PayloadError("bad v")
    b = obj["b"]
    if type(b) is not str or (b != _last_boot[0] and not BOOT_ID_RE.match(b)):
        raise PayloadError("bad b")
    _last_boot[0] = b
    t = obj["t"]
    if type(t) is not int or not 0 <= t <= MAX_T_MS:
        raise PayloadError("bad t")
    x = obj["s"]
    if type(x) is not int or not 0 <= x <= U32:
        raise PayloadError("bad s")
    x = obj["n"]
    if type(x) is not int or not 0 <= x <= MAX_N:
        raise PayloadError("bad n")
    pw = obj["pw"]
    if type(pw) is not int or not 0 <= pw <= 3:
        raise PayloadError("bad pw")
    sm = obj["sm"]
    if type(sm) is not str or sm not in SM_CODES:
        raise PayloadError("bad sm")
    if "mt" in obj:
        x = obj["mt"]
        if type(x) is not int or x not in (0, 1):
            raise PayloadError("bad mt")
    if "p2" in obj:
        x = obj["p2"]
        if type(x) is not int or x not in (0, 1):
            raise PayloadError("bad p2")


def parse_frames(payload: bytes) -> FramesBatch:
    """Decode one payload of <node_prefix>/radar/ld2454/frames. Raises PayloadError."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise PayloadError("payload is not bytes")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PayloadError("payload too large")
    try:
        text = bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        raise PayloadError("not UTF-8") from None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise PayloadError("not JSON") from None
    if not isinstance(obj, dict):
        raise PayloadError("not an object")
    _check_header(obj)

    n = obj["n"]
    mt = obj.get("mt")
    p2 = obj.get("p2")
    if n == 0:
        if "a0" in obj or "a1" in obj:
            raise PayloadError("ages with n 0")
        if "f" in obj:
            raise PayloadError("f with n 0")
        return FramesBatch(obj["b"], obj["t"], obj["s"], 0, None, None, mt, p2, obj["pw"], obj["sm"], ())
    if "a0" not in obj or "a1" not in obj:
        raise PayloadError("missing ages")
    a0, a1 = obj["a0"], obj["a1"]
    if not (type(a0) is int and type(a1) is int and 0 <= a1 <= a0 <= MAX_AGE_MS):
        raise PayloadError("bad ages")
    if n == 1 and a0 != a1:
        raise PayloadError("n 1 with a0 != a1")
    frames = ()
    if "f" in obj:
        frames = _parse_f(obj["f"], n, a0, a1)
    return FramesBatch(obj["b"], obj["t"], obj["s"], n, a0, a1, mt, p2, obj["pw"], obj["sm"], frames)


def _parse_f(f, n: int, a0: int, a1: int) -> tuple:
    if not isinstance(f, list) or not 1 <= len(f) <= MAX_LISTED:
        raise PayloadError("bad f")
    out = []
    prev_k = -1
    prev_age = a0
    lo, hi = -XYV_MAX, XYV_MAX
    for entry in f:
        # type(x) is int: an int and not a bool (JSON gives no other int subclass)
        if type(entry) is not list or len(entry) not in (6, 10, 14):
            raise PayloadError("bad f entry")
        for x in entry:
            if type(x) is not int:
                raise PayloadError("bad f entry")
        k = entry[0]
        age = entry[1]
        if not prev_k < k < n:
            raise PayloadError("bad k")
        if not a1 <= age <= prev_age:
            raise PayloadError("bad age")
        if (k == 0 and age != a0) or (k == n - 1 and age != a1):
            raise PayloadError("bad age")
        prev_k = k
        prev_age = age
        prev_slot = -1
        targets = []
        for j in range(2, len(entry), 4):
            slot, x, y, v = entry[j:j + 4]
            if not prev_slot < slot <= 2:
                raise PayloadError("slot order")
            prev_slot = slot
            if not (lo <= x <= hi and lo <= y <= hi and lo <= v <= hi):
                raise PayloadError("value range")
            if x == 0 and y == 0:
                continue                      # all-zero position record (P1 1.4; D6)
            targets.append(Target(slot, x, y, v))
        out.append(ListedFrame(k, age, tuple(targets)))
    return tuple(out)
