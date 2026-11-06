"""Scripted people of the P6 replay jobs (P6 spec 11.1, 11.6 item 5).

The world's own agents cannot give exact truth for 'the target entered the zone at time x': they walk with 8 cm
jitter, sit in bursts and are not where a zone will be. From day ``SCRIPT_FROM_DAY`` (8) the stream therefore drops them
(``RunStream`` ``quiet_from_day``) and ``ScriptedPeople`` adds people whose paths are known exactly.

* A person is a list of **tracks**; a track is a list of **segments** ``[t0, x0, y0, t1, x1, y1]`` (seconds, metres) that
  share their end points. The track's waypoints are one per second, linearly interpolated inside a segment and noise
  free (``track_points``); ``replay.framegen.TargetFrames`` turns them into 10 Hz frames exactly the way it does for
  the world (linear interpolation to the next waypoint, the last waypoint held for 10 frames), adds the per-frame
  detection noise (``JobSpec.noise_m``) and the 2 % frame misses. The truth path is the same polyline
  (``replay.truth``).
* Nothing here may depend on what the learner learned: ``RunStream`` generates day d + 1 while the run is still in
  the first hours of day d, long before the night-7 zones exist. Every geometry and every time is therefore fixed from
  the job, the seed and the world's fixed spots (``replay.jobs.P6_SPOTS``); the learned cells are only read after the
  run, by ``replay.evaluate_p6``.
* Every person has its own record (``ScriptedPeople.log``, written into the run result): kind, spot, speed, angle,
  its tracks, its hidden spells (``spells``: the last visible second ``t``, the hidden length ``g`` in seconds, whether
  the person returns) and whether the LD2412 model reports it as present (``ld``).

Programs (``JobSpec.programs``): ``walkers`` (straight passes through each spot at the speeds of ``JobSpec.crossings``),
``sweep`` (stayers at offsets of 0-60 cm from the desk spot), ``hold`` (hidden spells of ``JobSpec.spells``), ``pow``
(stayers across the radar-off window and the fault and gap windows), ``door`` (moving passages at a scripted door, exits
that vanish inside the door-side zone, stationary controls), ``ld`` (visible and hidden cycles at the desk with an
unseen leave every hour), ``count`` (two and three people in one zone) and ``busy`` (visits all day long).
"""
from __future__ import annotations

import math

from . import jobs as J
from . import world as W

DAY = W.DAY
SPOTS = J.P6_SPOTS
WALK_HALF_M = 2.5                    # the walkers' half length (so that they cross any zone area of the replay's size)
WALK_CLEAR_M = 1.6                   # a walker's first and last waypoint are at least this far from every spot (see ``walk_through``)
WALK_EXTRA_MAX = 12                  # at most this many extra one-second steps at an end
PASS_GAP_S = 10                      # seconds between two scripted people in the same room (spec 11.1)
HOLD_VISIBLE_S = 30                  # P6-3: visible between two hidden spells
HOLD_REPS = 4                        # P6-3: repetitions of each hidden length
SWEEP_OFFSETS_CM = tuple(range(0, 61, 5))     # P6-2: stayers at 0, 5, ..., 60 cm from the spot
SWEEP_DIRS_DEG = (0, 90, 180, 270)
SWEEP_STAY_S = 120
LD_CYCLES_PER_HOUR = 10              # P6-9: 60 s visible, 240 s hidden
LD_VISIBLE_S, LD_HIDDEN_S = 60, 240
LD_OCC_TAIL_S = 8                    # the LD2412 model keeps reporting the person this long after the last visible second
DOOR = J.DOOR_SITE                  # the scripted door (not the world's D2: see _prog_door)
DOOR_ROOM_DIR = (-1.0, 0.0)         # from the door into the room
DOOR_EXIT_FROM_DAY = 12             # exits at the door start once the door suggestion exists (spec 11.6a: evaluation day 12)
DOOR_EXIT_DISTS = (3.5, 2.8, 2.1, 1.4, 0.7, 0.2)         # metres from the door at each second of an exit (0.7 m/s, a last step of 0.5 m)
DOOR_IN_DISTS = (0.2, 0.45, 0.7, 1.4, 2.1, 2.8, 3.5)      # and of a passage into the room (born by the door)


def at(d: int, h: int = 0, m: int = 0, s: int = 0) -> int:
    return d * DAY + h * 3600 + m * 60 + s


