"""The actors of the P7 replay jobs (P7 spec 8.4; add-on 0.7.0: drift, moves, retire, radar moved, tuning).

A P7 job has the P5 setup person (``SetupUser``: the night-7 approvals and the bed mark; not for ``p7bednl``, where the learner finds
the bed by Night learning and ``NightLearningUser`` turns that on) and one ``P7User``, the owner at the review. ``P7User`` looks at the item
list a minute after every nightly run (the P5 actor's look) and

* **before the event** (nights up to ``until``: the day before the sofa moves for the move jobs, the night of the rotation for
  ``p7reset``, every night for the controls, the beds, the retire, rotation, shift, guest and tuning jobs) approves every open plain
  place item, except one whose suggestion's ``sim_label`` is ``FP:guest`` or ``FP:visitor`` (these are only recorded as *offered*: the
  check P7-7 reads the item lists, not the answers);
* **after the event** answers only the P7 items of its job: ``mod-`` Approve in the move jobs and in ``p7reset`` (every modify item),
  ``ret-`` Approve in ``p7ret``, ``radar_moved`` Approve in ``p7reset``. It never answers ``radar_moved`` in ``p7rot``, ``p7rot3``,
  ``p7rot6``, ``p7shift`` and ``p7shift5`` (so the latch is observable) and it never approves a plain place after the event (so a place
  that should have been a modify is *seen* as offered, not hidden by an approval);
* retries a refused answer (``zone_busy``, a stale revision) at the next look: it answers every open item of its kind at every look.

The setup person works first: ``P7User`` waits for it (its ``done``) before it approves places, at most until night ``SETUP_WAIT_NIGHT``,
so the first review of a P7 job is the one of ``p5setup`` and not the owner's tap on a place that the setup user would have made.

After a Approve of ``radar_moved`` the actor reads ``Learner.reset_summary()`` five seconds of stream time later (the radars are off at
that look under night-off, so no new frame refills the maps): ``{night, grids_nonzero, candidates, stays}`` (check P7-6 ``p7reset``).
``replay.driver.Run._p7_night`` reads what the actor did since the last night record (``take_answers``).
"""
from __future__ import annotations

from radar_zone_learner.core.types import AnswerIgnored, ReviewAnswer

from .framegen import BASE_MS
from .jobs import P7_EVENT_DAY
from .p4_scenarios import PLACE_TYPES, NightLearningUser
from .p5_scenarios import GuestsUser, P5Actor, SetupUser

FALSE_POSITIVE_LABELS = ("FP:guest", "FP:visitor")
MOVE_SCRIPTS = ("p7mv11", "p7mv29", "p7mv11e", "p7mv29e", "p7mvback")
NEVER_ANSWER_RADAR = ("p7rot", "p7rot3", "p7rot6", "p7shift", "p7shift5")
SETUP_WAIT_NIGHT = 10                  # the setup user is given until this night; then the owner approves places anyway
RESET_PROBE_MS = 5_000                 # the reset is read this long after the Approve of ``radar_moved``
MAX_ANSWERS_PER_LOOK = 40


def plan_of(job) -> dict:
    """What the ``P7User`` of a job does: ``until`` (the last night with plain-place approvals; None = every night) and the P7
    item types it approves (``mod``, ``ret``, ``radar``)."""
    s = job.script
    until = None
    if s in MOVE_SCRIPTS:
        until = int(job.move_day) - 1
    elif s == "p7reset":
        until = P7_EVENT_DAY
    return {"script": s, "until": until, "mod": s in MOVE_SCRIPTS or s == "p7reset", "ret": s == "p7ret",
            "radar": s == "p7reset"}


