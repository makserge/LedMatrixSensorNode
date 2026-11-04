"""P5 'Advanced': the silent entries, their order, ids and limits, the verbs and their preconditions, and the rule
that the list exists only once the setup review is ready (spec 2.10, 3.7, §11.3 `test_p5_advanced`)."""
import random
import unittest
from datetime import date

from radar_zone_learner.core import Learner
from radar_zone_learner.core.types import (AdvancedChanged, AdvancedEntry, DebugMapChanged, MarkResult, ReviewAnswer,
                                           SaveRequested, StatusChanged)

from .test_extraction import BASE_MS, DAY
from .test_labels import BED, press, setup
from .test_learner import sit_fn
from .test_p5_answers import ans, ignored, saves
from .test_p5_items import BED_ITEM, POS, evidence, ids, places
from .test_review import NW_ITEM, add_place, ready_learner, with_proposal

T8 = BASE_MS + 8 * DAY * 1000


def entries(L):
    return [e.id for e in L.advanced()]


def advanced(L, item, **kw):
    return ans(L, item, "advanced", **kw)


def add_reflection(L, pos, sid):
    s = add_place(L, pos, sid=sid, conf=0.5, evidence=evidence())
    s["type"] = "reflection"
    s["kind"] = "reflection"
    L.review.touch()
    return s


class OrderTest(unittest.TestCase):
    def build(self):
        L = places(4)
        with_proposal(L)
        add_reflection(L, POS[8], "s0004-2")
        ans(L, "s0005-1", "approve", name="Desk")                      # a zone
        ans(L, "s0005-2", "reject")                                    # a place rejection
        ans(L, BED_ITEM, "reject")                                     # [No bed in view]
        ans(L, NW_ITEM, "reject")                                      # Deny
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.0, "y": 7.9, "name": "Plant",
                             "kind": "manual", "cells": frozenset()})
        return L

    def test_order_and_ids(self):
        L = self.build()
        self.assertEqual(entries(L), [
            "adv:undo:nw", "adv:undo:bed", "adv:undo:s0005-2",             # suppressions, newest first
            "adv:real:s0004-2",                                           # a reflection not yet answered
            "adv:rename:z0001", "adv:remove:z0001",                       # zone records
            "adv:rename:l0001", "adv:remove:l0001"])                      # labels

    def test_the_fields(self):
        L = self.build()
        by = {e.id: e for e in L.advanced()}
        nw = by["adv:undo:nw"]
        self.assertEqual((nw.verb, nw.kind, nw.target, nw.until), ("undo", "night_window", "nw",
                                                                   date.fromordinal(L.review._today(L) + 30).isoformat()))
        self.assertEqual((by["adv:undo:bed"].kind, by["adv:undo:bed"].target), ("bed", "bed"))
        rej = by["adv:undo:s0005-2"]
        self.assertEqual((rej.kind, rej.target, rej.tag, rej.name), ("place", "s0005-2", L.p5.tags["s0005-2"], ""))
        real = by["adv:real:s0004-2"]
        self.assertEqual((real.verb, real.kind, real.tag), ("real", "reflection", L.p5.tags["s0004-2"]))
        z = by["adv:rename:z0001"]
        self.assertEqual((z.kind, z.name, z.rev, z.target), ("zone", "Desk", 0, "z0001"))
        self.assertEqual(by["adv:remove:z0001"].name, "Desk")
        lb = by["adv:remove:l0001"]
        self.assertEqual((lb.kind, lb.name, lb.rev), ("label", "Plant", 0))
        for e in L.advanced():
            self.assertIsInstance(e, AdvancedEntry)
            hash(e)

    def test_newest_first_among_place_rejections(self):
        L = places(3)
        ans(L, "s0005-1", "reject")
        ans(L, "s0005-3", "reject")
        ans(L, "s0005-2", "reject")
        self.assertEqual(entries(L), ["adv:undo:s0005-2", "adv:undo:s0005-3", "adv:undo:s0005-1"])

    def test_an_expired_denial_is_not_listed(self):
        L = ready_learner()
        with_proposal(L)
        ans(L, NW_ITEM, "reject")
        self.assertEqual(entries(L), ["adv:undo:nw"])
        L.review.suppressions[0]["until_ord"] = L.review._today(L)
        self.assertEqual(entries(L), [])

    def test_a_reflection_that_a_marker_matches_leaves_the_list(self):
        L = ready_learner()
        r = add_reflection(L, POS[0], "s0004-2")
        add_reflection(L, POS[1], "s0004-3")
        r["state"] = "stale"
        self.assertEqual(entries(L), ["adv:real:s0004-3"])                 # only a shadow reflection
        r["state"] = "shadow"
        self.assertEqual(entries(L), ["adv:real:s0004-2", "adv:real:s0004-3"])
        L.p5.real.append({"ref": "s0004-2", "cx": r["cx"], "cy": r["cy"], "cells": frozenset(r["cells"]), "night": 5})
        self.assertEqual(entries(L), ["adv:real:s0004-3"])


