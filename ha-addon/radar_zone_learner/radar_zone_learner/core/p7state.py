"""The ``zone7`` state key: container, defaults, export rule, validation, orphans (P7 spec 5, A.5). Owner: CORE.

P7 keeps its persisted data in one optional key ``zone7`` inside the existing ``p5`` section, a sibling of ``zone6``.
``P5State`` keeps unknown keys of ``p5`` in ``extra`` and writes them back, so an older app that loads a 0.7.0 file keeps
``zone7`` and ``p5state.py`` needs no edit. ``Learner`` owns one ``Zone7`` object (created empty before the push planner,
filled in place by ``load``) and calls ``sync_into(p5)`` before every ``export_state``: the key is written only when
something differs from the defaults, so a learner that never used P7 exports exactly what P6 exported.

``zone7`` is derived, droppable data (D43): a wrong container type is a ``StateError`` (the file is not one this app
wrote), any value problem resets the whole key to its defaults and is counted in ``reset_count``, so a damaged derived
section can never make ``state.json`` unloadable. In RAM the cell sets of the masks are frozensets.
"""
from __future__ import annotations

import copy
import math
import re

from .grids import rle_decode, rle_encode
from .types import DRIFT_STATES, MF_WHY, StateError

FORMAT_VERSION = 1
NC_DEFAULT = 70 * 40
COUNT_MAX = 99999
HIST_LEN = 6
PLACE_VARS = ("cf", "cd", "n", "gs", "cv", "ld")
RH_KEYS = ("mod_y", "mod_r", "ret_rm", "ret_kp")
OP_LEN = {"place": 4, "merge": 4, "modify": 4, "retire": 3, "radar": 3}      # approve, reject, [rename,] later

_ZID = re.compile(r"^[zl][0-9]{4,}$")
_SNZ = re.compile(r"^(ret-z[0-9]{4,}|radar_moved)$")
_MS = re.compile(r"^s[0-9]{4,}-[0-9]+#[0-9]+$")


class _Value(StateError):
    """A value problem inside an otherwise well-formed ``zone7`` (the load resets to defaults, D43)."""


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _val(msg: str):
    raise _Value(msg)


def _cont(msg: str):
    raise StateError(msg)


def default_entry() -> dict:
    return {"u": [], "lo": 0, "rt": 0, "n": 0, "lu": 0, "kp": 0, "mr": 0, "ms": "", "rr": 0, "ro": False, "rc": False,
            "rd": 0, "fz": False, "vs": "young"}


def default_rm() -> dict:
    return {"st": 0, "rev": 0, "open": False, "ign": 0, "tx": [0, 0, 0], "n": 0, "attn": False, "d0": 0, "qt": 0}


def default_ac() -> dict:
    return {"op": {k: [0] * n for k, n in OP_LEN.items()},
            "gh": {k: [[0] * HIST_LEN, [0] * HIST_LEN] for k in PLACE_VARS},
            "rh": {k: [0] * HIST_LEN for k in RH_KEYS}}


def _ac_is_zero(ac: dict) -> bool:
    return (all(v == 0 for lst in ac["op"].values() for v in lst)
            and all(v == 0 for pair in ac["gh"].values() for h in pair for v in h)
            and all(v == 0 for h in ac["rh"].values() for v in h))


# ---------------------------------------------------------------- parsing (validation and RAM form in one pass)
def _count_list(v, n: int, what: str) -> list:
    if not isinstance(v, list) or len(v) != n or not all(_isint(x) and 0 <= x <= 10 ** 9 for x in v):
        _val("bad " + what)
    return list(v)


def _parse_entry(e, what: str) -> dict:
    if not isinstance(e, dict):
        _val("bad " + what)
    out = default_entry()
    for k, v in e.items():
        if k not in out:
            out[k] = copy.deepcopy(v)                    # unknown keys of a newer writer are kept
    if "u" in e:
        u = e["u"]
        if not isinstance(u, list):
            _val("bad u")
        if u:
            if len(u) != 24 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                                       and 0.0 <= x <= 1.0 for x in u):
                _val("bad u")
            if abs(sum(u) - 1.0) > 0.05:
                _val("bad u sum")
        out["u"] = [float(x) for x in u]
    for k in ("lo", "rt", "mr", "rr"):
        if k in e:
            if not _isint(e[k]) or not 0 <= e[k] <= COUNT_MAX:
                _val("bad " + k)
            out[k] = e[k]
    for k in ("n", "lu", "kp", "rd"):
        if k in e:
            if not _isint(e[k]) or e[k] < 0:
                _val("bad " + k)
            out[k] = e[k]
    if "ms" in e:
        if not isinstance(e["ms"], str) or (e["ms"] != "" and not _MS.match(e["ms"])):
            _val("bad ms")
        out["ms"] = e["ms"]
    for k in ("ro", "rc", "fz"):
        if k in e:
            if not isinstance(e[k], bool):
                _val("bad " + k)
            out[k] = e[k]
    if "vs" in e:
        if e["vs"] not in DRIFT_STATES:
            _val("bad vs")
        out["vs"] = e["vs"]
    return out