def seg_points(seg) -> list:
    """The one-per-second waypoints ``[(t, x, y), ...]`` of a segment, both ends included, rounded to 0.1 mm."""
    t0, x0, y0, t1, x1, y1 = seg
    n = int(t1) - int(t0)
    if n <= 0:
        return [(int(t0), round(x0, 4), round(y0, 4))]
    return [(int(t0) + k, round(x0 + (x1 - x0) * k / n, 4), round(y0 + (y1 - y0) * k / n, 4)) for k in range(n + 1)]


def track_points(track) -> list:
    """The waypoints of a track (consecutive segments sharing an end point; a shared second appears once)."""
    pts = []
    for seg in track:
        for p in seg_points(seg):
            if pts and p[0] == pts[-1][0]:
                continue
            pts.append(p)
    return pts


def stay(x: float, y: float, t0: int, t1: int) -> list:
    return [t0, x, y, t1, x, y]


def leg(p0, p1, speed: float, t0: int) -> list:
    """A straight segment from ``p0`` to ``p1`` at ``speed`` m/s, as a whole number of seconds (the speed is exact when
    the length is a multiple of it, else a little lower). Returns the segment."""
    dist = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    n = max(1, int(math.ceil(dist / speed - 1e-9)))
    return [t0, p0[0], p0[1], t0 + n, p1[0], p1[1]]


def walk_through(spot_xy, angle_deg: float, direction: int, speed: float, t0: int, clear_of=None) -> list:
    """A pass straight through ``spot_xy`` at the angle, in the direction (+1 or -1) and at exactly ``speed`` m/s: a whole
    number of one-second steps of the length ``speed``, at least ``2 x WALK_HALF_M`` long and centred on the spot.

    ``clear_of`` (a list of points, the world's spots): each end is then extended by whole steps until it is at least
    ``WALK_CLEAR_M`` from every one of them. A track is born with a speed of 0 (the tracker's filter has not seen it move) and
    its last waypoint is held for 10 frames (zero speed): both look like a person who stops. At the end of a pass that lies
    inside or beside a zone that is a person who enters it, an artefact of the scripted world, not a property of the zone
    rule (P6-15: the pass of 1.0 m/s that ends beside a dwell zone gave an enter and a leave)."""
    n = int(math.ceil(2 * WALK_HALF_M / speed - 1e-9))
    half = n * speed / 2.0
    ux, uy = math.cos(math.radians(angle_deg)) * direction, math.sin(math.radians(angle_deg)) * direction
    a = b = 0
    if clear_of:
        def clear(x, y):
            return all(math.hypot(x - px, y - py) >= WALK_CLEAR_M for px, py in clear_of)
        while not clear(spot_xy[0] - ux * (half + a * speed), spot_xy[1] - uy * (half + a * speed)) and a < WALK_EXTRA_MAX:
            a += 1
        while not clear(spot_xy[0] + ux * (half + b * speed), spot_xy[1] + uy * (half + b * speed)) and b < WALK_EXTRA_MAX:
            b += 1
    h0, h1 = half + a * speed, half + b * speed
    return [t0, spot_xy[0] - ux * h0, spot_xy[1] - uy * h0, t0 + n + a + b, spot_xy[0] + ux * h1, spot_xy[1] + uy * h1]


def seg_end(seg) -> int:
    return int(seg[3])


def person(kind: str, tracks: list, **meta) -> dict:
    """A scripted person: ``tracks`` (segments), ``ld`` (True: the LD2412 model reports it, from ``ld_occ``)."""
    rec = {"kind": kind, "tracks": tracks, "ld": True, "ld_occ": None, "spells": []}
    rec.update(meta)
    if rec["ld"] and rec["ld_occ"] is None:
        rec["ld_occ"] = [[min(s[0] for tr in tracks for s in tr), max(s[3] for tr in tracks for s in tr) + LD_OCC_TAIL_S]]
    return rec


def _rnd(rec: dict) -> dict:
    """A record with its segment numbers rounded for the result file."""
    rec["tracks"] = [[[int(s[0]), round(s[1], 4), round(s[2], 4), int(s[3]), round(s[4], 4), round(s[5], 4)]
                      for s in tr] for tr in rec["tracks"]]
    return rec


