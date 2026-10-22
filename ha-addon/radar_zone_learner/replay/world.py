"""Port of the project's simulator world (P3 spec 11.2).

Source: the option-A learner simulator ``zone_sim_optA_dw.py`` (this project's own code):
lines 20-31 (grid helpers), 74-187 (room, spots, world config, ``lu``, ``spot_pos``, ``plan_day``),
191-313 (``World``), 316-354 (``union``, ``overlap``, ``_hm``, ``night_window_off``) and 645-663 (``label``).

The random-number order is kept exactly: one ``random.Random(seed)`` is shared by ``plan_day(rng, d, cfg)``
and ``World(rng, cfg).day_tracks(d, S)``, called per day in that order, so seed ``s`` here gives the same
people, sessions and ghosts as seed ``s`` of the published simulator runs. ``tests/test_world_port.py``
checks this against the original.

The only addition: ``day_tracks`` also returns ``sessions``, a list of
``(agent, place, mode, t0, t1, x, y)`` recorded where a session's true position is drawn (before the settle
shift of ``t0``). It makes no extra random-number calls.
"""
from __future__ import annotations

import math
from collections import defaultdict

DAY = 86400
CELL = 0.2
GX0 = -7.0
GY0 = 0.0
NX = 70
NY = 40
NC = NX * NY


def cell_of(x, y):
    i = int(math.floor((x - GX0) / CELL))
    j = int(math.floor((y - GY0) / CELL))
    return j * NX + i if 0 <= i < NX and 0 <= j < NY else -1


