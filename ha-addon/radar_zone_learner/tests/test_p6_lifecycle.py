"""The lifecycle of zone entities (check P6-12, P6-10, spec 8.4, 7.1, D25, D27, D41, D43, HA[U1], HA[U2], HA[U7]): add,
rename, modify, remove, Forget all and the epoch, a Home Assistant birth, the cap of 24, the kinds, the variant entities."""
import json
import unittest

from radar_zone_learner.core.types import ReviewChanged, VariantRequest, ZoneForget, ZoneTune
from tests.fakes import FakeLearner
from tests.p6_harness import AVAIL, LayerRig, zone_view

CFG = "homeassistant/%s/%s/config"


def cfg_topics(rig, uid_prefix="radar_spot_"):
    return sorted({p[0] for p in rig.client.published if p[0].startswith("homeassistant/") and uid_prefix in p[0]})


def published_configs(rig):
    return [p for p in rig.client.published if p[0].startswith("homeassistant/") and "radar_spot" in p[0]]


def add_zone(rig, view, **fields):
    rig.learner.p5_zones = tuple(rig.learner.p5_zones) + (view,)
    rig.learner.p6_zone(view.id, **fields)
    rig.push_states()


class AddTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"))

    def test_a_new_zone_gets_its_six_configs_its_tuning_states_and_its_state(self):
        self.rig.cleared()
        add_zone(self.rig, zone_view("z0002", "Sofa", "short_task"), occ=True, count=1)
        cfgs = published_configs(self.rig)
        self.assertEqual(len(cfgs), 6)
        self.assertTrue(all(p[2:] == (1, False) for p in cfgs))                        # QoS 1, not retained
        self.assertEqual(self.rig.ha.state("binary_sensor.radar_spot_z0002"), "on")
        self.assertEqual(self.rig.ha.state("number.radar_spot_z0002_clear_delay"), "300")
        self.assertEqual(self.rig.ha.state("number.radar_spot_z0002_margin"), "25")
        self.assertEqual(self.rig.ha.attrs("binary_sensor.radar_spot_z0002")["kind"], "short_task")
        self.assertEqual(self.rig.last("radar_zones/zone/z0002/clear_delay"), "300")
        self.assertEqual(self.rig.published("radar_zones/zone/z0002/clear_delay")[0][2:], (1, True))   # retained
        self.assertEqual(self.rig.layer.attributes()["zone_entities"], 2)

    def test_the_other_zone_is_not_touched(self):
        self.rig.cleared()
        add_zone(self.rig, zone_view("z0002", "Sofa"))
        self.assertEqual([p for p in self.rig.client.published if "z0001" in p[0]], [])

    def test_a_new_zone_is_noticed_from_the_review_output_and_from_the_tick(self):
        self.rig.learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("l0001", "Bed", "bed"))
        self.rig.learner.p6_zone("l0001")
        self.rig.feed(ReviewChanged((), None))
        self.assertIsNotNone(self.rig.ha.entity("binary_sensor.radar_spot_l0001"))
        self.rig.learner.p5_zones += (zone_view("z0002", "Sofa"),)                   # no output at all: the tick finds it
        self.rig.run(6)
        self.assertIsNotNone(self.rig.ha.entity("binary_sensor.radar_spot_z0002"))

    def test_a_zone_that_comes_while_the_radars_are_off_has_its_entities_unavailable_until_the_resume(self):
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)
        add_zone(self.rig, zone_view("z0002", "Sofa"))
        self.rig.push_states(valid=False)
        self.assertEqual(self.rig.ha.state("binary_sensor.radar_spot_z0002"), "unavailable")
        self.assertEqual(self.rig.ha.state("number.radar_spot_z0002_clear_delay"), "300")
        self.rig.radar(state="on", observing=True)
        self.rig.push_states(valid=True)
        self.assertEqual(self.rig.ha.state("binary_sensor.radar_spot_z0002"), "off")

    def test_the_state_goes_out_again_after_ten_seconds_for_an_entity_home_assistant_creates_late(self):
        rig = LayerRig(self, create_delay_s=3.0)
        rig.ready(zone_view("z0001", "Desk"))
        rig.learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("z0002", "Sofa"))
        rig.learner.p6_zone("z0002", occ=True, count=1)
        rig.push_states()
        rig.run(1)
        self.assertIsNone(rig.ha.entity("binary_sensor.radar_spot_z0002"))            # not created yet: the state was lost
        rig.run(3)
        self.assertEqual(rig.ha.state("binary_sensor.radar_spot_z0002"), "unknown")       # created, no state yet
        rig.run(8)
        self.assertEqual(rig.ha.state("binary_sensor.radar_spot_z0002"), "on")        # the resend, 10 s after the config
        self.assertEqual(rig.ha.attrs("binary_sensor.radar_spot_z0002")["kind"], "dwell")


class RenameAndModifyTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"))
        self.rig.cleared()

    def test_a_rename_republishes_the_six_configs_with_the_same_unique_ids_and_topics(self):
        old_ids = {"radar_spot_z0001", "radar_spot_z0001_count", "radar_spot_z0001_event", "radar_spot_z0001_clear_delay",
                   "radar_spot_z0001_margin", "radar_spot_z0001_forget"}
        self.rig.learner.p5_zones = (zone_view("z0001", "Work desk"),)
        self.rig.feed(ReviewChanged((), None))
        cfgs = published_configs(self.rig)
        self.assertEqual(len(cfgs), 6)
        new = {json.loads(p[1])["unique_id"]: json.loads(p[1]) for p in cfgs}
        self.assertEqual(set(new), old_ids)
        self.assertEqual(new["radar_spot_z0001"]["name"], "Work desk occupancy")
        self.assertEqual(new["radar_spot_z0001"]["device"]["name"], "Work desk")
        self.assertEqual(new["radar_spot_z0001"]["state_topic"], "radar_zones/zone/z0001/occupancy")
        self.assertEqual(self.rig.ha.entity("binary_sensor.radar_spot_z0001").name, "Work desk occupancy")
        self.assertEqual(self.rig.ha.entity("binary_sensor.radar_spot_z0001").entity_id, "binary_sensor.radar_spot_z0001")
        self.assertEqual(self.rig.published("radar_zones/zone/z0001/occupancy"), [])   # the state is not sent again

    def test_a_label_named_later_changes_from_spot_to_its_name(self):
        add_zone(self.rig, zone_view("l0001", "", "bed"))
        self.assertEqual(self.rig.ha.entity("binary_sensor.radar_spot_l0001").name, "Spot l0001 occupancy")
        self.rig.learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("l0001", "Bed", "bed"))
        self.rig.feed(ReviewChanged((), None))
        self.assertEqual(self.rig.ha.entity("binary_sensor.radar_spot_l0001").name, "Bed occupancy")

    def test_modify_and_merge_change_no_config_but_the_attributes(self):
        self.rig.learner.p5_zones = (zone_view("z0001", "Desk", rev=2, poly=((0, 0), (50, 0), (50, 50), (0, 50))),)
        self.rig.feed(ReviewChanged((), None))
        self.assertEqual(published_configs(self.rig), [])
        self.assertEqual(self.rig.ha.attrs("binary_sensor.radar_spot_z0001")["poly_cm"],
                         [[0, 0], [50, 0], [50, 50], [0, 50]])

    def test_a_changed_kind_shows_in_the_attributes_only(self):
        self.rig.learner.p5_zones = (zone_view("z0001", "Desk", "bed"),)
        self.rig.feed(ReviewChanged((), None))
        self.assertEqual(published_configs(self.rig), [])
        self.assertEqual(self.rig.ha.attrs("binary_sensor.radar_spot_z0001")["kind"], "bed")


class RemoveTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"), zone_view("z0002", "Sofa"))
        self.rig.learner.p6_zone("z0002", occ=True, count=1)
        self.rig.push_states()
        self.rig.cleared()

    def remove(self, zid="z0002"):
        self.rig.learner.p5_zones = tuple(v for v in self.rig.learner.p5_zones if v.id != zid)
        self.rig.learner.p6_zones.pop(zid, None)
        self.rig.feed(ReviewChanged((), None))
        self.rig.push_states()

    def test_a_removed_zone_gets_six_empty_not_retained_configs_once(self):
        self.remove()
        cfgs = published_configs(self.rig)
        self.assertEqual(len(cfgs), 6)
        self.assertTrue(all(p[1] == b"" and p[2:] == (1, False) for p in cfgs))
        self.assertEqual({p[0] for p in cfgs}, {CFG % (c, u) for c, u in (
            ("binary_sensor", "radar_spot_z0002"), ("sensor", "radar_spot_z0002_count"),
            ("event", "radar_spot_z0002_event"), ("number", "radar_spot_z0002_clear_delay"),
            ("number", "radar_spot_z0002_margin"), ("button", "radar_spot_z0002_forget"))})
        self.rig.cleared()
        self.rig.run(12)
        self.assertEqual(published_configs(self.rig), [])                            # once, not every tick

    def test_the_retained_tuning_states_are_cleared_with_an_empty_retained_publish(self):
        self.remove()
        for topic in ("radar_zones/zone/z0002/clear_delay", "radar_zones/zone/z0002/margin"):
            pubs = self.rig.published(topic)
            self.assertEqual([(p[1], p[3]) for p in pubs], [(b"", True)], topic)
        self.assertNotIn("radar_zones/zone/z0002/clear_delay", self.rig.ha.broker_retained)
        self.assertIn("radar_zones/zone/z0001/clear_delay", self.rig.ha.broker_retained)

    def test_nothing_more_is_published_for_the_removed_zone_and_the_entities_are_gone(self):
        self.remove()
        self.rig.cleared()
        self.rig.run(12)
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.assertEqual([p for p in self.rig.client.published if "z0002" in p[0]], [])
        for eid in ("binary_sensor.radar_spot_z0002", "event.radar_spot_z0002_event",
                    "number.radar_spot_z0002_clear_delay", "button.radar_spot_z0002_forget"):
            self.assertIsNone(self.rig.ha.entity(eid), eid)                          # HA[U2]
        self.assertEqual(self.rig.ha.state("binary_sensor.radar_spot_z0001"), "on")

    def test_a_removed_zone_has_no_entity_after_a_home_assistant_restart(self):
        self.remove()
        self.rig.ha.stop()
        self.rig.ha.start(birth=False)
        self.rig.burst()
        self.assertEqual([e for e in self.rig.ha.entity_ids() if "z0002" in e], [])
        self.assertIsNotNone(self.rig.ha.entity("binary_sensor.radar_spot_z0001"))   # HA[U1]: the other one is back

    def test_a_removed_zone_that_was_never_announced_costs_nothing(self):
        n = len(self.rig.client.published)
        self.rig.learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("z0002", "Sofa"))
        self.rig.feed(ReviewChanged((), None))
        self.assertEqual(len(self.rig.client.published), n)

    def test_a_command_for_a_removed_zone_is_dropped(self):
        self.remove()
        given = self.rig.layer.on_event(ZoneTune("z0002", "cd", 60, self.rig.ms), self.rig.ms)
        self.assertEqual(given, [])
        self.assertEqual(self.rig.layer.attributes()["zone_cmds_ignored"], 1)


