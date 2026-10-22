import copy
import importlib.util
import json
import random
import unittest
from pathlib import Path

from radar_zone_learner.core.frames import parse_frames
from radar_zone_learner.core.types import FramesBatch, PayloadError

CHECKER = Path(__file__).resolve().parents[3] / "tools" / "frames_checker.py"

EX_BATCH = b'{"v":1,"b":"9f3c21aa","t":81234567,"s":1234560,"n":5,"a0":480,"a1":40,"mt":1,"p2":1,"pw":3,"sm":"A",' \
           b'"f":[[0,480,0,-15,1193,55],[4,40,0,-761,5446,58]]}'
EX_EMPTY = b'{"v":1,"b":"9f3c21aa","t":81239567,"s":1234565,"n":50,"a0":4990,"a1":40,"mt":1,"p2":0,"pw":3,"sm":"A"}'
EX_SILENT = b'{"v":1,"b":"9f3c21aa","t":81244567,"s":1234615,"n":0,"mt":1,"p2":0,"pw":3,"sm":"A"}'
EX_OFF = b'{"v":1,"b":"9f3c21aa","t":81274567,"s":1234615,"n":0,"pw":0,"sm":"N"}'


def _load_checker():
    if not CHECKER.is_file():
        return None
    spec = importlib.util.spec_from_file_location("rzl_test_frames_checker", str(CHECKER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rand_valid(rng):
    n = rng.choice([0, 0, 1, 2, 3, 5, 5, 8, 20, 50])
    obj = {"v": 1, "b": "%08x" % rng.getrandbits(32), "t": rng.randint(0, 10 ** 9),
           "s": rng.randint(0, 2 ** 32 - 1 - n), "n": n}
    if n > 0:
        a1 = rng.randint(0, 100)
        a0 = a1 if n == 1 else a1 + rng.randint(4 * (n - 1), 100 * (n - 1) + 50)
        obj["a0"] = a0
        obj["a1"] = a1
    if rng.random() < 0.8:
        obj["mt"] = rng.choice([0, 1])
    if rng.random() < 0.8:
        obj["p2"] = rng.choice([0, 1])
    obj["pw"] = rng.randint(0, 3)
    obj["sm"] = rng.choice("ANFXS")
    if n > 0 and rng.random() < 0.8:
        ks = sorted(rng.sample(range(n), min(n, rng.randint(1, 20))))
        f = []
        for k in ks:
            age = obj["a0"] if n == 1 else int(round(obj["a0"] - (obj["a0"] - obj["a1"]) * k / (n - 1)))
            e = [k, age]
            for slot in sorted(rng.sample([0, 1, 2], rng.randint(1, 3))):
                if rng.random() < 0.05:
                    e += [slot, 0, 0, rng.randint(-50, 50)]
                else:
                    e += [slot, rng.randint(-8000, 8000), rng.randint(0, 9000), rng.randint(-300, 300)]
            f.append(e)
        obj["f"] = f
    return obj


BAD_VALUES = [True, False, None, 1.5, "1", [], {}, -1, 2 ** 32, 2 ** 40]


def mutate(rng, obj):
    o = copy.deepcopy(obj)
    r = rng.randrange(9)
    keys = list(o)
    if r == 0 and keys:
        del o[rng.choice(keys)]
    elif r == 1:
        o[rng.choice(keys)] = rng.choice(BAD_VALUES)
    elif r == 2:
        o["zz"] = 1
    elif r == 3 and "f" in o:
        e = rng.choice(o["f"])
        e[rng.randrange(len(e))] = rng.choice(BAD_VALUES + [40000, -40000, 3])
    elif r == 4 and "f" in o and len(o["f"]) > 1:
        i = rng.randrange(len(o["f"]) - 1)
        o["f"][i], o["f"][i + 1] = o["f"][i + 1], o["f"][i]
    elif r == 5 and "f" in o:
        o["f"].append(list(o["f"][-1]))
    elif r == 6 and "f" in o:
        e = rng.choice(o["f"])
        if len(e) >= 10:
            e[2], e[6] = e[6], e[2]
        else:
            e.append(1)
    elif r == 7 and o.get("n", 0) > 0:
        o["a1"] = o["a0"] + rng.randint(1, 5)
    elif r == 8:
        o["n"] = max(0, o["n"] + rng.choice([-1, 1]))
    return o


class FramesTest(unittest.TestCase):
    def test_p1_examples(self):
        b = parse_frames(EX_BATCH)
        self.assertIsInstance(b, FramesBatch)
        self.assertEqual((b.boot, b.t_ms, b.s, b.n, b.a0, b.a1, b.mt, b.p2, b.pw, b.sm),
                         ("9f3c21aa", 81234567, 1234560, 5, 480, 40, 1, 1, 3, "A"))
        self.assertEqual(len(b.frames), 2)
        self.assertEqual((b.frames[0].k, b.frames[0].age_ms), (0, 480))
        t = b.frames[1].targets[0]
        self.assertEqual((t.slot, t.x_mm, t.y_mm, t.v_cms), (0, -761, 5446, 58))
        e = parse_frames(EX_EMPTY)
        self.assertEqual((e.n, e.a0, e.a1, e.frames), (50, 4990, 40, ()))
        s = parse_frames(EX_SILENT)
        self.assertEqual((s.n, s.a0, s.a1, s.mt, s.p2), (0, None, None, 1, 0))
        o = parse_frames(EX_OFF)
        self.assertEqual((o.n, o.pw, o.sm, o.mt, o.p2), (0, 0, "N", None, None))

    def bad(self, obj):
        with self.assertRaises(PayloadError):
            parse_frames(json.dumps(obj).encode())

    def test_errors(self):
        good = json.loads(EX_BATCH)
        for key in ("v", "b", "t", "s", "n", "pw", "sm"):
            o = dict(good)
            del o[key]
            self.bad(o)
        self.bad(dict(good, pw=True))
        self.bad(dict(good, mt=True))
        self.bad(dict(good, v=True))
        self.bad(dict(good, f=[[4, 40, 0, 1, 1, 1], [0, 480, 0, 1, 1, 1]]))           # k not increasing
        self.bad(dict(good, f=[[0, 480, 0, 1, 1, 1], [2, 500, 0, 1, 1, 1]]))          # age outside a1..a0
        self.bad(dict(good, f=[[0, 480, 0, 1, 1, 1], [2, 20, 0, 1, 1, 1]]))           # age outside a1..a0
        self.bad(dict(good, n=1, a0=40, a1=30, f=[[0, 40, 0, 1, 1, 1]]))              # n 1, a0 != a1
        self.bad(dict(good, f=[[0, 480, 1, 1, 1, 1, 0, 2, 2, 2]]))                    # slot order
        self.bad(dict(good, f=[[0, 480, 3, 1, 1, 1]]))                                # slot > 2
        self.bad(dict(good, n=50, a0=4990, a1=40,
                      f=[[k, 4990 - 100 * k, 0, 1, 1, 1] for k in range(21)]))         # 21 entries
        self.bad(dict(good, f=[[0, 480, 0, 40000, 1, 1]]))                            # range
        self.bad(dict(good, b="9F3C21AA"))
        self.bad(dict(good, sm="Z"))
        self.bad(dict(good, unknown=1))
        self.bad(dict(json.loads(EX_SILENT), a0=1, a1=1))
        self.bad([1, 2])
        with self.assertRaises(PayloadError):
            parse_frames(b"\xff\xfe{}")
        with self.assertRaises(PayloadError):
            parse_frames(b"{not json")
        big = dict(good, f=[[0, 480] + [0, 1, 1, 1]])
        payload = json.dumps(big).encode()
        payload = payload[:-1] + b" " * (4097 - len(payload)) + b"}"
        self.assertEqual(len(payload), 4097)
        with self.assertRaises(PayloadError):
            parse_frames(payload)
        try:
            parse_frames(b'{"v":1,"b":"secret","t":0}')
        except PayloadError as exc:
            self.assertNotIn("secret", str(exc))

    def test_range_limits(self):
        # values no node can send (the broker has no ACLs): beyond t 2**48, n 5000, a0 120 s
        good = json.loads(EX_BATCH)
        nof = {k: v for k, v in good.items() if k != "f"}
        self.assertEqual(parse_frames(json.dumps(dict(good, t=1 << 48)).encode()).t_ms, 1 << 48)
        self.bad(dict(good, t=(1 << 48) + 1))
        self.bad(dict(good, t=10 ** 30))
        b = parse_frames(json.dumps(dict(nof, n=5000, a0=120000, a1=40)).encode())
        self.assertEqual((b.n, b.a0), (5000, 120000))
        self.bad(dict(nof, n=5001))
        self.bad(dict(nof, n=4294967295))
        self.bad(dict(nof, a0=120001))
        self.bad(dict(nof, a0=4000000000, a1=10))

    def test_zero_target_dropped(self):
        b = parse_frames(json.dumps(dict(json.loads(EX_BATCH),
                                         f=[[0, 480, 0, 0, 0, 12, 1, 5, 6, 7], [4, 40, 2, 0, 0, 0]])).encode())
        self.assertEqual(len(b.frames), 2)
        self.assertEqual([(t.slot, t.x_mm, t.y_mm) for t in b.frames[0].targets], [(1, 5, 6)])
        self.assertEqual(b.frames[1].targets, ())
        b = parse_frames(json.dumps(dict(json.loads(EX_BATCH), f=[[0, 480, 0, 0, 7, 0]])).encode())
        self.assertEqual(b.frames[0].targets[0].y_mm, 7)

    def test_agrees_with_p1_validator(self):
        checker = _load_checker()
        if checker is None:
            self.skipTest("tools/frames_checker.py not found next to the add-on")
        rng = random.Random(20261022)
        n_ok = n_bad = 0
        for i in range(300):
            obj = rand_valid(rng)
            if i % 2:
                obj = mutate(rng, obj)
            payload = json.dumps(obj, separators=(",", ":")).encode()
            if len(payload) > 4096:
                continue
            errs = checker.validate_frames(obj, phase="any")
            try:
                parse_frames(payload)
                ok = True
            except PayloadError:
                ok = False
            self.assertEqual(ok, not errs, (obj, errs))
            n_ok += ok
            n_bad += not ok
        self.assertGreater(n_ok, 100)
        self.assertGreater(n_bad, 50)


if __name__ == "__main__":
    unittest.main()