def _parse_mask(m, nc: int) -> dict:
    if not isinstance(m, dict):
        _val("bad mf entry")
    zid = m.get("zid")
    if not isinstance(zid, str) or not _ZID.match(zid):
        _val("bad mf zid")
    runs = m.get("cells")
    if not isinstance(runs, list):
        _val("bad mf cells")
    for r in runs:
        if not (isinstance(r, list) and len(r) == 2 and _isint(r[0]) and _isint(r[1]) and r[0] >= 0 and r[1] >= 1
                and r[0] + r[1] <= nc):
            _val("bad mf cells")
    for k in ("cx", "cy"):
        v = m.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -33.0 <= v <= 33.0:
            _val("bad mf " + k)
    if m.get("why") not in MF_WHY:
        _val("bad mf why")
    if not _isint(m.get("day")) or m["day"] < 0:
        _val("bad mf day")
    n0 = m.get("n0", 0)
    if not _isint(n0) or n0 < 0:
        _val("bad mf n0")
    return {"zid": zid, "cells": rle_decode(runs), "cx": m["cx"], "cy": m["cy"], "day": m["day"], "n0": n0,
            "why": m["why"]}


def _parse_rm(d) -> dict:
    out = default_rm()
    for k in ("st", "rev", "ign", "n", "d0", "qt"):
        if k in d:
            if not _isint(d[k]) or d[k] < 0:
                _val("bad rm " + k)
            out[k] = d[k]
    for k in ("open", "attn"):
        if k in d:
            if not isinstance(d[k], bool):
                _val("bad rm " + k)
            out[k] = d[k]
    if "tx" in d:
        tx = d["tx"]
        if not isinstance(tx, list) or len(tx) != 3 or not all(_isint(x) for x in tx):
            _val("bad rm tx")
        out["tx"] = list(tx)
    return out


def _parse_ac(d) -> dict:
    out = default_ac()
    op = d.get("op", None)
    if op is not None:
        if not isinstance(op, dict):
            _val("bad ac.op")
        for k, v in op.items():
            if k in OP_LEN:
                out["op"][k] = _count_list(v, OP_LEN[k], "ac.op")
    gh = d.get("gh", None)
    if gh is not None:
        if not isinstance(gh, dict):
            _val("bad ac.gh")
        for k, v in gh.items():
            if k in PLACE_VARS:
                if not isinstance(v, list) or len(v) != 2:
                    _val("bad ac.gh")
                out["gh"][k] = [_count_list(v[0], HIST_LEN, "ac.gh"), _count_list(v[1], HIST_LEN, "ac.gh")]
    rh = d.get("rh", None)
    if rh is not None:
        if not isinstance(rh, dict):
            _val("bad ac.rh")
        for k, v in rh.items():
            if k in RH_KEYS:
                out["rh"][k] = _count_list(v, HIST_LEN, "ac.rh")
    return out


def _parse(d, nc: int) -> dict:
    """Validates a ``zone7`` value and returns its parts in RAM form. A wrong container type raises StateError, a value
    problem raises ``_Value`` (a StateError too)."""
    if not isinstance(d, dict):
        _cont("zone7 is not an object")
    z = d.get("z", {})
    rm = d.get("rm", {})
    snz = d.get("snz", {})
    ac = d.get("ac", {})
    mf = d.get("mf", [])
    if not isinstance(z, dict) or not isinstance(rm, dict) or not isinstance(snz, dict) or not isinstance(ac, dict):
        _cont("bad zone7 container")
    if not isinstance(mf, list):
        _cont("bad zone7.mf")
    v = d.get("v", FORMAT_VERSION)
    if not _isint(v) or v < 1:
        _val("bad zone7.v")
    out = {"z": {}, "mf": [], "rm": default_rm(), "snz": {}, "ac": default_ac(), "extra": {}}
    for k, x in d.items():
        if k not in ("v", "z", "mf", "rm", "snz", "ac"):
            out["extra"][k] = copy.deepcopy(x)
    for zid, e in z.items():
        if not isinstance(zid, str) or not _ZID.match(zid):
            _val("bad zone7.z key")
        out["z"][zid] = _parse_entry(e, "zone7.z entry")
    for m in mf:
        out["mf"].append(_parse_mask(m, nc))
    out["rm"] = _parse_rm(rm)
    for k, x in snz.items():
        if not isinstance(k, str) or not _SNZ.match(k) or not _isint(x) or x < 0:
            _val("bad zone7.snz")
        out["snz"][k] = x
    out["ac"] = _parse_ac(ac)
    return out