class ForgetAllTest(unittest.TestCase):
    """Forget all (spec 6.3, 8.4, D24, D41): every entity goes, ids restart, the new unique ids carry the epoch."""

    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"), zone_view("l0001", "Bed", "bed"))
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.ha = self.rig.ha

    def forget_all(self):
        """``Service.forget_all`` for the layer: reset, a new learner (epoch + 1), bind, burst."""
        fresh = FakeLearner()
        fresh.p6_epoch = self.rig.learner.p6_epoch + 1
        self.rig.layer.reset()
        self.rig.learner = fresh
        self.rig.bind()
        self.rig.burst()
        return fresh

    def test_every_zone_entity_is_removed_and_the_availability_is_offline(self):
        self.rig.cleared()
        self.rig.layer.reset()
        self.ha.process()
        cfgs = published_configs(self.rig)
        self.assertEqual(len(cfgs), 12)
        self.assertTrue(all(p[1] == b"" for p in cfgs))
        self.assertEqual(self.rig.last(AVAIL), "offline")
        self.assertEqual([e for e in self.ha.entity_ids() if "radar_spot" in e], [])
        for topic in ("radar_zones/zone/z0001/clear_delay", "radar_zones/zone/l0001/margin"):
            self.assertEqual(self.rig.last(topic), "")
        self.assertEqual({t: p for t, p in self.ha.broker_retained.items() if "radar_zones/zone/z" in t
                          or "radar_zones/zone/l" in t}, {})

    def test_a_new_zone_with_a_reused_id_has_a_different_unique_id_and_device_identifier(self):
        fresh = self.forget_all()
        fresh.p5_zones = (zone_view("z0001", "New desk"),)                           # the id restarts at z0001
        fresh.p6_zone("z0001")
        self.rig.push_states(True)
        self.rig.radar()
        self.rig.push_states(True)
        ent = self.ha.entity("binary_sensor.radar_spot_e1_z0001")
        self.assertIsNotNone(ent)
        self.assertEqual(ent.cfg["unique_id"], "radar_spot_e1_z0001")
        self.assertEqual(ent.cfg["device"]["identifiers"], ["radar_zone_e1_z0001"])
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_z0001"))
        # the data topics keep the plain id
        self.assertEqual(self.rig.last("radar_zones/zone/z0001/occupancy"), "OFF")

    def test_the_old_history_is_not_inherited_by_the_new_zone(self):
        old_states = self.ha.states("binary_sensor.radar_spot_z0001")
        self.assertIn("on", old_states)
        fresh = self.forget_all()
        fresh.p5_zones = (zone_view("z0001", "New desk"),)
        fresh.p6_zone("z0001")
        self.rig.push_states(True)
        self.rig.radar()
        self.rig.push_states(True)
        self.assertEqual(self.ha.states("binary_sensor.radar_spot_e1_z0001"), ["unavailable", "off"])   # HA[U7]

    def test_without_the_epoch_a_reused_unique_id_would_inherit_the_registry_entry(self):
        # the reason for D41: the fake registry (HA[U7]) hands the old entity id and history to a reused unique id
        fresh = FakeLearner()
        self.rig.layer.reset()
        self.rig.learner = fresh                                                     # epoch stays 0: the wrong behaviour
        self.rig.bind()
        fresh.p5_zones = (zone_view("z0001", "New desk"),)
        fresh.p6_zone("z0001")
        self.rig.burst()
        self.rig.radar()
        self.rig.push_states(True)
        self.assertGreater(len(self.ha.states("binary_sensor.radar_spot_z0001")), 2)
        self.assertEqual(self.ha.registry["radar_spot_z0001"]["created"], 2)

    def test_the_second_forget_all_goes_to_epoch_two(self):
        self.forget_all()
        self.rig.learner.p5_zones = (zone_view("z0001", "A"),)
        self.rig.learner.p6_zone("z0001")
        self.rig.push_states(True)
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_e1_z0001"))
        self.forget_all()
        self.rig.learner.p5_zones = (zone_view("z0001", "B"),)
        self.rig.learner.p6_zone("z0001")
        self.rig.push_states(True)
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_e2_z0001"))
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_e1_z0001"))

    def test_a_forget_all_with_no_zone_publishes_nothing_for_zones_but_the_availability(self):
        rig = LayerRig(self)
        rig.start()
        rig.cleared()
        rig.layer.reset()
        topics = {p[0] for p in rig.client.published}
        self.assertEqual(topics - {"radar_zones/learner/forget_confirm"}, {AVAIL})

    def test_the_state_after_it_is_unavailable_until_the_new_core_resumes(self):
        fresh = self.forget_all()
        self.rig.radar()
        self.assertEqual(self.rig.last(AVAIL), "offline")                            # the new core is not valid yet
        fresh.p5_zones = (zone_view("z0001", "N"),)
        fresh.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states(True)
        self.assertEqual(self.rig.last(AVAIL), "online")
        self.assertEqual(self.ha.state("binary_sensor.radar_spot_e1_z0001"), "on")