class P7User(P5Actor):
    """The owner at the review of a P7 job (see the module docstring)."""

    name = "p7user"

    def __init__(self, plan: dict):
        super().__init__()
        self.plan = plan
        self.setup = None                 # the job's SetupUser, if it has one
        self.sim = {}                     # suggestion id -> its record of the last night (sim_label, cx, cy)
        self.pending = []                 # answers since the last night record (``take_answers``)
        self.answers = []                 # every answer, in order
        self.offered = {}                 # item id -> {"night", "id", "type", "label"}: items of a guest or visitor spot
        self.resets = []                  # {"night", "grids_nonzero", "candidates", "stays", ...} after each Approve of radar_moved
        self.looks = 0
        self.refused = 0

    def attach(self, run) -> None:
        super().attach(run)
        self.setup = next((a for a in run.actors if getattr(a, "name", "") == "setup_user"), None)

    # ---- the nights
    def nightly(self, rec: dict) -> None:
        self.sim = {s["id"]: s for s in rec.get("suggestions", [])}

    def take_answers(self) -> list:
        """The answers taken since the last call (the next night record gets them)."""
        out, self.pending = self.pending, []
        return out

    def label_of(self, item) -> str:
        sid = item.data.get("sid") if item.type in ("modify_zone", "merge_into") else item.id
        s = self.sim.get(sid) or {}
        return s.get("sim_label") or ""

    # ---- the look
    def _places_ok(self) -> bool:
        night = self.night
        if night is None:
            return False
        u = self.plan["until"]
        if u is not None and night > u:
            return False
        return self.setup is None or bool(getattr(self.setup, "done", False)) or night >= SETUP_WAIT_NIGHT

    def _note_offered(self) -> None:
        for it in self.items:
            lab = self.label_of(it)
            if lab in FALSE_POSITIVE_LABELS and it.id not in self.offered:
                self.offered[it.id] = {"night": self.night, "id": it.id, "type": it.type, "label": lab}

    def _next(self, tried: set, places_ok: bool):
        for it in self.items:
            key = (it.id, it.rev)
            if key in tried:
                continue
            if it.tier == 2 and it.type in PLACE_TYPES:
                if places_ok and self.label_of(it) not in FALSE_POSITIVE_LABELS:
                    return it
            elif it.type == "modify_zone" and self.plan["mod"]:
                return it
            elif it.type == "retire_zone" and self.plan["ret"]:
                return it
            elif it.type == "radar_moved" and self.plan["radar"]:
                return it
        return None

    def look(self, feed, t: int) -> None:
        if self.night is None:
            return
        self.looks += 1
        self._note_offered()
        tried = set()
        places_ok = self._places_ok()
        for _ in range(MAX_ANSWERS_PER_LOOK):
            it = self._next(tried, places_ok)
            if it is None:
                break
            tried.add((it.id, it.rev))
            self._answer(feed, t, it, "approve")

    def _answer(self, feed, t: int, it, op: str) -> None:
        outs = self.send(feed, ReviewAnswer(it.id, op, it.rev, False, self.ms(t)))
        ign = [o.reason for o in outs if isinstance(o, AnswerIgnored)]
        rec = {"night": self.night, "item": it.id, "type": it.type, "op": op, "rev": it.rev,
               "outcome": ("ignored:" + ign[0]) if ign else "applied"}
        self.answers.append(rec)
        self.pending.append(rec)
        if ign:
            self.refused += 1
        elif it.type == "radar_moved":
            self.at(t + RESET_PROBE_MS, self._reset_probe)

    def _reset_probe(self, feed, t: int) -> None:
        try:
            s = dict(self.learner().reset_summary())
        except Exception:
            s = {}
        s["night"] = self.night
        s["t"] = t
        self.resets.append(s)

    def summary(self) -> dict:
        return {"plan": dict(self.plan), "answers": self.answers, "offered": sorted(self.offered.values(), key=lambda d: (d["night"], d["id"])),
                "resets": self.resets, "looks": self.looks, "refused": self.refused,
                "setup_done": bool(getattr(self.setup, "done", False)) if self.setup is not None else None, "done": self.done}


def make_actors(job) -> list:
    """The actors of a P7 job (``job.script`` starts with ``p7``): the P5 setup person (the Night learning user for ``p7bednl``), the
    pause switch of ``p7guestp`` and the ``P7User``."""
    s = job.script
    out = [NightLearningUser() if s == "p7bednl" else SetupUser()]
    if s == "p7guestp":
        out.append(GuestsUser(True))
    out.append(P7User(plan_of(job)))
    return out