class LimitTest(unittest.TestCase):
    def test_limits_of_20_rejections_10_reflections_and_60_entries(self):
        L = ready_learner()
        for k in range(30):
            L.review.suppressions.append({"type": "place", "ref": "s%04d-1" % (100 + k), "night": 3})
        for k in range(15):
            add_reflection(L, (-6.5 + 0.8 * k, 7.5), "s%04d-2" % (200 + k))
        for k in range(40):
            L.review.zones.append({"zid": "z%04d" % (k + 1), "name": "Z%d" % k, "kind": "dwell", "src": "approve",
                                   "ref": None, "cx": 0.0, "cy": 0.0, "cells": frozenset(), "night": 1, "created_s": 0,
                                   "poly_cm": [], "rev": 0, "upd": 1, "srev": 0, "also": []})
        L.review.touch()
        e = L.advanced()
        self.assertEqual(len(e), 60)                                       # 20 + 10 + the first 30 of the 80 zone entries
        self.assertEqual(len([x for x in e if x.verb == "undo"]), 20)
        self.assertEqual(len([x for x in e if x.verb == "real"]), 10)
        self.assertEqual(e[-1].id, "adv:remove:z0015")
        self.assertEqual(len({x.id for x in e}), 60)

    def test_ids_match_the_shell_grammar(self):
        import re
        from radar_zone_learner.core.types import ADV_ID_RE
        L = places(2)
        ans(L, "s0005-1", "reject")
        ans(L, "s0005-2", "approve", name="Desk")
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.0, "y": 7.9, "name": "P",
                             "kind": "manual", "cells": frozenset()})
        self.assertTrue(all(re.match(ADV_ID_RE, x) for x in entries(L)))


class ReadyTest(unittest.TestCase):
    def test_no_entries_and_no_change_before_the_setup_review_is_ready(self):
        L = places(1)
        L.night = 3
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.0, "y": 7.9, "name": "P",
                             "kind": "manual", "cells": frozenset()})
        L.review.touch()
        self.assertEqual(L.advanced(), ())
        self.assertEqual([o for o in L.tick(T8) if isinstance(o, AdvancedChanged)], [])
        L.night = 7
        L.review.touch()
        self.assertEqual(entries(L), ["adv:rename:l0001", "adv:remove:l0001"])        # listed from night 7
        ch = [o for o in L.tick(T8 + 1000) if isinstance(o, AdvancedChanged)]
        self.assertEqual(len(ch), 1)
        self.assertEqual([e.id for e in ch[0].entries], ["adv:rename:l0001", "adv:remove:l0001"])

    def test_a_mark_at_night_zero_emits_exactly_the_p3_outputs(self):
        L, f = setup()
        rng = random.Random(3)
        f.targets_until(40000, sit_fn(rng, BED))
        outs = press(L, f)
        self.assertEqual([type(o) for o in outs], [MarkResult, StatusChanged, DebugMapChanged, SaveRequested])
        self.assertEqual(L.advanced(), ())
        L.night = 7
        L.review.touch()
        self.assertEqual(entries(L), ["adv:rename:l0001", "adv:remove:l0001"])        # the label shows from night 7 on