class ScriptedPeople:
    """The scripted people of one run: ``day(d, rng)`` for the stream, ``log`` for the result."""

    def __init__(self, job, seed: int):
        self.job = job
        self.seed = seed
        self.programs = tuple(job.programs)
        self.log = []
        self._n = 0

    def day(self, d: int, rng) -> tuple:
        """``(tracks, ld_occupancy)`` of day ``d``: framegen track dicts and LD2412 occupancy intervals (seconds)."""
        if d < J.SCRIPT_FROM_DAY or not self.programs:
            return [], []
        people = []
        for prog in self.programs:
            people += getattr(self, "_prog_" + prog)(d, rng)
        tracks, occ = [], []
        for p in people:
            self._n += 1
            p["id"] = "s%05d" % self._n
            p["d"] = d
            for tr in p["tracks"]:
                pts = track_points(tr)
                if pts:
                    tracks.append({"pts": pts, "kind": "human", "agent": "S"})
            if p["ld"]:
                occ += [(a, b) for a, b in p["ld_occ"]]
            self.log.append(_rnd(p))
        return tracks, occ

    # ------------------------------------------------------------------ walkers and the edge sweep (P6-1, 2, 15)
    def _prog_walkers(self, d, rng) -> list:
        items = []
        for spot, xy in SPOTS.items():
            for ang in (0, 45, 90, 135):
                for direction in (1, -1):
                    for v in self.job.crossings:
                        items.append((spot, xy, ang, direction, v))
        rng.shuffle(items)
        t = at(d, 8) + rng.randint(0, 30)
        out = []
        for spot, xy, ang, direction, v in items:
            seg = walk_through(xy, ang, direction, v, t, clear_of=tuple(SPOTS.values()))
            out.append(person("walker", [[seg]], spot=spot, speed=v, angle=ang, dir=direction, ld=False))
            t = seg_end(seg) + 1 + PASS_GAP_S
        self._walkers_end = t
        return out

    def _prog_sweep(self, d, rng) -> list:
        xy = SPOTS["desk"]
        t = max(getattr(self, "_walkers_end", 0), at(d, 9)) + rng.randint(0, 30)
        out = []
        for off in SWEEP_OFFSETS_CM:
            for ang in SWEEP_DIRS_DEG:
                x = xy[0] + off / 100.0 * math.cos(math.radians(ang))
                y = xy[1] + off / 100.0 * math.sin(math.radians(ang))
                out.append(person("sweep", [[stay(x, y, t, t + SWEEP_STAY_S)]], spot="desk", offset_cm=off, angle=ang))
                t += SWEEP_STAY_S + PASS_GAP_S
        return out

    # ------------------------------------------------------------------ hidden spells (P6-3, P6-7)
    def _prog_hold(self, d, rng) -> list:
        t = at(d, 8) + rng.randint(0, 20)
        out = []
        for spot in ("desk", "table", "counter", "sofaAB", "bed"):
            xy = SPOTS[spot]
            lengths = [g for sp, g in self.job.spells if sp == spot]
            if not lengths:
                continue
            order = list(lengths) * HOLD_REPS
            rng.shuffle(order)
            tracks, spells = [], []
            vis = t
            for g in order:
                tracks.append([stay(xy[0], xy[1], vis, vis + HOLD_VISIBLE_S)])
                spells.append({"t": vis + HOLD_VISIBLE_S, "g": g, "ret": True})
                vis += HOLD_VISIBLE_S + g
            tracks.append([stay(xy[0], xy[1], vis, vis + HOLD_VISIBLE_S)])
            spells.append({"t": vis + HOLD_VISIBLE_S, "g": None, "ret": False})
            end = vis + HOLD_VISIBLE_S
            out.append(person("stayer", tracks, spot=spot, spells=spells, ld_occ=[[t, end + LD_OCC_TAIL_S]]))
            t = end + J.P6_DEFAULT_DELAY_S[J.P6_SPOT_KIND[spot]] + 150
        return out

    # ------------------------------------------------------------------ power, resume and gaps (P6-4, P6-5)
    def _prog_pow(self, d, rng) -> list:
        desk, bed, table, counter = SPOTS["desk"], SPOTS["bed"], SPOTS["table"], SPOTS["counter"]
        out = [
            # the overnight stayers: inside the desk and bed zones from before 00:00 to after the resume at 06:00
            person("stayer", [[stay(desk[0], desk[1], at(d, 22), at(d + 1, 8))]], spot="desk", note="overnight"),
            person("stayer", [[stay(bed[0] + 0.25, bed[1], at(d, 22, 30), at(d + 1, 8))]], spot="bed", note="overnight"),
            # visible across the scripted gap of 10:00:20 (day 12: and across the 10 min loss window at 10:00)
            person("stayer", [[stay(desk[0], desk[1], at(d, 9, 58), at(d, 10, 12))]], spot="desk", note="gap_visible"),
            # visible across the node reboot of day 9 at 14:00
            person("stayer", [[stay(desk[0], desk[1], at(d, 13, 58), at(d, 14, 3))]], spot="desk", note="fault_visible"),
        ]
        # hidden across the scripted gap of 11:00:20: visible 10:59:30-10:59:50, never returns (the counter's hold)
        t0 = at(d, 10, 59, 30)
        out.append(person("stayer", [[stay(counter[0], counter[1], t0, t0 + 20)]], spot="counter", note="gap_hidden",
                          spells=[{"t": t0 + 20, "g": None, "ret": False}]))
        out.append(person("stayer", [[stay(table[0], table[1], at(d, 15), at(d, 15, 2))]], spot="table", note="plain"))
        return out

    # ------------------------------------------------------------------ the door (P6-8)
    def _prog_door(self, d, rng) -> list:
        """Passages that build the shadow ``entry_zone`` suggestion at a scripted door, exits that vanish moving inside the door-side
        zone, movers that vanish inside the desk zone (the control: no door there) and stationary controls.

        The door is NOT the world's D2 (3.0, 0.9). The learner's door rule (``extraction._doors``) accepts a region of births and
        deaths only where it reaches beyond the walking envelope, which extends about 1 m past every 'walk' sample (a step of more
        than 0.3 m) and keeps the world's own evidence of days 0-7: a measured fact. With the world's walkers ending at D2 the
        region around D2 never reaches out of the envelope, and no entry_zone is ever made there. The scripted door lies beyond the
        east wall, where nothing else walks. Its evidence is BIRTHS only (``door_in``: born by the door, two 0.25 m steps, then
        walking in at 0.7 m/s) on every day from day 8: a person who ends a walk by the door puts a walk sample there and closes
        the envelope round the door (an exit cannot be a 'moving death' without a step of more than 0.3 m in its last 3 s). The
        exits therefore start on day ``DOOR_EXIT_FROM_DAY``, after the suggestion exists (the nightly run of day 11 is the third
        night of the streak); a suggestion stays in state shadow for 7 nights without fresh evidence. An exit is a plain walk at
        0.7 m/s up to 0.2 m inside the door: the last waypoint is held for 0.9 s, so a path that slows down at the end would not be
        a 'moving death' (its speed over the last 3 s must be above 0.35 m/s, held second included)."""
        ux, uy = DOOR_ROOM_DIR

        def along(dist: float) -> tuple:
            return DOOR[0] + ux * dist, DOOR[1] + uy * dist

        def path(dists: list, t0: int) -> list:
            return [[t0 + i, along(a)[0], along(a)[1], t0 + i + 1, along(b)[0], along(b)[1]]
                    for i, (a, b) in enumerate(zip(dists, dists[1:]))]

        out = []
        if d == J.REFLECTOR_LABEL_DAY:          # the door-side label stayer (the 'Mark spot here' press of DoorScript), 10:00-11:30
            a, b = J.REFLECTOR_LABEL_S           # noise free on purpose: a jittering stayer (7 cm) makes 'walk' samples (a step of more
            dx, dy = J.DOOR_PROBE                # than 0.3 m) round the door and closes the envelope the door region has to leave
            out.append(person("stayer", [[stay(dx, dy, d * DAY + a, d * DAY + b)]], spot="door", note="label"))
        for k in range(24):                     # 24 passages into the room a day, 11:00 to 12:55 (the blurred birth rate must pass
            t = at(d, 11, 5 * k) + rng.randint(0, 20)       # 0.3 per 24 covered hours: 20 births in all, a night before the streak)
            out.append(person("door_in", [path(list(DOOR_IN_DISTS), t)], spot="door", speed=0.7, ld=False))
        if d >= DOOR_EXIT_FROM_DAY:
            for k in range(8):                  # exits that vanish moving inside the door-side zone (14:00, every 10 minutes)
                t = at(d, 14, 10 * k) + rng.randint(0, 15)
                segs = path(list(DOOR_EXIT_DISTS), t)
                out.append(person("door_exit", [segs], spot="door", speed=0.7, ld=False,
                                  spells=[{"t": seg_end(segs[-1]), "g": None, "ret": False}]))
        desk = SPOTS["desk"]                    # movers that end inside the desk zone, moving: never a door
        for k in range(4):
            t = at(d, 15, 10 * k) + rng.randint(0, 15)
            start = (desk[0] - 3.0, desk[1])
            seg = leg(start, desk, 0.7, t)
            out.append(person("mover_vanish", [[seg]], spot="desk", speed=0.7, ld=False,
                              spells=[{"t": seg_end(seg), "g": None, "ret": False}]))
        dx, dy = J.DOOR_PROBE                   # stationary controls at the door-side zone: visible 40 s, then gone (16:00)
        for k in range(4):
            t = at(d, 16, 10 * k) + rng.randint(0, 15)
            out.append(person("door_sitter", [[stay(dx, dy, t, t + 40)]], spot="door",
                              spells=[{"t": t + 40, "g": None, "ret": False}]))
        return out

    # ------------------------------------------------------------------ LD2412 evidence and early clear (P6-9)
    def _prog_ld(self, d, rng) -> list:
        xy = SPOTS["desk"]
        t0 = at(d, 8) + rng.randint(0, 20)
        out = []
        for h in range(14):
            base = t0 + h * 3600
            tracks, spells = [], []
            t = base
            for _ in range(LD_CYCLES_PER_HOUR):
                tracks.append([stay(xy[0], xy[1], t, t + LD_VISIBLE_S)])
                spells.append({"t": t + LD_VISIBLE_S, "g": LD_HIDDEN_S, "ret": True})
                t += LD_VISIBLE_S + LD_HIDDEN_S
            tracks.append([stay(xy[0], xy[1], t, t + LD_VISIBLE_S)])
            end = t + LD_VISIBLE_S
            spells.append({"t": end, "g": None, "ret": False, "unseen": True})      # leaves unseen: LD2412 says absent
            out.append(person("stayer", tracks, spot="desk", spells=spells, ld_occ=[[base, end + LD_OCC_TAIL_S]]))
        return out

    # ------------------------------------------------------------------ several people in one zone (P6-14)
    def _prog_count(self, d, rng) -> list:
        bed = SPOTS["bed"]
        ang = math.radians(90)
        far = (bed[0] + math.cos(ang) * -2.5, bed[1] + math.sin(ang) * -2.5)       # 2.5 m from the bed, in the room

        def visit(dx: float, t_in: int, t_leave: int, vanish: bool = False, label: str = "") -> dict:
            """Walks in from 2.5 m away at 0.7 m/s, stays at ``dx`` metres beside the bed spot until ``t_leave``, then
            walks out (a leave at the zone's edge) or, with ``vanish``, the track simply ends inside the zone."""
            p_in = (bed[0] + dx, bed[1])
            walk_in = leg(far, p_in, 0.7, t_in)
            s_in = seg_end(walk_in)
            s_out = max(t_leave, s_in + 5)
            segs = [walk_in, stay(p_in[0], p_in[1], s_in, s_out)]
            spells = []
            if vanish:
                spells = [{"t": s_out, "g": None, "ret": False}]
            else:
                segs.append(leg(p_in, far, 0.7, s_out))
            return person("count", [segs], spot="bed", spells=spells, note=label)

        t = at(d, 10) + rng.randint(0, 20)
        out = []
        # case A: two people; the second leaves first while the first stays (count 2 -> 1, no leave event)
        out.append(visit(0.15, t, t + 1500, label="A1"))
        out.append(visit(-0.15, t + 300, t + 900, label="A2"))
        # case B: three people at once (count_max)
        t2 = t + 1800
        out.append(visit(0.0, t2, t2 + 600, label="B1"))
        out.append(visit(0.25, t2 + 60, t2 + 540, label="B2"))
        out.append(visit(-0.25, t2 + 120, t2 + 480, label="B3"))
        # case C: one vanishes inside while the other stays (D11); the stayer walks out at the end
        t3 = t2 + 900
        out.append(visit(0.1, t3, t3 + 900, label="C1"))
        out.append(visit(-0.1, t3 + 30, t3 + 120, vanish=True, label="C2"))
        return out

    # ------------------------------------------------------------------ visits all day (P6-11)
    def _prog_busy(self, d, rng) -> list:
        out = []
        t = at(d, 8) + rng.randint(0, 30)
        end_t = at(d, 22)
        k = 0
        names = ("desk", "table", "counter", "sofaAB", "bed")
        while t < end_t:
            spot = names[k % len(names)]
            k += 1
            xy = SPOTS[spot]
            ang = math.radians(90 if k % 2 else 0)
            far_in = (xy[0] - math.cos(ang) * 2.5, xy[1] - math.sin(ang) * 2.5)
            far_out = (xy[0] + math.cos(ang) * 2.5, xy[1] + math.sin(ang) * 2.5)
            walk_in = leg(far_in, xy, 0.7, t)
            s_in = seg_end(walk_in)
            s_out = s_in + 180
            walk_out = leg(xy, far_out, 1.0, s_out)
            out.append(person("visit", [[walk_in, stay(xy[0], xy[1], s_in, s_out), walk_out]], spot=spot))
            t = seg_end(walk_out) + 1 + 30
        return out