# ---------------------------------------------------------------- the container
class Zone7:
    """The ``zone7`` key in RAM (spec 5, A.5)."""

    def __init__(self) -> None:
        self.nc = NC_DEFAULT              # number of grid cells, set by ``load`` (the mask cells are checked against it)
        self.reset_count = 0              # value problems seen at load or export (Learner.counters()["zone7_reset"])
        self._defaults()

    def _defaults(self) -> None:
        self.z: dict = {}                 # zid (record or label) -> entry dict (default_entry keys)
        self.mf: list = []                # {zid, cells (frozenset), cx, cy, day, n0, why}
        self.rm: dict = default_rm()
        self.snz: dict = {}               # "ret-z0002" | "radar_moved" -> date ordinal of the run at the press
        self.ac: dict = default_ac()
        self.extra: dict = {}             # unknown keys of a newer writer: kept and written back

    # ------------------------------------------------------------ defaults and the export rule
    def is_default(self) -> bool:
        return (not self.z and not self.mf and self.rm == default_rm() and not self.snz and _ac_is_zero(self.ac)
                and not self.extra)

    def export(self) -> dict:
        """JSON-ready; ``{}`` when everything is at its defaults (the key is then absent from ``p5``)."""
        if self.is_default():
            return {}
        out = {"v": FORMAT_VERSION,
               "z": {zid: self._entry_out(e) for zid, e in sorted(self.z.items())},
               "mf": [{"zid": m["zid"], "cells": rle_encode(m["cells"]), "cx": m["cx"], "cy": m["cy"], "day": m["day"],
                       "n0": m["n0"], "why": m["why"]} for m in self.mf],
               "rm": {k: (list(v) if k == "tx" else v) for k, v in self.rm.items()},
               "snz": {k: v for k, v in sorted(self.snz.items())},
               "ac": copy.deepcopy(self.ac)}
        for k, v in self.extra.items():
            out[k] = copy.deepcopy(v)
        return out

    @staticmethod
    def _entry_out(e: dict) -> dict:
        out = {}
        for k, v in e.items():
            out[k] = list(v) if k == "u" else copy.deepcopy(v)
        return out

    # ------------------------------------------------------------ validation and load
    @staticmethod
    def validate(d, nc: int) -> None:
        """Raises StateError for a wrong container type or a value problem (spec 5.2)."""
        _parse(d, nc)

    def load(self, d, nc: int) -> None:
        """In place. ``None`` or ``{}`` give the defaults; a wrong container type raises StateError; a value problem
        gives the defaults and counts ``reset_count`` (D43)."""
        self.nc = nc
        if d is None or d == {}:
            self._defaults()
            return
        try:
            parts = _parse(d, nc)
        except _Value:
            self._defaults()
            self.reset_count += 1
            return
        self.z = parts["z"]
        self.mf = parts["mf"]
        self.rm = parts["rm"]
        self.snz = parts["snz"]
        self.ac = parts["ac"]
        self.extra = parts["extra"]

    def sync_into(self, p5) -> None:
        """Writes ``zone7`` into ``p5.extra`` or removes the key when it equals the defaults. The export is validated
        first: a failure (a bug) writes the defaults and counts ``reset_count``, so the app never writes what it would
        reject at its own next load."""
        d = self.export()
        if d:
            try:
                _parse(d, self.nc)
            except StateError:
                self._defaults()
                self.reset_count += 1
                d = {}
        if d:
            p5.extra["zone7"] = d
        else:
            p5.extra.pop("zone7", None)

    # ------------------------------------------------------------ entries
    def entry(self, zid: str, create: bool = False):
        e = self.z.get(zid)
        if e is None and create:
            e = self.z[zid] = default_entry()
        return e

    def frozen(self, zid: str) -> bool:
        e = self.z.get(zid)
        return bool(e is not None and e["fz"])

    def forget_zone(self, zid: str) -> None:
        """``ZoneForget`` (D44): restarts the flag and retire runs of that zone only."""
        e = self.z.get(zid)
        if e is not None:
            e["lo"] = 0
            e["rt"] = 0

    def drop(self, zid: str) -> None:
        """A zone was removed: its entry and its retire snooze go."""
        self.z.pop(zid, None)
        self.snz.pop("ret-" + zid, None)

    def prune(self, zone_ids) -> int:
        """Removes the entries (and retire snoozes) of zones that are gone; returns the number of zone ids dropped."""
        keep = set(zone_ids)
        gone = {zid for zid in self.z if zid not in keep}
        gone.update(k[4:] for k in self.snz if k.startswith("ret-") and k[4:] not in keep)
        for zid in gone:
            self.z.pop(zid, None)
            self.snz.pop("ret-" + zid, None)
        return len(gone)

    # ------------------------------------------------------------ small accessors used by the P7 modules
    def snoozed(self, key: str, run_ord: int) -> bool:
        """The marker is the date ordinal of the nightly run at the press; it holds until the next run (spec 5.1)."""
        v = self.snz.get(key)
        return v is not None and v == run_ord

    def clear_snoozes(self) -> None:
        self.snz.clear()

    def reset_all(self) -> None:
        """Everything back to the defaults (RAM only; ``reset_count`` is kept)."""
        self._defaults()
