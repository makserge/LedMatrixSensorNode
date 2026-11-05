"""``PushPlanner``: when a push request is due, the acknowledgements and the retry rule (P5 spec 6.2, A.10). Owner: CORE.

The learner owns the 'sent once' and 'at most one a day' rules in its persisted state (``p5.push``); it never sends
anything. A request is a retained fact (``PushRequest``) that the blueprint turns into a notification and acknowledges
per phone. The planner never reads a text, a time or a name: ``PushRequest`` has none.

Rules (spec 6.2), in this order, evaluated only when ``(count, announced set, local date, minute, seq, acked)`` changed:

1. setup push: once, when the setup review is ready, at least ``push_min_items`` items are open and the local time is
   inside the push window (08:00-21:00);
2. daily push: at most one a day, only for an ``id#rev`` that was never announced (the bed item always counts as
   revision 0: its revision moves with [Later] and with the end of the snooze, and a [Later] must not become a daily
   reminder, D41);
3. every request sets ``last_day = today``;
4. every evaluation emits ``PushRequest`` when it differs from the last emitted one (also when only ``count``,
   ``acked`` or ``acks`` changed), but only once a request exists (``seq > 0``): before the first request the
   blueprint has nothing to act on, and a learner with no P5 data must emit exactly what P4 emitted;
5. ``PushAck(seq, target)`` marks a phone as served;
6. retry: a request nobody acknowledged is issued again once a day inside the window, without an upper limit. The
   acknowledgement means 'the blueprint's notify call returned', not 'the phone has it': a failed remote delivery is
   only logged by Home Assistant, so the rule covers a notify call that raises (no such notify service) and a
   blueprint installed after the request.
"""
from __future__ import annotations

import re
from datetime import date

from .params import P5Params
from .types import ITEM_BED, ITEM_RADAR_MOVED, PUSH_TARGET_RE, PushRequest, SaveRequested

_TARGET = re.compile(PUSH_TARGET_RE)
SIG_MAX = 40
ACKS_MAX = 8


def _sig(item_id: str, rev: int) -> str:
    """The announced-set entry of an item. The bed item's revision moves with [Later] and with the end of its snooze,
    so it is always counted as revision 0: a returned bed item is never announced again (D41)."""
    return "%s#%d" % (item_id, 0 if item_id == ITEM_BED else rev)