class ChangeSignalTest(unittest.TestCase):
    def test_advanced_changed_on_change_only(self):
        L = places(2)
        self.assertEqual([o for o in L.tick(T8) if isinstance(o, AdvancedChanged)], [])    # nothing to list
        outs = ans(L, "s0005-1", "reject")
        ch = [o for o in outs if isinstance(o, AdvancedChanged)]
        self.assertEqual([[e.id for e in c.entries] for c in ch], [["adv:undo:s0005-1"]])
        for k in range(1, 4):
            self.assertEqual([o for o in L.tick(T8 + k * 1000) if isinstance(o, AdvancedChanged)], [])
        outs = ans(L, "adv:undo:s0005-1", "advanced")
        ch = [o for o in outs if isinstance(o, AdvancedChanged)]
        self.assertEqual([c.entries for c in ch], [()])                    # the list became empty: said once
        self.assertEqual([o for o in L.tick(T8 + 9000) if isinstance(o, AdvancedChanged)], [])

    def test_a_status_change_follows_so_the_count_is_published(self):
        L = places(2)
        outs = ans(L, "s0005-1", "reject")
        st = [o for o in outs if isinstance(o, StatusChanged)][-1]
        self.assertEqual(st.attributes["review"]["advanced"], 1)


class VerbTest(unittest.TestCase):
    def test_undo_bed(self):
        L = ready_learner()
        ans(L, BED_ITEM, "reject")
        rev = L.review.bed_rev
        outs = advanced(L, "adv:undo:bed")
        self.assertEqual((ignored(outs), saves(outs)), ([], ["review"]))
        self.assertEqual((L.review.bed_state(L), L.review.bed_rev), (("open", None), rev + 1))
        self.assertEqual(ids(L), [BED_ITEM])
        self.assertEqual(ignored(advanced(L, "adv:undo:bed")), [("adv:undo:bed", "advanced", "not_pending")])

    def test_undo_nw_equals_the_resuggest_button(self):
        L = ready_learner()
        with_proposal(L)
        ans(L, NW_ITEM, "reject")
        self.assertEqual(L.review.nw_state(L), "suppressed")
        outs = advanced(L, "adv:undo:nw")
        self.assertEqual((ignored(outs), L.review.nw_state(L)), ([], "proposed"))
        L2 = ready_learner()
        with_proposal(L2)
        ans(L2, NW_ITEM, "reject")
        ans(L2, NW_ITEM, "undo")                                           # the button 'Re-suggest night window'
        self.assertEqual((L2.review.suppressions, L2.review.nw_state(L2)), (L.review.suppressions, "proposed"))
        self.assertEqual(ignored(advanced(L, "adv:undo:nw")), [("adv:undo:nw", "advanced", "not_pending")])

    def test_undo_place(self):
        L = places(2)
        ans(L, "s0005-1", "reject")
        self.assertEqual(ids(L, 2), ["s0005-2"])
        outs = advanced(L, "adv:undo:s0005-1")
        self.assertEqual((ignored(outs), L.review.suppressions, L.p5.masks), ([], [], {}))
        self.assertEqual(ids(L, 2), ["s0005-1", "s0005-2"])
        self.assertEqual(ignored(advanced(L, "adv:undo:s0005-1")), [("adv:undo:s0005-1", "advanced", "no_such_item")])

    def test_undo_place_works_when_the_suggestion_is_gone(self):
        L = places(1)
        ids(L)                                                             # listed: the place has its tag
        ans(L, "s0005-1", "reject")
        L.ns.suggestions.clear()
        L.review.touch()
        e = L.advanced()[0]
        self.assertEqual((e.id, e.tag), ("adv:undo:s0005-1", L.p5.tags["s0005-1"]))   # the tag was kept: the label can say it
        self.assertEqual(ignored(advanced(L, e.id)), [])
        self.assertEqual(L.advanced(), ())

    def test_real(self):
        L = ready_learner()
        add_reflection(L, POS[0], "s0004-2")
        outs = advanced(L, "adv:real:s0004-2")
        self.assertEqual((ignored(outs), saves(outs), len(L.p5.real)), ([], ["review"], 1))
        self.assertEqual(L.p5.real[0]["ref"], "s0004-2")
        self.assertEqual(ignored(advanced(L, "adv:real:s0004-2")), [("adv:real:s0004-2", "advanced", "not_pending")])
        self.assertEqual(ignored(advanced(L, "adv:real:s9999-1")), [("adv:real:s9999-1", "advanced", "no_such_item")])
        add_place(L, POS[1], sid="s0004-5", conf=0.5, evidence=evidence())            # a place, not a reflection
        self.assertEqual(ignored(advanced(L, "adv:real:s0004-5")), [("adv:real:s0004-5", "advanced", "no_such_item")])
        L.ns.suggestions[0]["state"] = "stale"
        L.p5.real.clear()
        self.assertEqual(ignored(advanced(L, "adv:real:s0004-2")), [("adv:real:s0004-2", "advanced", "not_pending")])

    def test_rename_a_zone_and_a_label(self):
        L = places(1)
        ans(L, "s0005-1", "approve", name="Desk")
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.0, "y": 7.9, "name": "Spot",
                             "kind": "manual", "cells": frozenset()})
        outs = advanced(L, "adv:rename:z0001", name="Office")
        self.assertEqual((ignored(outs), saves(outs), L.review.zones[0]["name"], L.review.zones[0]["rev"]),
                         ([], ["review"], "Office", 1))
        self.assertIn(DebugMapChanged("review"), outs)
        advanced(L, "adv:rename:l0001", name="Plant")
        self.assertEqual(L.label_list[0]["name"], "Plant")
        outs = advanced(L, "adv:rename:z0001")
        self.assertEqual(ignored(outs), [("adv:rename:z0001", "advanced", "name_needed")])
        self.assertEqual(ignored(advanced(L, "adv:rename:z0009", name="X")), [("adv:rename:z0009", "advanced",
                                                                                "no_such_item")])
        advanced(L, "adv:rename:z0001", name="plant")                       # unique among zones and labels
        self.assertEqual(L.review.zones[0]["name"], "plant 2")

    def test_rename_uses_and_consumes_the_name_text_but_never_with_an_answer_id(self):
        from radar_zone_learner.core.types import SpotName
        L = places(1)
        ans(L, "s0005-1", "approve", name="Desk")
        L.handle(SpotName("Office", T8))
        outs = advanced(L, "adv:rename:z0001", aid="01HV")                  # a push or a script with an id: never the text
        self.assertEqual(ignored(outs), [("adv:rename:z0001", "advanced", "name_needed")])
        self.assertEqual(L.spot_name, "Office")
        outs = advanced(L, "adv:rename:z0001")
        self.assertEqual((L.review.zones[0]["name"], L.spot_name), ("Office", ""))
        self.assertEqual([o.text for o in outs if o.__class__.__name__ == "NameEcho"], [""])
        self.assertEqual(saves(outs), ["review"])
        # the leftover-of-a-mark filter does not apply: the user pressed Rename for this entry
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": 0.0, "y": 7.9, "name": "Bed",
                             "kind": "bed", "cells": frozenset()})
        L.handle(SpotName("Bed", T8 + 1))
        advanced(L, "adv:rename:z0001")
        self.assertEqual(L.review.zones[0]["name"], "Bed 2")

    def test_rename_to_bed_makes_a_bed_and_is_never_downgraded(self):
        L = places(1)
        ans(L, "s0005-1", "approve", name="Desk")
        advanced(L, "adv:rename:z0001", name="Bett")
        self.assertEqual(L.review.zones[0]["kind"], "bed")
        advanced(L, "adv:rename:z0001", name="Sofa")
        self.assertEqual((L.review.zones[0]["name"], L.review.zones[0]["kind"]), ("Sofa", "bed"))

    def test_the_record_revision_is_checked_when_given(self):
        L = places(1)
        ans(L, "s0005-1", "approve", name="Desk")
        outs = advanced(L, "adv:rename:z0001", name="X", rev=3)
        self.assertEqual(ignored(outs), [("adv:rename:z0001", "advanced", "stale")])
        self.assertEqual(L.review.stale_answers, 1)
        outs = advanced(L, "adv:remove:z0001", rev=3)
        self.assertEqual(ignored(outs), [("adv:remove:z0001", "advanced", "stale")])
        self.assertEqual(len(L.review.zones), 1)

    def test_remove_a_zone_gives_its_place_back(self):
        L = places(2)
        ans(L, "s0005-1", "approve", name="Bett")
        self.assertEqual(ids(L), ["s0005-2"])
        self.assertEqual(L.review.bed_state(L)[0], "withdrawn")
        outs = advanced(L, "adv:remove:z0001")
        self.assertEqual((ignored(outs), saves(outs), L.review.zones), ([], ["review"], []))
        self.assertIn(DebugMapChanged("review"), outs)
        self.assertEqual(ids(L), [BED_ITEM, "s0005-1", "s0005-2"])         # the place and the bed item return
        self.assertEqual(ignored(advanced(L, "adv:remove:z0001")), [("adv:remove:z0001", "advanced", "no_such_item")])
        self.assertEqual(L.review.next_zone_n, 2)                          # the counter never goes back

    def test_remove_a_label_resets_the_suggestions_it_linked(self):
        L = places(2)
        s = L.ns.suggestions[0]
        L.label_list.append({"id": "l0001", "created_s": BASE_MS // 1000, "x": POS[0][0] + 0.1, "y": POS[0][1],
                             "name": "Bed", "kind": "bed", "cells": L.geom.cells_within(POS[0][0], POS[0][1], 0.4)})
        L.label_list.append({"id": "l0002", "created_s": BASE_MS // 1000, "x": POS[1][0], "y": POS[1][1],
                             "name": "Desk", "kind": "manual", "cells": L.geom.cells_within(*POS[1], 0.4)})
        L.label_list.append({"id": "l0003", "created_s": BASE_MS // 1000, "x": POS[1][0] + 0.2, "y": POS[1][1],
                             "name": "Second", "kind": "manual", "cells": frozenset()})
        for x in L.ns.suggestions:
            L.link_labels_one(x)
        self.assertEqual((s["kind"], s["name_hint"]), ("bed", "Bed"))
        self.assertEqual(L.ns.suggestions[1]["name_hint"], "Desk")
        outs = advanced(L, "adv:remove:l0001")
        self.assertEqual((ignored(outs), [lb["id"] for lb in L.label_list]), ([], ["l0002", "l0003"]))
        self.assertEqual((s["kind"], s["name_hint"]), ("dwell", None))
        advanced(L, "adv:remove:l0002")                                    # l0003 still links the second place:
        self.assertEqual(L.ns.suggestions[1]["name_hint"], "Second")        # it is not reset, the learner relinks it
        advanced(L, "adv:remove:l0003")
        self.assertIsNone(L.ns.suggestions[1]["name_hint"])                 # the last link is gone
        self.assertEqual(ignored(advanced(L, "adv:remove:l0009")), [("adv:remove:l0009", "advanced", "no_such_item")])

    def test_malformed_advanced_answers(self):
        L = places(1)
        self.assertEqual(ignored(L.handle(ReviewAnswer(None, "advanced", None, False, T8))),
                         [(None, "advanced", "no_item")])
        self.assertEqual(ignored(advanced(L, "adv:bogus:z0001")), [("adv:bogus:z0001", "advanced", "no_such_item")])
        self.assertEqual(ignored(advanced(L, "s0005-1")), [("s0005-1", "advanced", "no_such_item")])
        self.assertEqual(ignored(advanced(L, "night_window")), [("night_window", "advanced", "no_such_item")])


if __name__ == "__main__":
    unittest.main()
