"""The ``zone6`` state key: container, defaults, validation, orphans (P6 spec 6, A.6). Owner: CORE.

P6 adds one optional key ``zone6`` inside the existing ``p5`` section (D2, D5). ``P5State`` already keeps every unknown
key of ``p5`` in ``extra`` and writes it back, so this module needs no edit of ``p5state.py``: the learner builds a
``Zone6`` from ``p5.extra["zone6"]`` at load (``Zone6.load`` validates and raises ``StateError``) and writes it back
into ``p5.extra`` immediately before every export (``sync_into``), or deletes the key when it equals its defaults, so a
learner with no zone data exports exactly what 0.5.0 exported.

Keys of ``tune``, ``delay``, ``ld``, ``info`` and ``ve`` are zone ids (``^[zl][0-9]{4,}$``). Unknown keys inside
``zone6`` are kept and written back (P7 adds ``drift``, ``moves`` and so on). Occupancy, deadlines and per-track
runtime are RAM only and never saved.
"""
from __future__ import annotations

import copy
import math
import re
from typing import Optional

from .types import VARIANT_ID_RE, ZONE_ID_RE, StateError

FORMAT_VERSION = 1
SPELL_MAX_S = 86400
SPELLS_MAX = 50
VARIANTS_MAX = 6
TH_RANGES_MAX = 3
KNOWN_KEYS = ("v", "set", "tune", "delay", "ld", "ldr", "info", "ve")

_ZID = re.compile(ZONE_ID_RE)
_VID = re.compile(VARIANT_ID_RE)
_RANGE = re.compile(r"^[0-9]{2}-[0-9]{2}$")