class PushPlanner:
    def __init__(self, p5, p5params: P5Params, zone7=None) -> None:
        self.p5 = p5                         # the P5State: its ``push`` dict is replaced on a load, so it is read live
        self.params = p5params
        self.zone7 = zone7                   # P7: the Zone7 object (attention state); None = the P5 construction
        self._eval_key = None                # RAM: the inputs of the last rule evaluation
        self._last_emitted = PushRequest(0, 0, "none", 0, None, None, ())

    # ------------------------------------------------------------ the request as it is now
    def request(self, items) -> PushRequest:
        """The current request: the stored counters and head with ``count`` = the open items now."""
        ps = self.p5.push
        z7 = self.zone7
        attention = bool(z7 is not None and z7.rm["attn"] and any(i.id == ITEM_RADAR_MOVED for i in items))
        return PushRequest(ps["seq"], ps["acked"], ps["kind"], len(items), ps["head_id"], ps["head_rev"],
                           tuple(ps["acks"]), attention)

    def state(self) -> dict:
        return dict(self.p5.push)

    # ------------------------------------------------------------ acknowledgements (spec 6.2 rule 5)
    def ack(self, seq, target: str = "") -> bool:
        """The blueprint acknowledged request ``seq`` for a phone. Ignored unless ``seq`` is the current one. True when
        something changed (the learner then saves: an acknowledged phone must not be notified again after a crash)."""
        ps = self.p5.push
        if isinstance(seq, bool) or not isinstance(seq, int) or seq != ps["seq"] or seq <= 0:
            return False
        changed = ps["acked"] != seq
        ps["acked"] = seq
        if isinstance(target, str) and _TARGET.match(target) and target not in ps["acks"]:
            ps["acks"].append(target)
            del ps["acks"][:-ACKS_MAX]
            changed = True
        if changed:
            self._eval_key = None
        return changed

    # ------------------------------------------------------------ nightly housekeeping
    def prune_sig(self, exists) -> bool:
        """Drops announced ids that no longer exist (``exists(id)`` says so); True if any went."""
        ps = self.p5.push
        keep = [x for x in ps["sig"] if exists(x.rsplit("#", 1)[0])]
        changed = len(keep) != len(ps["sig"])
        ps["sig"] = keep
        return changed

    # ------------------------------------------------------------ the rules
    def step(self, L, items, now_ms: int, outs: list) -> None:
        ps = self.p5.push
        p = self.params
        lt = L.tm.local(now_ms)
        count = len(items)
        sig_now = tuple(sorted(_sig(i.id, i.rev) for i in items))
        minute = lt.sec_of_day // 60
        key = (count, sig_now, lt.date_ord, minute, ps["seq"], ps["acked"], ps["setup_sent"])
        if key != self._eval_key:
            self._eval_key = key
            self._evaluate(L, items, sig_now, lt.date_ord, minute, outs)
        if ps["seq"] > 0:
            req = self.request(items)
            if req != self._last_emitted:
                self._last_emitted = req
                outs.append(req)

    def _evaluate(self, L, items, sig_now: tuple, date_ord: int, minute: int, outs: list) -> None:
        ps = self.p5.push
        p = self.params
        count = len(items)
        today = date.fromordinal(date_ord).isoformat()
        in_window = p.push_after_min <= minute < p.push_before_min
        head = next((i for i in items if i.tier == 2 and i.type != "retire_zone"), None)    # P7: a removal is never a head
        z7 = self.zone7 if L.p7on else None
        # P7 (spec 6.5): a radar_moved item that was never announced makes the daily request an attention request
        attn_new = z7 is not None and any(i.id == ITEM_RADAR_MOVED and _sig(i.id, i.rev) not in ps["sig"]
                                          for i in items)
        radar_open = z7 is not None and any(i.id == ITEM_RADAR_MOVED for i in items)
        kind = None
        announce = False
        if not ps["setup_sent"]:
            if L.review.ready(L) and count >= p.push_min_items and in_window:
                kind = "setup"
                announce = True
                ps["setup_sent"] = True
        else:
            new = any(s not in ps["sig"] for s in sig_now)
            if count >= 1 and in_window and ps["last_day"] != today and new:
                kind = "daily"
                announce = True
            elif ps["kind"] in ("setup", "daily") and ps["acked"] < ps["seq"] and count >= 1 and in_window \
                    and ps["last_day"] != today:
                kind = ps["kind"]                      # the retry (rule 6): kind and sig stay
        if kind is None:
            return
        if z7 is not None:
            if kind == "setup":
                z7.rm["attn"] = False
            elif announce:                                  # the daily rule: attention is part of it, not a rule after it
                z7.rm["attn"] = bool(attn_new)
                if attn_new:
                    head = None                            # an attention request has no head: the push says what is wrong
            elif z7.rm["attn"] and radar_open:
                head = None                                # the retry keeps the attention request as it was
            else:
                z7.rm["attn"] = False
        ps["seq"] += 1
        ps["kind"] = kind
        ps["last_day"] = today
        ps["acks"] = []
        ps["head_id"] = head.id if head is not None else None
        ps["head_rev"] = head.rev if head is not None else None
        if announce:
            for i in items:
                s = _sig(i.id, i.rev)
                if s not in ps["sig"]:
                    ps["sig"].append(s)
            # an item that was snoozed with [Later] before this push counts as announced as well: when it returns it is
            # listed in the review but never pushed (D41, 'including an item snoozed before any push mentioned it')
            for key in list(L.p5.later):
                kind, _, rev = L.review.resolve(L, key)
                if kind in ("place", "merge", "modify"):
                    s = "%s#%d" % (key, rev)
                    if s not in ps["sig"]:
                        ps["sig"].append(s)
            if z7 is not None:                              # the same for the P7 snoozes (retire, radar_moved)
                for key in list(z7.snz):
                    kind, _, rev = L.review.resolve(L, key)
                    if kind in ("retire", "radar"):
                        s = "%s#%d" % (key, rev)
                        if s not in ps["sig"]:
                            ps["sig"].append(s)
            if L.review.later.get(ITEM_BED) is not None and _sig(ITEM_BED, 0) not in ps["sig"]:
                ps["sig"].append(_sig(ITEM_BED, 0))             # a bed item snoozed before this push: same rule
            del ps["sig"][:-SIG_MAX]
        outs.append(SaveRequested("push"))