class BirthTest(unittest.TestCase):
    def test_a_home_assistant_birth_sends_the_full_set_again(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"), zone_view("l0001", "", "bed"))
        rig.learner.p6_zone("z0001", occ=True, count=1)
        rig.push_states()
        rig.ha.stop()
        rig.ha.start(birth=False)
        rig.cleared()
        rig.burst()
        self.assertEqual(len(published_configs(rig)), 12)
        self.assertEqual(rig.ha.state("binary_sensor.radar_spot_z0001"), "on")
        self.assertEqual(rig.ha.state("binary_sensor.radar_spot_l0001"), "off")
        self.assertEqual(rig.ha.state("number.radar_spot_z0001_clear_delay"), "300")
        self.assertEqual(rig.last(AVAIL), "online")


class CapTest(unittest.TestCase):
    def views(self, n):
        return [zone_view("z%04d" % k, "Z%d" % k) for k in range(1, n + 1)]

    def test_only_the_first_twenty_four_get_entities(self):
        rig = LayerRig(self)
        rig.ready(*self.views(30))
        occ = [e for e in rig.ha.entity_ids() if e.startswith("binary_sensor.radar_spot_")]
        self.assertEqual(len(occ), 24)
        self.assertEqual(occ[0], "binary_sensor.radar_spot_z0001")
        self.assertEqual(occ[-1], "binary_sensor.radar_spot_z0024")
        self.assertEqual(rig.layer.attributes()["zone_entities"], 24)
        self.assertEqual([p for p in rig.client.published if "z0025" in p[0]], [])    # the others are not published at all
        self.assertEqual(len([e for e in rig.ha.entity_ids() if e.startswith(("number.radar_spot_", "button.radar_spot_",
                                                                            "event.radar_spot_"))]), 24 * 4)

    def test_a_slot_frees_when_a_zone_is_removed(self):
        rig = LayerRig(self)
        rig.ready(*self.views(26))
        rig.learner.p5_zones = tuple(v for v in rig.learner.p5_zones if v.id != "z0003")
        rig.learner.p6_zones.pop("z0003")
        rig.feed(ReviewChanged((), None))
        self.assertIsNone(rig.ha.entity("binary_sensor.radar_spot_z0003"))
        self.assertIsNotNone(rig.ha.entity("binary_sensor.radar_spot_z0025"))
        self.assertIsNone(rig.ha.entity("binary_sensor.radar_spot_z0026"))

    def test_a_command_for_a_zone_beyond_the_cap_is_dropped(self):
        rig = LayerRig(self)
        rig.ready(*self.views(30))
        self.assertEqual(rig.layer.on_event(ZoneForget("z0030", rig.ms), rig.ms), [])
        self.assertEqual(len(rig.layer.on_event(ZoneForget("z0024", rig.ms), rig.ms)), 1)

    def test_the_cap_follows_the_parameter(self):
        rig = LayerRig(self, cap=3)
        rig.ready(*self.views(5))
        self.assertEqual(len([e for e in rig.ha.entity_ids() if e.startswith("binary_sensor.radar_spot_")]), 3)


class KindTest(unittest.TestCase):
    def test_only_dwell_short_task_bed_and_manual_zones_have_entities(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "A", "dwell"), zone_view("z0002", "B", "entry"), zone_view("z0003", "C", "bed"),
                  zone_view("z0004", "D", "transit"), zone_view("z0005", "E", "short_task"),
                  zone_view("l0001", "F", "manual"), zone_view("l0002", "G", "pet"))
        occ = sorted(e for e in rig.ha.entity_ids() if e.startswith("binary_sensor.radar_spot_"))
        self.assertEqual(occ, ["binary_sensor.radar_spot_l0001", "binary_sensor.radar_spot_z0001",
                               "binary_sensor.radar_spot_z0003", "binary_sensor.radar_spot_z0005"])