def _bad(msg: str):
    raise StateError(msg)


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _isnum(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if isinstance(v, int):
        return True                  # a huge integer is a number; the range checks reject it (never an OverflowError)
    return math.isfinite(v)


def _zid_map(d, what: str) -> dict:
    if not isinstance(d, dict):
        _bad("zone6.%s is not an object" % what)
    for k in d:
        if not isinstance(k, str) or not _ZID.match(k):
            _bad("zone6.%s: bad zone id" % what)
    return d


def _parse(d) -> dict:
    """Validates a ``zone6`` value and returns its parts. A missing or non-object value is the defaults.
    Raises StateError (never another exception) for a known key of the wrong JSON type or a value out of range."""
    out = {"epoch": 0, "tune": {}, "delay": {}, "ld": {}, "ldr": [], "info": {}, "ve": {}, "extra": {}}
    if d is None:
        return out
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if k not in KNOWN_KEYS:
            out["extra"][k] = copy.deepcopy(v)
    v = d.get("v", FORMAT_VERSION)
    if not _isint(v) or v < 1:
        _bad("bad zone6.v")

    st = d.get("set", {})
    if not isinstance(st, dict):
        _bad("bad zone6.set")
    ep = st.get("epoch", 0)
    if not _isint(ep) or ep < 0:
        _bad("bad zone6.set.epoch")
    out["epoch"] = ep

    tune = _zid_map(d.get("tune", {}), "tune")
    for zid, t in tune.items():
        if not isinstance(t, dict):
            _bad("bad zone6.tune entry")
        rec = {}
        if "cd" in t:
            if not _isint(t["cd"]) or not 3 <= t["cd"] <= 900:
                _bad("bad zone6.tune.cd")
            rec["cd"] = t["cd"]
        if "mg" in t:
            if not _isint(t["mg"]) or not -50 <= t["mg"] <= 100:
                _bad("bad zone6.tune.mg")
            rec["mg"] = t["mg"]
        out["tune"][zid] = rec

    delay = _zid_map(d.get("delay", {}), "delay")
    for zid, e in delay.items():
        if not isinstance(e, dict):
            _bad("bad zone6.delay entry")
        sp = e.get("sp", [])
        if not isinstance(sp, list) or len(sp) > SPELLS_MAX or \
                not all(_isint(x) and 1 <= x <= SPELL_MAX_S for x in sp):
            _bad("bad zone6.delay.sp")
        rec = {"sp": list(sp)}
        if "cd" in e:
            if not _isint(e["cd"]) or not 5 <= e["cd"] <= 600:
                _bad("bad zone6.delay.cd")
            rec["cd"] = e["cd"]
        if "upd" in e:
            if not _isint(e["upd"]) or e["upd"] < 0:
                _bad("bad zone6.delay.upd")
            rec["upd"] = e["upd"]
        out["delay"][zid] = rec

    ld = _zid_map(d.get("ld", {}), "ld")
    for zid, e in ld.items():
        if not (isinstance(e, list) and len(e) == 2 and _isnum(e[0]) and _isnum(e[1])
                and e[0] >= 0 and e[1] >= 0 and e[0] <= e[1]):
            _bad("bad zone6.ld")
        out["ld"][zid] = [e[0], e[1]]

    ldr = d.get("ldr", [])
    if not isinstance(ldr, list) or not all(isinstance(z, str) and _ZID.match(z) for z in ldr):
        _bad("bad zone6.ldr")
    out["ldr"] = sorted(set(ldr))

    info = _zid_map(d.get("info", {}), "info")
    for zid, e in info.items():
        if not isinstance(e, dict):
            _bad("bad zone6.info entry")
        rec = {}
        th = e.get("th", {})
        if not isinstance(th, dict):
            _bad("bad zone6.info.th")
        thr = {}
        for cls, rngs in th.items():
            if cls not in ("wd", "we") or not isinstance(rngs, list) or len(rngs) > TH_RANGES_MAX or \
                    not all(isinstance(r, str) and _RANGE.match(r) for r in rngs):
                _bad("bad zone6.info.th")
            thr[cls] = list(rngs)
        rec["th"] = thr
        var = e.get("var", [])
        if not isinstance(var, list) or len(var) > VARIANTS_MAX:
            _bad("bad zone6.info.var")
        vr = []
        for x in var:
            if not isinstance(x, dict) or x.get("d") not in ("wd", "we") or not _isint(x.get("s")) \
                    or not _isint(x.get("e")) or not 0 <= x["s"] <= 24 or not 0 <= x["e"] <= 24:
                _bad("bad zone6.info.var entry")
            n = x.get("n", 0)
            if not _isint(n) or n < 0:
                _bad("bad zone6.info.var.n")
            vr.append({"d": x["d"], "s": x["s"], "e": x["e"], "n": n})
        rec["var"] = vr
        conf = e.get("conf")
        if conf is not None and not (_isnum(conf) and 0.0 <= conf <= 1.0):
            _bad("bad zone6.info.conf")
        rec["conf"] = conf
        if "upd" in e:
            if not _isint(e["upd"]) or e["upd"] < 0:
                _bad("bad zone6.info.upd")
            rec["upd"] = e["upd"]
        out["info"][zid] = rec

    ve = _zid_map(d.get("ve", {}), "ve")
    for zid, e in ve.items():
        if not isinstance(e, list) or len(e) > VARIANTS_MAX or \
                not all(isinstance(x, str) and _VID.match(x) for x in e):
            _bad("bad zone6.ve entry")
        out["ve"][zid] = sorted(set(e))
    return out


def variant_id(v: dict) -> str:
    """The id of a variant record ``{"d", "s", "e"}``: ``<days><start:02d><end:02d>``, for example ``wd0917``."""
    return "%s%02d%02d" % (v["d"], v["s"], v["e"])


class Zone6:
    """The ``zone6`` container in RAM (spec 6.1)."""

    def __init__(self) -> None:
        self.epoch = 0                    # installation epoch: +1 at every Forget all (D41)
        self.tune: dict = {}              # zid -> {"cd": seconds, "mg": cm}; absent = no override
        self.delay: dict = {}             # zid -> {"sp": [seconds...], "cd": learned seconds, "upd": night}
        self.ld: dict = {}                # zid -> [on_s, known_s] LD2412 evidence
        self.ldr: list = []               # zone ids that are ld2412_reliable
        self.info: dict = {}              # zid -> {"th": {"wd": [...], "we": [...]}, "var": [...], "conf": f, "upd": n}
        self.ve: dict = {}                # zid -> variant ids whose occupancy entity was requested
        self.extra: dict = {}             # unknown keys of a newer writer: kept and written back
        self._ldr_set: frozenset = frozenset()

    # ------------------------------------------------------------ defaults and the export rule
    def is_default(self) -> bool:
        return (self.epoch == 0 and not self.tune and not self.delay and not self.ld and not self.ldr
                and not self.info and not self.ve and not self.extra)

    def export(self) -> dict:
        out = {"v": FORMAT_VERSION, "set": {"epoch": self.epoch},
               "tune": {z: dict(t) for z, t in sorted(self.tune.items())},
               "delay": {z: {k: (list(v) if k == "sp" else v) for k, v in e.items()}
                         for z, e in sorted(self.delay.items())},
               "ld": {z: [e[0], e[1]] for z, e in sorted(self.ld.items())},
               "ldr": sorted(self.ldr),
               "info": {z: copy.deepcopy(e) for z, e in sorted(self.info.items())},
               "ve": {z: list(e) for z, e in sorted(self.ve.items())}}
        for k, v in self.extra.items():
            out[k] = copy.deepcopy(v)
        return out

    # ------------------------------------------------------------ validation and load
    @staticmethod
    def validate(d) -> None:
        """Raises StateError when the value is not a valid ``zone6`` (spec 6.2)."""
        _parse(d)

    @classmethod
    def load(cls, d) -> "Zone6":
        parts = _parse(d)
        z = cls()
        z.epoch = parts["epoch"]
        z.tune = parts["tune"]
        z.delay = parts["delay"]
        z.ld = parts["ld"]
        z.set_ldr(parts["ldr"])
        z.info = parts["info"]
        z.ve = parts["ve"]
        z.extra = parts["extra"]
        return z

    def sync_into(self, p5) -> None:
        """Writes ``zone6`` into ``p5.extra`` or deletes the key when it equals the defaults (spec 6.1)."""
        if self.is_default():
            p5.extra.pop("zone6", None)
        else:
            p5.extra["zone6"] = self.export()

    # ------------------------------------------------------------ ld2412_reliable
    def set_ldr(self, ids) -> None:
        self.ldr = sorted(set(ids))
        self._ldr_set = frozenset(self.ldr)

    def is_ldr(self, zid: str) -> bool:
        return zid in self._ldr_set

    # ------------------------------------------------------------ orphans
    def prune(self, zone_ids) -> int:
        """Drops the entries of zone ids that are not in ``zone_ids``. Returns the number of ids dropped."""
        keep = set(zone_ids)
        gone = set()
        for name in ("tune", "delay", "ld", "info", "ve"):
            m = getattr(self, name)
            for z in [z for z in m if z not in keep]:
                del m[z]
                gone.add(z)
        bad = [z for z in self.ldr if z not in keep]
        if bad:
            gone.update(bad)
            self.set_ldr([z for z in self.ldr if z in keep])
        return len(gone)

    def forget_zone(self, zid: str) -> None:
        """``ZoneForget``: the learned evidence and the tuning of one zone (not the zone, D15)."""
        for name in ("tune", "delay", "ld", "info", "ve"):
            getattr(self, name).pop(zid, None)
        if zid in self._ldr_set:
            self.set_ldr([z for z in self.ldr if z != zid])

    def reset_for_forget(self) -> "Zone6":
        """Forget all (D24, D41): all zone data is dropped, the epoch is carried over plus one."""
        fresh = Zone6()
        fresh.epoch = self.epoch + 1
        return fresh

    # ------------------------------------------------------------ small accessors
    def spells(self, zid: str) -> list:
        e = self.delay.get(zid)
        return e["sp"] if e is not None else []

    def learned(self, zid: str) -> Optional[int]:
        e = self.delay.get(zid)
        return e.get("cd") if e is not None else None