def cell_xy(c):
    return (GX0 + (c % NX + 0.5) * CELL, GY0 + (c // NX + 0.5) * CELL)


# ------------------------------------------------------------------ world (assumed models)
ROOM = (-3.0, 3.0, 0.3, 5.5)
DOORS = {'D1': (-1.5, 5.5), 'D2': (3.0, 0.9)}
SPOT = {'desk': (2.55, 2.2), 'sofaA': (-2.5, 3.2), 'sofaB': (-2.5, 3.8), 'table': (0.3, 4.4),
        'counter': (1.9, 5.15), 'armchair': (-1.0, 1.4), 'bed': (-2.3, 4.9),
        'guest': (0.2, 2.4), 'visitor': (-0.4, 2.0), 'dogbed': (1.6, 1.0), 'dogsofa': (-2.35, 3.5)}
REFLECTOR = (2.0, 0.8)
INSIDE_GHOST = (1.5, 3.0)        # multipath image of the desk sitter (metal cabinet), inside the room
WALL_L, WALL_R = ROOM[0], ROOM[1]

# The world part of the simulator's CFG (SIM:84-85); the learner keys are not used here.
WORLD_CFG = dict(Vm=15, Hm=90, gw=0.25, gs=0.0, gi=0.0, pet=0, bed=0, move=0.0, move_day=35, wfh=1, nw='', nl=0)


def make_cfg(**overrides):
    """A world config: WORLD_CFG with the given keys replaced (unknown keys are an error)."""
    cfg = dict(WORLD_CFG)
    for k, v in overrides.items():
        if k not in cfg:
            raise KeyError(k)
        cfg[k] = v
    return cfg


def lu(rng, a, b):
    return math.exp(rng.uniform(math.log(a), math.log(b)))


def spot_pos(name, d, cfg):
    p = SPOT[name]
    if name in ('sofaA', 'sofaB', 'dogsofa') and cfg['move'] and d >= cfg['move_day']:
        if cfg['move'] < 2.0:
            return (p[0] + cfg['move'], p[1])           # shifted 1.1 m to the right
        return (p[0] + 2.2, p[1] - 1.9)                 # large move: other side of the room (~2.9 m)
    return p


def plan_day(rng, d, cfg):
    """Sessions per agent ``S`` (agent -> [[t0, t1, place, mode(, flag)], ...]) and home-empty intervals."""
    wd = d % 7
    weekend = wd >= 5
    at = lambda h, m=0: d * DAY + int(h * 3600 + m * 60)   # noqa: E731
    jit = lambda s: int(rng.gauss(0, s))                     # noqa: E731
    S = defaultdict(list)          # agent -> [t0, t1, place, mode]
    away = defaultdict(list)
    if cfg['bed']:
        wake = at(6, 50) + jit(900) if not weekend else at(8, 30) + jit(1800)
        S['P1'].append([d * DAY, wake, 'bed', 'sleep', 'cont_in'])
    if wd in (0, 2, 4) and cfg['wfh']:
        for (a, b) in ((at(9) + jit(600), at(12)), (at(13) + jit(600), at(17) + jit(900))):
            t = a
            while t < b - 600:
                e = min(b, t + int(lu(rng, 1800, 7200)))
                S['P1'].append([t, e, 'desk', 'sit'])
                t = e
                if t >= b - 600:
                    break
                if rng.random() < 0.5:
                    c = int(lu(rng, 60, 300))
                    S['P1'].append([t + 15, t + 15 + c, 'counter', 'stand'])
                    t += c + 30
                else:
                    t += rng.randint(180, 600)
    elif wd < 5:
        away['P1'].append((at(8) + jit(600), at(17, 30) + jit(1200)))
    if not weekend:
        away['P2'].append((at(8) + jit(600), at(17, 30) + jit(1200)))
    else:
        if rng.random() < 0.6:
            a, b = at(10) + jit(900), at(12) + jit(900)
            away['P1'].append((a, b))
            if rng.random() < 0.8:
                away['P2'].append((a, b))
        if rng.random() < 0.6:
            s = at(14) + jit(1800)
            S['P1'].append([s, s + int(lu(rng, 3600, 7200)), 'sofaA', 'sit'])
        if rng.random() < 0.7:
            s = at(12, 30) + jit(600)
            e = s + rng.randint(1800, 2700)
            S['P1'].append([s, e, 'table', 'sit'])
            S['P2'].append([s, e, 'table', 'sit'])
    if rng.random() < 0.9:
        s = at(18, 30) + jit(600)
        e = s + rng.randint(1800, 2700)
        S['P1'].append([s, e, 'table', 'sit'])
        S['P2'].append([s, e, 'table', 'sit'])
    if rng.random() < 4 / 7:
        s = at(20, 30) + jit(900)
        S['P1'].append([s, s + int(lu(rng, 1800, 5400)), 'desk', 'sit'])
        if rng.random() < 0.5:
            s2 = at(22) + jit(600)
            S['P1'].append([s2, s2 + rng.randint(1800, 3600), 'sofaA', 'sit'])
    else:
        s = at(19, 30) + jit(900)
        S['P1'].append([s, s + int(lu(rng, 3600, 10800)), 'sofaA', 'sit'])
    if rng.random() < 6 / 7:
        s = at(19, 30) + jit(900)
        S['P2'].append([s, s + int(lu(rng, 3600, 10800)), 'sofaB', 'sit'])
    if rng.random() < 2 / 7:
        s = (at(15, 30) if weekend else at(18)) + jit(600)
        S['P2'].append([s, s + rng.randint(1800, 3600), 'armchair', 'sit'])
    for _ in range(rng.randint(4, 8)):
        h = rng.choice([7.2, 17.9, 19.2, 21.0, 21.8, 22.5] + ([10.5, 13.5, 16.0] if weekend else []))
        s = at(h) + jit(900)
        S['P2'].append([s, s + int(lu(rng, 60, 300)), 'counter', 'stand'])
    if cfg['bed']:
        s = at(23, 15) + jit(1200)
        S['P1'].append([s, (d + 1) * DAY, 'bed', 'sleep', 'cont_out'])
    if d == 20:
        S['G'].append([at(19), at(22), 'guest', 'sit'])
    if wd == 5:
        S['V'].append([at(15), at(17), 'visitor', 'sit'])
    # remove sessions overlapping away; resolve overlaps per agent
    for ag in list(S):
        lst = []
        for s in sorted(S[ag]):
            if any(a < s[1] and s[0] < b for a, b in away.get(ag, [])):
                continue
            if lst and s[0] < lst[-1][1] + 60:
                s[0] = lst[-1][1] + 60
            if s[1] - s[0] >= 30 or (len(s) > 4):
                if s[1] > s[0]:
                    lst.append(s)
        S[ag] = lst
    # home empty = both humans away (guests / visitors make it non-empty)
    he = []
    for a1, b1 in away.get('P1', []):
        for a2, b2 in away.get('P2', []):
            a, b = max(a1, a2), min(b1, b2)
            if b > a:
                he.append((a, b))
    he = [(a, b) for a, b in he if not any(s[0] < b and a < s[1] for ag in ('G', 'V') for s in S.get(ag, []))]
    if cfg['pet']:
        for a, b in he:
            t = a + rng.randint(300, 1800)
            if b - a > 7200 and rng.random() < 0.7:
                e = min(b - 600, t + int(lu(rng, 3600, 7200)))
                S['dog'].append([t, e, 'dogsofa', 'pet'])
                t = e + 120
            if b - t > 1800:
                S['dog'].append([t, b - 300, 'dogbed', 'pet'])
        if rng.random() < 0.7:
            s = at(20) + jit(1200)
            S['dog'].append([s, s + int(lu(rng, 2400, 7200)), 'dogbed', 'pet'])
        S['dog'].sort()
    return S, he


# ------------------------------------------------------------------ radar observation model
class World:
    def __init__(self, rng, cfg):
        self.rng = rng
        self.cfg = cfg

    def jit(self, p, s=0.07):
        return (p[0] + self.rng.gauss(0, s), p[1] + self.rng.gauss(0, s))

    def walk(self, p0, p1, t0):
        rng = self.rng
        v = rng.uniform(0.7, 1.2)
        d = math.dist(p0, p1)
        n = max(1, int(d / v))
        off = rng.uniform(-0.3, 0.3)
        nx, ny = (-(p1[1] - p0[1]) / (d or 1), (p1[0] - p0[0]) / (d or 1))
        pts = []
        for k in range(n + 1):
            f = k / n
            b = math.sin(math.pi * f) * off
            pts.append((t0 + k, p0[0] + (p1[0] - p0[0]) * f + nx * b + rng.gauss(0, 0.08),
                        p0[1] + (p1[1] - p0[1]) * f + ny * b + rng.gauss(0, 0.08)))
        return pts

    def day_tracks(self, d, S):
        """Tracks (dict pts=[(t, x, y)], kind, agent), occupancy intervals, motion times and sessions."""
        rng, cfg = self.rng, self.cfg
        tracks = []
        occ = []
        motion = []
        sessions = []
        for ag, sess in S.items():
            prev_end = None
            prev_pos = None
            cur = None
            for k, s in enumerate(sess):
                t0, t1, place, mode = s[:4]
                flag = s[4] if len(s) > 4 else ''
                pos = spot_pos(place, d, cfg)
                sd = {'sit': 0.08, 'stand': 0.15, 'sleep': 0.15, 'pet': 0.15}[mode]
                p = (pos[0] + rng.gauss(0, sd), pos[1] + rng.gauss(0, sd))
                sessions.append((ag, place, mode, t0, t1, p[0], p[1]))      # the port's addition
                Vm, Hm = {'sit': (cfg['Vm'], cfg['Hm']), 'stand': (30, 10), 'sleep': (5, 600), 'pet': (10, 120)}[mode]
                kind = 'pet' if mode == 'pet' else 'human'
                # arrival
                if flag == 'cont_in':
                    cur = None
                    arr_t = t0
                else:
                    if prev_end is not None and t0 - prev_end <= 120 and prev_pos is not None:
                        src = prev_pos
                    else:
                        src = rng.choice(list(DOORS.values()))
                    w = self.walk(src, p, t0 - max(1, int(math.dist(src, p) / 0.9)))
                    arr_t = w[0][0]
                    motion.append(arr_t)
                    if cur is not None and prev_end is not None and t0 - prev_end <= 120:
                        cur['pts'].extend(w)
                    else:
                        cur = {'pts': list(w), 'kind': kind, 'agent': ag}
                        tracks.append(cur)
                    settle = rng.randint(5, 20)
                    cur['pts'].extend((t0 + j, *self.jit(p)) for j in range(1, min(settle, t1 - t0)))
                    t0 = t0 + settle
                occ.append((arr_t, t1 + 8, ag))
                # dwell bursts
                t = t0
                last_vis_end = t0 if flag != 'cont_in' else -10 ** 9
                while t < t1:
                    h = 1 + int(rng.expovariate(1 / Hm))
                    t += h
                    if t >= t1:
                        break
                    v = min(1 + int(rng.expovariate(1 / Vm)), t1 - t)
                    burst = [(t + j, *self.jit(p)) for j in range(v)]
                    if h <= 2 and cur is not None and cur['pts'] and t - cur['pts'][-1][0] <= 2:
                        cur['pts'].extend(burst)
                    else:
                        cur = {'pts': burst, 'kind': kind, 'agent': ag}
                        tracks.append(cur)
                    # seated mirror ghost and in-room multipath ghost (person-induced)
                    if mode in ('sit', 'sleep') and (p[0] > WALL_R - 0.8 or p[0] < WALL_L + 0.8) and rng.random() < cfg['gs']:
                        wall = WALL_R if p[0] > 0 else WALL_L
                        tracks.append({'pts': [(tt, 2 * wall - x + rng.gauss(0, 0.1), y + rng.gauss(0, 0.1)) for tt, x, y in burst],
                                       'kind': 'mirror', 'agent': ag})
                    if place == 'desk' and rng.random() < cfg['gi']:
                        tracks.append({'pts': [(tt, INSIDE_GHOST[0] + rng.gauss(0, 0.1), INSIDE_GHOST[1] + rng.gauss(0, 0.1)) for tt, _, _ in burst],
                                       'kind': 'inghost', 'agent': ag})
                    t += v
                    last_vis_end = t
                # departure
                nxt = sess[k + 1] if k + 1 < len(sess) else None
                if flag == 'cont_out':
                    prev_end = None
                    prev_pos = None
                    cur = None
                    continue
                motion.append(t1)
                if nxt is not None and nxt[0] - t1 <= 120 and (len(nxt) <= 4 or nxt[4] != 'cont_in'):
                    prev_end = t1
                    prev_pos = p
                    if not (t1 - last_vis_end <= 2 and cur is not None):
                        cur = {'pts': [(t1, *self.jit(p))], 'kind': kind, 'agent': ag}
                        tracks.append(cur)
                    continue
                dst = rng.choice(list(DOORS.values()))
                w = self.walk(p, dst, t1)
                if t1 - last_vis_end <= 2 and cur is not None:
                    cur['pts'].extend(w)
                else:
                    cur = {'pts': w, 'kind': kind, 'agent': ag}
                    tracks.append(cur)
                occ[-1] = (occ[-1][0], w[-1][0] + 1, ag)
                prev_end = t1
                prev_pos = None
                cur = None
        # walking mirror ghosts
        extra = []
        for tr in tracks:
            if tr['kind'] not in ('human', 'pet'):
                continue
            g = []
            pts = tr['pts']
            for k in range(1, len(pts)):
                t, x, y = pts[k]
                if math.hypot(x - pts[k - 1][1], y - pts[k - 1][2]) > 0.3 and (x > WALL_R - 0.8 or x < WALL_L + 0.8) and rng.random() < cfg['gw']:
                    wall = WALL_R if x > 0 else WALL_L
                    gp = (t, 2 * wall - x + rng.gauss(0, 0.1), y + rng.gauss(0, 0.1))
                    if g and t - g[-1][-1][0] <= 2:
                        g[-1].append(gp)
                    else:
                        g.append([gp])
            for gg in g:
                extra.append({'pts': gg, 'kind': 'mirror', 'agent': tr['agent']})
        tracks.extend(extra)
        # static reflector (fan/curtain): byte-identical-ish
        if rng.random() < 0.5:
            st = d * DAY + int(rng.uniform(0, 20) * 3600)
            du = int(rng.uniform(1, 3) * 3600)
            t = st
            while t < st + du:
                v = rng.randint(60, 600)
                tracks.append({'pts': [(t + j, REFLECTOR[0] + rng.gauss(0, 0.003), REFLECTOR[1] + rng.gauss(0, 0.003)) for j in range(v)],
                               'kind': 'refl', 'agent': 'refl'})
                t += v + rng.randint(3, 30)
        for tr in tracks:
            tr['pts'].sort()
        return tracks, occ, motion, sessions


def union(iv):
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def overlap(iv, a, b):
    s = 0
    for x, y in iv:
        if y <= a:
            continue
        if x >= b:
            break
        s += min(b, y) - max(a, x)
    return s


def _hm(x):
    h, m = x.split(':')
    return int(h) * 3600 + int(m) * 60


def night_window_off(d, cfg):
    """Radar-off intervals of day ``d`` (simulated seconds) for the night window ``cfg['nw']`` (SIM:340-354).

    The simulator also returns event counters; the port returns the list of intervals only. No random numbers.
    """
    if not cfg['nw']:
        return []
    a, b = (_hm(x) for x in cfg['nw'].split('-'))
    lo = d * DAY
    off = []
    nl = int(cfg['nl'])
    if a > b:     # crosses midnight: [a, 24h) belongs to night d, [0, b) to night d-1
        if d - 1 >= nl and d - 1 >= 0:
            off.append((lo, lo + b))
        if d >= nl:
            off.append((lo + a, lo + DAY))
    else:         # whole window after midnight: it is night d-1 (the evening before)
        if d - 1 >= nl and d - 1 >= 0:
            off.append((lo + a, lo + b))
    return off


# ------------------------------------------------------------------ truth labelling
def label(x, y, d, cfg):
    pts = {}
    for k in ['desk', 'sofaA', 'sofaB', 'table', 'counter', 'armchair'] + (['bed'] if cfg['bed'] else []):
        pts['TP:' + k] = spot_pos(k, d, cfg)
    pa, pb = spot_pos('sofaA', d, cfg), spot_pos('sofaB', d, cfg)
    pts['TP:sofaAB'] = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
    if cfg['move'] and d >= cfg['move_day']:
        pts['FP:old_sofa'] = ((SPOT['sofaA'][0] + SPOT['sofaB'][0]) / 2, (SPOT['sofaA'][1] + SPOT['sofaB'][1]) / 2)
    pts.update({'FP:guest': SPOT['guest'], 'FP:visitor': SPOT['visitor'], 'FP:reflector': REFLECTOR, 'FP:inghost': INSIDE_GHOST,
                'FP:mirror_desk': (2 * WALL_R - SPOT['desk'][0], SPOT['desk'][1]),
                'FP:mirror_sofa': (2 * WALL_L - SPOT['sofaA'][0], 3.5)})
    if cfg['pet']:
        pts['FP:dogbed'] = SPOT['dogbed']
        pts['FP:dogsofa'] = spot_pos('dogsofa', d, cfg)
    if cfg['bed']:
        pts['FP:mirror_bed'] = (2 * WALL_L - SPOT['bed'][0], SPOT['bed'][1])
    best = min(pts.items(), key=lambda kv: math.hypot(x - kv[1][0], y - kv[1][1]))
    if math.hypot(x - best[1][0], y - best[1][1]) < 0.6:
        return best[0]
    return 'FP:other(%.1f,%.1f)' % (x, y)


class RunWorld:
    """The world of one run, generated day by day in the simulator's random-number order.

    ``day(d)`` generates every day up to ``d`` (in order) and returns day ``d``. Each generated day is passed
    to the ``on_day`` callbacks once, in order, so consumers (frame generator, truth, labels) see every day
    exactly once without the world being generated twice.
    """

    def __init__(self, seed, cfg):
        import random
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.world = World(self.rng, cfg)
        self.next_day = 0
        self.days = {}
        self.on_day = []

    def ensure(self, d):
        while self.next_day <= d:
            dd = self.next_day
            S, he = plan_day(self.rng, dd, self.cfg)
            tracks, occ, motion, sessions = self.world.day_tracks(dd, S)
            rec = WorldDay(dd, S, he, tracks, occ, motion, sessions)
            self.days[dd] = rec
            self.next_day += 1
            for cb in self.on_day:
                cb(rec)

    def day(self, d):
        self.ensure(d)
        return self.days[d]

    def forget_before(self, d):
        """Drop the stored days before ``d`` (consumers already have them)."""
        for k in [k for k in self.days if k < d]:
            del self.days[k]


class WorldDay:
    __slots__ = ("d", "S", "he", "tracks", "occ", "motion", "sessions")

    def __init__(self, d, S, he, tracks, occ, motion, sessions):
        self.d = d
        self.S = S
        self.he = he
        self.tracks = tracks
        self.occ = occ
        self.motion = motion
        self.sessions = sessions