class VariantEntitiesTest(unittest.TestCase):
    """D43, spec 5.8: one request switch per variant, an occupancy sensor only on request."""

    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.zones(zone_view("z0001", "Desk"), zone_view("l0001", "Bed", "bed"))
        self.rig.learner.p6_zone("z0001", variants=("wd0917", "we1022"))
        self.rig.learner.p6_zone("l0001")
        self.rig.start()
        self.rig.radar()
        self.rig.push_states(True)
        self.rig.run(11)
        self.ha = self.rig.ha

    def test_a_request_switch_exists_for_each_variant_and_off_by_default_and_no_sensor(self):
        self.assertEqual(self.ha.state("switch.radar_spot_z0001_vreq_wd0917"), "off")
        self.assertEqual(self.ha.state("switch.radar_spot_z0001_vreq_we1022"), "off")
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))
        self.assertEqual([e for e in self.ha.entity_ids() if "radar_spot_l0001_vreq" in e], [])   # a zone without variants
        self.assertEqual(self.ha.entity("switch.radar_spot_z0001_vreq_wd0917").cfg["entity_category"], "config")
        self.assertEqual(self.rig.published("radar_zones/zone/z0001/wd0917/entity")[0][1:], (b"OFF", 1, True))   # retained

    def test_turning_the_switch_on_creates_the_sensor_and_it_follows_occupancy_and_variant_now(self):
        given = self.rig.send_event(VariantRequest("z0001", "wd0917", True, self.rig.ms))
        self.assertEqual(len(given), 1)
        self.assertEqual(self.ha.state("switch.radar_spot_z0001_vreq_wd0917"), "on")
        sensor = "binary_sensor.radar_spot_z0001_v_wd0917"
        self.assertEqual(self.ha.state(sensor), "off")
        self.assertEqual(self.ha.entity(sensor).cfg["device_class"], "occupancy")
        L = self.rig.learner
        L.p6_zone("z0001", occ=True, count=1, variant="we1022")                       # occupied, but another variant
        self.rig.push_states()
        self.assertEqual(self.ha.state(sensor), "off")
        L.p6_zone("z0001", variant="wd0917")                                          # the hour boundary
        self.rig.push_states()
        self.assertEqual(self.ha.state(sensor), "on")
        L.p6_zone("z0001", occ=False, count=0)
        self.rig.push_states()
        self.assertEqual(self.ha.state(sensor), "off")
        self.assertEqual(self.rig.published("radar_zones/zone/z0001/wd0917/occupancy")[0][2:], (1, False))

    def test_turning_the_switch_off_removes_the_sensor_but_not_the_switch(self):
        self.rig.send_event(VariantRequest("z0001", "wd0917", True, self.rig.ms))
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))
        self.rig.cleared()
        self.rig.send_event(VariantRequest("z0001", "wd0917", False, self.rig.ms))
        cfgs = published_configs(self.rig)
        self.assertEqual([(p[0], p[1], p[2:]) for p in cfgs],
                         [(CFG % ("binary_sensor", "radar_spot_z0001_v_wd0917"), b"", (1, False))])
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))
        self.assertIsNotNone(self.ha.entity("switch.radar_spot_z0001_vreq_wd0917"))
        self.assertEqual(self.ha.state("switch.radar_spot_z0001_vreq_wd0917"), "off")

    def test_a_variant_that_disappears_at_the_nightly_refresh_removes_its_switch_and_sensor_and_its_state(self):
        self.rig.send_event(VariantRequest("z0001", "we1022", True, self.rig.ms))
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_we1022"))
        self.rig.cleared()
        self.rig.learner.p6_zone("z0001", variants=("wd0917",), requested=())
        self.rig.push_states()
        cfgs = {p[0]: p[1] for p in published_configs(self.rig)}
        self.assertEqual(cfgs, {CFG % ("switch", "radar_spot_z0001_vreq_we1022"): b"",
                                CFG % ("binary_sensor", "radar_spot_z0001_v_we1022"): b""})
        self.assertIsNone(self.ha.entity("switch.radar_spot_z0001_vreq_we1022"))
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_we1022"))
        self.assertEqual([(p[1], p[3]) for p in self.rig.published("radar_zones/zone/z0001/we1022/entity")],
                         [(b"", True)])                                               # the retained request state is cleared
        self.assertNotIn("radar_zones/zone/z0001/we1022/entity", self.ha.broker_retained)
        self.assertIsNotNone(self.ha.entity("switch.radar_spot_z0001_vreq_wd0917"))

    def test_a_new_variant_gets_its_request_switch(self):
        self.rig.cleared()
        self.rig.learner.p6_zone("z0001", variants=("wd0917", "we1022", "wd1822"))
        self.rig.push_states()
        cfgs = published_configs(self.rig)
        self.assertEqual([(p[0], p[1] != b"") for p in cfgs], [(CFG % ("switch", "radar_spot_z0001_vreq_wd1822"), True)])
        self.assertEqual(self.ha.state("switch.radar_spot_z0001_vreq_wd1822"), "off")

    def test_a_requested_variant_is_restored_after_a_restart_of_home_assistant(self):
        self.rig.send_event(VariantRequest("z0001", "wd0917", True, self.rig.ms))
        self.ha.stop()
        self.ha.start(birth=False)
        self.rig.burst()
        self.assertEqual(self.ha.state("switch.radar_spot_z0001_vreq_wd0917"), "on")  # the retained request state
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))

    def test_a_removed_zone_takes_its_variant_entities_with_it(self):
        self.rig.send_event(VariantRequest("z0001", "wd0917", True, self.rig.ms))
        self.rig.cleared()
        self.rig.learner.p5_zones = (zone_view("l0001", "Bed", "bed"),)
        self.rig.learner.p6_zones.pop("z0001")
        self.rig.feed(ReviewChanged((), None))
        gone = {p[0] for p in published_configs(self.rig) if p[1] == b""}
        self.assertEqual(len(gone), 6 + 2 + 1)
        self.assertEqual([e for e in self.ha.entity_ids() if "z0001" in e], [])
        retained = [t for t in self.ha.broker_retained if t.startswith("radar_zones/zone/z0001/")]
        self.assertEqual(retained, [])

    def test_a_request_for_a_variant_the_zone_does_not_have_is_dropped(self):
        self.assertEqual(self.rig.layer.on_event(VariantRequest("z0001", "wd1111", True, 0), 0), [])
        self.assertEqual(self.rig.layer.on_event(VariantRequest("l0001", "wd0917", True, 0), 0), [])
        self.assertEqual(self.rig.layer.attributes()["zone_cmds_ignored"], 2)


if __name__ == "__main__":
    unittest.main()
