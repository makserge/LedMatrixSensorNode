"""P6Layer with the FakeLearner and the fake Home Assistant (checks P6-5 b and P6-12, spec 5.7, 8.1, 8.3, 8.6, D19, D20):
availability and the order at resume, the state source, the attributes, the events, the count rate, republish and the second
pass. Home Assistant's behaviour is the HA[U<n>] assumptions of ``tests/fake_ha_zone.py``."""
import json
import unittest

from radar_zone_learner.core.types import NodeStatus, ReviewChanged, StatusChanged, ZoneEvent, ZoneStates
from radar_zone_learner.shell.p6 import P6Layer, read_epoch
from radar_zone_learner.shell.zonepub import attribute_payload, event_payload, state_source
from tests.fakes import FakeLearner
from tests.p6_harness import AVAIL, LayerRig, zone_view

OCC = "radar_zones/zone/z0001/occupancy"
COUNT = "radar_zones/zone/z0001/count"
EVENT = "radar_zones/zone/z0001/event"
ATTR = "radar_zones/zone/z0001/attributes"
CD = "radar_zones/zone/z0001/clear_delay"
MG = "radar_zones/zone/z0001/margin"
OCC_ID = "binary_sensor.radar_spot_z0001"
EV_ID = "event.radar_spot_z0001_event"
NUM_ID = "number.radar_spot_z0001_clear_delay"


def zones_on(rig, *views):
    rig.ready(*views)
    return rig


class AvailabilityTest(unittest.TestCase):
    """P6-5 (b): the zone entities are unavailable while the radars are off, and the history is unavailable -> real state."""

    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"))
        self.ha = self.rig.ha

    def test_online_with_the_radars_on(self):
        self.assertEqual(self.rig.last(AVAIL), "online")
        self.assertEqual(self.ha.state(OCC_ID), "off")
        self.assertEqual(self.ha.state(NUM_ID), "300")

    def test_p1_the_radars_off_by_schedule_make_every_zone_entity_unavailable_within_one_pass(self):
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.assertEqual(self.ha.state(OCC_ID), "on")
        self.rig.client.published.clear()
        self.ha._cursor = 0
        self.rig.radar(state="night_off", observing=False)                  # the shell's fast path: no core output needed
        self.assertEqual(self.rig.last(AVAIL), "offline")
        for eid in (OCC_ID, EV_ID):
            self.assertEqual(self.ha.state(eid), "unavailable", eid)
        # nothing but the availability and the attributes was published
        topics = {p[0] for p in self.rig.client.published}
        self.assertTrue(topics <= {AVAIL, ATTR, "radar_zones/radar/valid", "radar_zones/learner/state",
                                   "radar_zones/learner/attr", "radar_zones/scan/rf_on_24h",
                                   "radar_zones/scan/rf_saving_7d", "radar_zones/learner/coverage_7d"}, topics)
        self.assertEqual(len(self.rig.published(AVAIL)), 1)

    def test_the_core_turning_invalid_alone_also_makes_the_zones_unavailable(self):
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.rig.push_states(valid=False)
        self.assertEqual(self.rig.last(AVAIL), "offline")
        self.assertEqual(self.ha.state(OCC_ID), "unavailable")

    def test_p2_the_return_is_unavailable_then_the_real_state_with_no_off_row_between_and_never_the_stale_on(self):
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()                                              # the evening: occupied
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)                                   # the core's resume state: invalid
        self.assertEqual(self.ha.states(OCC_ID), ["unavailable", "off", "on", "unavailable"])
        self.rig.client.published.clear()
        self.ha._cursor = 0
        self.rig.learner.p6_zone("z0001", occ=False, count=0)               # nobody there at 06:00
        self.rig.radar(state="on", observing=True)                          # the shell's side is back first
        self.assertEqual(self.ha.state(OCC_ID), "unavailable")              # the core has not resumed: the AND holds it
        self.rig.push_states(valid=True)
        self.assertEqual(self.ha.state(OCC_ID), "off")
        self.assertEqual(self.ha.states(OCC_ID), ["unavailable", "off", "on", "unavailable", "off"])
        self.assertEqual(self.ha.history(OCC_ID)[-2:], [("unavailable", False), ("off", True)])
        # the order on the wire: the real state first, availability last
        topics = [p[0] for p in self.rig.client.published if p[0] in (OCC, COUNT, AVAIL)]
        self.assertEqual(topics, [OCC, COUNT, AVAIL])
        self.assertEqual(self.rig.last(OCC), "OFF")
        self.assertEqual(self.rig.last(AVAIL), "online")

    def test_p2_with_a_person_there_it_is_unavailable_then_on_and_no_event(self):
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)
        events_before = len(self.ha.events(EV_ID))
        self.rig.learner.p6_zone("z0001", occ=True, count=1)                # a silent resume snapshot: no event
        self.rig.radar(state="on", observing=True)
        self.rig.push_states(valid=True)
        self.assertEqual(self.ha.states(OCC_ID)[-2:], ["unavailable", "on"])
        self.assertEqual(self.ha.state(OCC_ID), "on")
        self.assertEqual(len(self.ha.events(EV_ID)), events_before)
        self.assertEqual(self.rig.published(EVENT), [])

    def test_p2_the_core_resuming_before_the_shell_gives_the_same_history(self):
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)
        self.rig.learner.p6_zone("z0001", occ=False, count=0)
        self.rig.push_states(valid=True)                                    # the core first: the shell is still off
        self.assertEqual(self.ha.state(OCC_ID), "unavailable")
        self.assertEqual(self.rig.last(AVAIL), "offline")
        self.rig.radar(state="on", observing=True)
        self.assertEqual(self.ha.states(OCC_ID), ["unavailable", "off", "on", "unavailable", "off"])

    def test_p3_the_state_source_shows_while_the_zone_is_unavailable(self):
        def source():
            return self.ha.attrs(OCC_ID)["state_source"]
        self.assertEqual(source(), "live")
        self.rig.radar(state="night_off", observing=False)
        self.assertEqual((self.ha.state(OCC_ID), source()), ("unavailable", "radar_off"))
        self.rig.radar(state="on", observing=False)                          # powered, not observing yet
        self.assertEqual((self.ha.state(OCC_ID), source()), ("unavailable", "warmup"))
        self.rig.radar(state="on", observing=True)
        self.rig.push_states(valid=True)
        self.assertEqual((self.ha.state(OCC_ID), source()), ("off", "live"))
        self.rig.radar(state="failsafe_on", observing=True)
        self.assertEqual(source(), "failsafe")
        self.rig.radar(state="forced_off", observing=False)
        self.assertEqual(source(), "radar_off")

    def test_p4_the_node_will_makes_every_zone_unavailable(self):
        self.rig.node(False)
        self.assertEqual(self.ha.state(OCC_ID), "unavailable")                # Home Assistant sees the will itself
        self.assertEqual(self.rig.last(AVAIL), "offline")                     # and the layer's own AND follows
        self.assertEqual(self.ha.attrs(OCC_ID)["state_source"], "fault")
        self.rig.node(True)
        self.rig.radar(state="on", observing=True)
        self.rig.push_states(valid=True)
        self.assertEqual(self.ha.state(OCC_ID), "off")

    def test_p5_the_learner_will_makes_every_zone_entity_unavailable_even_the_tuning_ones(self):
        self.rig.learner_status(False)
        for eid in (OCC_ID, EV_ID, NUM_ID, "button.radar_spot_z0001_forget", "number.radar_spot_z0001_margin"):
            self.assertEqual(self.ha.state(eid), "unavailable", eid)
        self.rig.learner_status(True)
        self.assertEqual(self.ha.state(OCC_ID), "off")
        self.assertEqual(self.ha.state(NUM_ID), "300")

    def test_the_tuning_entities_stay_available_while_the_radars_are_off(self):
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)
        self.assertEqual(self.ha.state(OCC_ID), "unavailable")
        self.assertEqual(self.ha.state(NUM_ID), "300")
        self.assertEqual(self.ha.state("number.radar_spot_z0001_margin"), "25")

    def test_p6_a_home_assistant_restart_while_the_radars_are_off(self):
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)
        self.ha.stop()
        self.ha.start(birth=False)
        self.assertEqual(self.ha.entity_ids(), [])
        self.rig.burst()                                                     # the app's answer to the birth message
        self.rig.run(11)
        self.assertEqual(self.ha.state(OCC_ID), "unavailable")               # the retained availability is offline
        self.assertEqual(self.ha.attrs(OCC_ID)["state_source"], "radar_off")
        self.assertEqual(self.ha.state(NUM_ID), "300")                       # the retained tuning state is replayed
        self.rig.learner.p6_zone("z0001", occ=False)
        self.rig.radar(state="on", observing=True)
        self.rig.push_states(valid=True)
        self.assertEqual(self.ha.state(OCC_ID), "off")

    def test_a_home_assistant_restart_while_the_zone_is_occupied_shows_the_state_after_the_second_pass(self):
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.ha.stop()
        self.ha.start(birth=False)
        self.rig.burst()                                                     # the entities exist after their configs
        self.assertEqual(self.ha.state(OCC_ID), "on")                        # the burst's own states come after the configs
        self.rig.run(11)
        self.assertEqual(self.ha.state(OCC_ID), "on")

    def test_a_flapping_availability_is_published_once_per_change(self):
        self.rig.radar(state="night_off", observing=False)
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)
        self.assertEqual([p[1] for p in self.rig.published(AVAIL)][-1], b"offline")
        n = len(self.rig.published(AVAIL))
        self.rig.push_states(valid=False)
        self.rig.run(3)
        self.assertEqual(len(self.rig.published(AVAIL)), n)


class AddonRestartTest(unittest.TestCase):
    """P7: the add-on restarts while the radars are on and a person is in a zone."""

    def test_unavailable_from_bind_until_the_core_resumes_then_on_with_no_off_row_and_no_event(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        rig.learner.p6_zone("z0001", occ=True, count=1)
        rig.push_states()
        self.assertEqual(rig.ha.state(OCC_ID), "on")
        # the add-on dies (its will) and starts again with a new learner that has not resumed
        rig.learner_status(False)
        self.assertEqual(rig.ha.state(OCC_ID), "unavailable")
        learner = FakeLearner()
        learner.p5_zones = (zone_view("z0001", "Desk"),)
        learner.p6_zone("z0001", occ=False)                                  # the core has no state yet: every zone starts off
        rig.layer = P6Layer(rig.topics, rig.publish, None, monotonic=rig.clock.monotonic, clock_ms=rig.clock.clock_ms,
                            debug_map=False, p6params=rig.p6)
        rig.learner = learner
        rig.client.published.clear()
        rig.ha._cursor = 0
        rig.start()                                                          # bind (availability offline), connect, burst
        self.assertEqual(rig.last(AVAIL), "offline")
        first_zone_topic = [p[0] for p in rig.client.published if p[0].startswith("radar_zones/zone/")
                            or "radar_spot" in p[0]][0]
        self.assertEqual(first_zone_topic, AVAIL)                            # offline before the first config and state
        rig.layer.on_event(NodeStatus(True, rig.ms), rig.ms)
        rig.radar(state="on", observing=True)                                # the retained observing ON: 'radar/valid' is true
        self.assertEqual(rig.ha.state(OCC_ID), "unavailable")                # the zone topic is the AND: still unavailable
        self.assertEqual(rig.last(AVAIL), "offline")
        rig.run(12)
        self.assertEqual(rig.ha.state(OCC_ID), "unavailable")
        learner.p6_zone("z0001", occ=True, count=1)                          # the core resumed with the person in the zone
        rig.push_states(valid=True)
        self.assertEqual(rig.ha.state(OCC_ID), "on")
        self.assertEqual(rig.ha.states(OCC_ID), ["unavailable", "off", "on", "unavailable", "on"])
        self.assertEqual(rig.ha.events(EV_ID), [])
        self.assertEqual(rig.published(EVENT), [])


class BindTest(unittest.TestCase):
    def test_bind_publishes_the_availability_offline_before_any_zone_state_and_it_is_retained(self):
        rig = LayerRig(self)
        rig.zones(zone_view("z0001", "Desk"))
        rig.learner.p6_zone("z0001", occ=True, count=1)
        rig.learner.p6_valid = True                                          # even a valid snapshot: the shell is not valid yet
        rig.bind()
        first = [p for p in rig.client.published if p[0].startswith("radar_zones/zone/")][0]
        self.assertEqual((first[0], first[1], first[2:]), (AVAIL, b"offline", (1, True)))
        self.assertEqual(rig.published(OCC), [])                              # no occupancy before the radars are seen valid
        self.assertEqual(rig.published(COUNT), [])

    def test_bind_before_the_link_is_up_publishes_nothing_and_the_burst_sends_it_all(self):
        rig = LayerRig(self, connected=False)
        rig.zones(zone_view("z0001", "Desk"))
        rig.learner.p6_zone("z0001")
        rig.bind()
        self.assertEqual(rig.client.published, [])
        rig.connected = True
        rig.start()
        self.assertEqual(rig.last(AVAIL), "offline")
        self.assertEqual(rig.last(CD), "300")

    def test_the_occupancy_topic_is_not_retained_and_the_availability_is(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        self.assertTrue(all(p[3] is False for p in rig.published(OCC)))
        self.assertTrue(all(p[3] is True for p in rig.published(AVAIL)))


class StateSourceTest(unittest.TestCase):
    def test_the_table(self):
        s = state_source
        self.assertEqual(s("on", True, True), "live")
        self.assertEqual(s("forced_on", True, True), "live")
        self.assertEqual(s(None, None, None), "live")                          # a P1 node: no radar topics
        self.assertEqual(s("failsafe_on", True, True), "failsafe")
        self.assertEqual(s("night_off", False, True), "radar_off")
        self.assertEqual(s("forced_off", None, True), "radar_off")
        self.assertEqual(s("on", False, True), "warmup")
        self.assertEqual(s("forced_on", False, True), "warmup")
        self.assertEqual(s("on", True, False), "fault")
        self.assertEqual(s("night_off", False, False), "fault")                 # the node itself is gone
        self.assertEqual(s("on", True, True, "no_data"), "fault")
        self.assertEqual(s(None, None, True, "not_observing"), "radar_off")


class StatePublishingTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"), zone_view("l0001", ""))
        self.ha = self.rig.ha
        self.rig.cleared()

    def test_occupancy_is_published_at_once_on_and_off_and_held_counts_as_occupied(self):
        L = self.rig.learner
        L.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.assertEqual(self.rig.last(OCC), "ON")
        L.p6_zone("z0001", occ=True, count=0, held=True)                    # the target vanished inside: held
        self.rig.push_states()
        self.assertEqual(self.rig.last(OCC), "ON")
        self.assertEqual(len(self.rig.published(OCC)), 1)                    # nothing changed for occupancy: no publish
        L.p6_zone("z0001", occ=False, count=0, held=False)
        self.rig.push_states()
        self.assertEqual(self.rig.last(OCC), "OFF")
        self.assertEqual([p[1] for p in self.rig.published(OCC)], [b"ON", b"OFF"])
        self.assertEqual(self.rig.published(OCC)[0][2:], (1, False))         # QoS 1, not retained

    def test_a_repeated_identical_state_publishes_nothing(self):
        L = self.rig.learner
        L.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        n = len(self.rig.client.published)
        self.rig.push_states()
        self.rig.push_states()
        self.assertEqual(len(self.rig.client.published), n)

    def test_the_count_is_published_at_most_once_a_second_with_a_trailing_publish(self):
        L = self.rig.learner
        L.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.assertEqual([p[1] for p in self.rig.published(COUNT)], [b"1"])
        for n in (2, 3, 2):                                                  # three changes inside the next second
            self.rig.clock.advance(0.2)
            L.p6_zone("z0001", count=n)
            self.rig.push_states()
        self.assertEqual([p[1] for p in self.rig.published(COUNT)], [b"1"])  # held back
        self.assertEqual(self.rig.last(OCC), "ON")                           # occupancy is never held back
        self.rig.clock.advance(0.3)
        self.rig.layer.tick(self.rig.ms)
        self.assertEqual([p[1] for p in self.rig.published(COUNT)], [b"1"])  # 0.9 s after the first publish: still held
        self.rig.clock.advance(0.2)
        self.rig.layer.tick(self.rig.ms)
        self.assertEqual([p[1] for p in self.rig.published(COUNT)], [b"1", b"2"])    # the latest value, once
        self.rig.run(3)
        self.assertEqual(len(self.rig.published(COUNT)), 2)

    def test_a_count_change_after_a_quiet_second_goes_out_at_once(self):
        L = self.rig.learner
        L.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.rig.run(2)
        L.p6_zone("z0001", count=2)
        self.rig.push_states()
        self.assertEqual([p[1] for p in self.rig.published(COUNT)], [b"1", b"2"])

    def test_the_trailing_value_equal_to_the_published_one_publishes_nothing(self):
        L = self.rig.learner
        L.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.rig.clock.advance(0.3)
        L.p6_zone("z0001", count=2)
        self.rig.push_states()
        self.rig.clock.advance(0.3)
        L.p6_zone("z0001", count=1)
        self.rig.push_states()
        self.rig.run(3)
        self.assertEqual([p[1] for p in self.rig.published(COUNT)], [b"1"])

    def test_the_count_is_capped_at_three(self):
        self.rig.learner.p6_zone("z0001", occ=True, count=7)
        self.rig.push_states()
        self.assertEqual(self.rig.last(COUNT), "3")

    def test_the_count_sensor_is_disabled_by_default_so_home_assistant_does_not_create_it(self):
        self.assertIsNone(self.ha.entity("sensor.radar_spot_z0001_count"))
        self.assertTrue(self.ha.enable("sensor.radar_spot_z0001_count"))
        self.assertIsNotNone(self.ha.entity("sensor.radar_spot_z0001_count"))

    def test_nothing_but_availability_is_published_while_the_zones_are_invalid(self):
        self.rig.push_states(valid=False)
        self.rig.cleared()
        L = self.rig.learner
        L.p6_zone("z0001", occ=True, count=2)
        self.rig.push_states(valid=False)
        self.rig.run(3)
        topics = {p[0] for p in self.rig.client.published}
        self.assertFalse(topics & {OCC, COUNT, EVENT, AVAIL}, topics)

    def test_the_resume_republishes_the_values_even_when_they_are_the_same_as_before(self):
        self.rig.push_states(valid=False)
        self.rig.cleared()
        self.rig.push_states(valid=True)
        self.assertEqual(self.rig.last(OCC), "OFF")
        self.assertEqual(self.rig.last(COUNT), "0")
        topics = [p[0] for p in self.rig.client.published if p[0] in (OCC, COUNT, AVAIL, "radar_zones/zone/l0001/occupancy")]
        self.assertEqual(topics[-1], AVAIL)

    def test_the_other_zones_do_not_share_the_state(self):
        self.rig.learner.p6_zone("l0001", occ=True, count=1)
        self.rig.push_states()
        self.assertEqual(self.ha.state("binary_sensor.radar_spot_l0001"), "on")
        self.assertEqual(self.ha.state(OCC_ID), "off")


class AttributesTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"), zone_view("l0001", "", "bed"))
        self.ha = self.rig.ha

    def test_the_six_keys_always(self):
        attrs = self.ha.attrs(OCC_ID)
        self.assertEqual(list(attrs), ["kind", "state_source", "variant_now", "typical_hours", "confidence", "poly_cm"])
        self.assertEqual(attrs["kind"], "dwell")
        self.assertEqual(attrs["state_source"], "live")
        self.assertEqual((attrs["variant_now"], attrs["typical_hours"], attrs["confidence"]), ("", "", None))
        self.assertEqual(attrs["poly_cm"], [[-120, 250], [-40, 250], [-40, 330], [-120, 330]])
        self.assertEqual(self.ha.attrs("binary_sensor.radar_spot_l0001")["kind"], "bed")

    def test_no_clear_delay_ld2412_or_frozen_key(self):
        for key in ("clear_delay", "delay_s", "delay", "ld2412_reliable", "ldr", "frozen", "margin", "name"):
            self.assertNotIn(key, self.ha.attrs(OCC_ID))
        text = self.rig.last(ATTR)
        self.assertNotIn("Desk", text)                                       # no zone name in a topic payload either

    def test_typical_hours_confidence_and_variant_now_come_from_the_zone_state(self):
        self.rig.learner.p6_zone("z0001", typical_hours="wd 09-12,13-17; we 10-22", confidence=0.8649,
                                 variant="wd0917", variants=("wd0917", "we1022"))
        self.rig.push_states()
        attrs = self.ha.attrs(OCC_ID)
        self.assertEqual(attrs["typical_hours"], "wd 09-12,13-17; we 10-22")
        self.assertEqual(attrs["confidence"], 0.86)
        self.assertEqual(attrs["variant_now"], "wd0917")

    def test_confidence_is_null_for_a_label_and_a_zone_without_a_suggestion(self):
        self.assertIsNone(self.ha.attrs("binary_sensor.radar_spot_l0001")["confidence"])
        self.assertIn('"confidence":null', self.rig.last("radar_zones/zone/l0001/attributes"))

    def test_attributes_are_published_only_when_they_change(self):
        n = len(self.rig.published(ATTR))
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.rig.learner.p6_zone("z0001", occ=False)
        self.rig.push_states()
        self.assertEqual(len(self.rig.published(ATTR)), n)
        self.rig.learner.p6_zone("z0001", variant="we1022")
        self.rig.push_states()
        self.assertEqual(len(self.rig.published(ATTR)), n + 1)

    def test_a_modified_zone_republishes_its_outline(self):
        self.rig.learner.p5_zones = (zone_view("z0001", "Desk", rev=2, poly=((0, 0), (10, 0), (10, 10))),
                                     zone_view("l0001", "", "bed"))
        self.rig.feed(ReviewChanged((), None))                                # an answer changed a zone: the views are read
        self.assertEqual(self.ha.attrs(OCC_ID)["poly_cm"], [[0, 0], [10, 0], [10, 10]])

    def test_a_polygon_is_cut_to_twelve_vertices_and_rounded(self):
        view = zone_view("z0001", "Desk", poly=tuple((k * 10 + 0.4, k * 5 + 0.6) for k in range(20)))
        obj = json.loads(attribute_payload(view, None, "live"))
        self.assertEqual(len(obj["poly_cm"]), 12)
        self.assertEqual(obj["poly_cm"][1], [10, 6])
        self.assertLess(len(json.dumps(obj)), 1500)                          # spec 8.7

    def test_the_attributes_are_published_while_the_zone_is_unavailable(self):
        self.rig.radar(state="night_off", observing=False)
        self.assertEqual(self.ha.state(OCC_ID), "unavailable")
        self.assertEqual(self.ha.attrs(OCC_ID)["state_source"], "radar_off")


class EventTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"))
        self.ha = self.rig.ha
        self.rig.cleared()

    def test_an_enter_and_a_leave_payload(self):
        self.rig.feed(ZoneEvent("z0001", "enter", "", 1000, 1842, 0, ""))
        self.assertEqual(json.loads(self.rig.last(EVENT)), {"event_type": "enter", "track": "t1842"})
        self.rig.feed(ZoneEvent("z0001", "leave", "edge", 9000, 1842, 5230, "wd0917"))
        self.assertEqual(json.loads(self.rig.last(EVENT)),
                         {"event_type": "leave", "track": "t1842", "dwell_s": 5230, "via": "edge", "variant": "wd0917"})
        self.assertEqual(self.rig.published(EVENT)[0][2:], (1, False))      # not retained
        self.assertEqual([e["event_type"] for e in self.ha.events(EV_ID)], ["enter", "leave"])
        self.assertEqual(self.ha.events(EV_ID)[1]["via"], "edge")           # HA[U6]

    def test_the_key_order_and_the_variant_left_out_when_there_is_none(self):
        self.assertEqual(event_payload(ZoneEvent("z0001", "enter", "", 1, 7, 0, "")), '{"event_type":"enter","track":"t7"}')
        self.assertEqual(event_payload(ZoneEvent("z0001", "enter", "", 1, 7, 0, "we1022")),
                         '{"event_type":"enter","track":"t7","variant":"we1022"}')
        self.assertEqual(event_payload(ZoneEvent("z0001", "leave", "hold_expired", 1, 7, 300, "")),
                         '{"event_type":"leave","track":"t7","dwell_s":300,"via":"hold_expired"}')

    def test_every_via_of_the_core_is_published(self):
        for via in ("edge", "hold_expired", "door_death", "early"):
            self.rig.feed(ZoneEvent("z0001", "leave", via, 1, 1, 10, ""))
            self.assertEqual(json.loads(self.rig.last(EVENT))["via"], via)

    def test_no_zone_name_and_no_coordinate_in_an_event(self):
        self.rig.feed(ZoneEvent("z0001", "leave", "edge", 9000, 12, 60, ""))
        self.assertNotIn("Desk", self.rig.last(EVENT))
        self.assertEqual(set(json.loads(self.rig.last(EVENT))), {"event_type", "track", "dwell_s", "via"})

    def test_an_event_is_not_held_by_the_count_rate_limit_or_the_availability(self):
        self.rig.feed(ZoneEvent("z0001", "enter", "", 1, 1, 0, ""))
        self.rig.feed(ZoneEvent("z0001", "leave", "edge", 2, 1, 1, ""))
        self.rig.feed(ZoneEvent("z0001", "enter", "", 3, 2, 0, ""))
        self.assertEqual(len(self.rig.published(EVENT)), 3)

    def test_an_event_of_a_zone_without_an_entity_is_dropped_and_counted(self):
        self.rig.feed(ZoneEvent("z0099", "enter", "", 1, 1, 0, ""))
        self.assertEqual(self.rig.published("radar_zones/zone/z0099/event"), [])
        self.assertEqual(self.rig.layer.attributes()["zone_events_dropped"], 1)

    def test_an_event_of_a_zone_that_is_new_to_the_shell_pulls_the_views_first(self):
        self.rig.learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("z0002", "Sofa"))
        self.rig.learner.p6_zone("z0002")
        self.rig.feed(ZoneEvent("z0002", "enter", "", 1, 1, 0, ""))
        self.assertEqual(len(self.rig.published("radar_zones/zone/z0002/event")), 1)
        self.assertEqual(self.rig.layer.attributes()["zone_entities"], 2)

    def test_no_event_comes_from_a_resume(self):
        self.rig.radar(state="night_off", observing=False)
        self.rig.push_states(valid=False)
        self.rig.radar(state="on", observing=True)
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states(valid=True)
        self.assertEqual(self.rig.published(EVENT), [])


class RepublishTest(unittest.TestCase):
    def setUp(self):
        self.rig = LayerRig(self)
        self.rig.ready(zone_view("z0001", "Desk"))
        self.rig.learner.p6_zone("z0001", occ=True, count=1)
        self.rig.push_states()
        self.rig.cleared()

    def test_the_order_of_spec_8_6(self):
        self.rig.burst()
        pub = self.rig.client.published
        order = [p[0] for p in pub if p[0].startswith("radar_zones/zone/") or p[0].startswith("homeassistant/") and
                 "radar_spot" in p[0]]
        configs = [t for t in order if t.startswith("homeassistant/")]
        self.assertEqual(len(configs), 6)
        states = [t for t in order if t.startswith("radar_zones/zone/")]
        self.assertEqual(states, [CD, MG, ATTR, OCC, COUNT, AVAIL])
        self.assertLess(max(order.index(c) for c in configs), order.index(CD))       # configs before every state
        self.assertEqual(self.rig.last(AVAIL), "online")
        self.assertEqual(self.rig.last(OCC), "ON")
        self.assertEqual(self.rig.last(COUNT), "1")
        # the retained ones are retained and the others are not
        retained = {p[0]: p[3] for p in pub if p[0].startswith("radar_zones/zone/")}
        self.assertEqual(retained, {CD: True, MG: True, ATTR: False, OCC: False, COUNT: False, AVAIL: True})

    def test_while_invalid_only_the_availability_and_the_not_state_topics_go_out(self):
        self.rig.push_states(valid=False)
        self.rig.cleared()
        self.rig.burst()
        states = [p[0] for p in self.rig.client.published if p[0].startswith("radar_zones/zone/")]
        # 'offline' is also the first thing the burst does (discovery), before any config: see AddonRestartTest
        self.assertEqual(states, [AVAIL, CD, MG, ATTR, AVAIL])
        self.assertEqual(self.rig.last(AVAIL), "offline")

    def test_the_second_pass_repeats_occupancy_count_and_attributes_ten_seconds_after_a_burst(self):
        self.rig.burst()
        self.rig.cleared()
        self.rig.run(9)
        self.assertEqual(self.rig.published(OCC), [])
        self.rig.run(2)
        self.assertEqual([p[1] for p in self.rig.published(OCC)], [b"ON"])
        self.assertEqual([p[1] for p in self.rig.published(COUNT)], [b"1"])
        self.assertEqual(len(self.rig.published(ATTR)), 1)
        self.assertEqual(self.rig.published(AVAIL), [])

    def test_an_entity_created_after_its_config_gets_the_state_from_the_second_pass(self):
        # Home Assistant drops a not retained state that arrives before the entity exists (HA[U1], R-H11)
        self.rig.ha.stop()
        self.rig.ha.start(birth=False)
        self.rig.burst()
        self.assertEqual(self.rig.ha.state(OCC_ID), "on")
        self.assertEqual(self.rig.ha.attrs(OCC_ID)["kind"], "dwell")


class DisconnectedTest(unittest.TestCase):
    def test_a_down_link_is_not_hammered_every_second(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"), zone_view("l0001", "Bed", "bed"))
        attempts = []
        real = rig.layer.zpub._publish

        def counting(*a, **k):
            attempts.append(a[0])
            return real(*a, **k)
        rig.layer.zpub._publish = counting
        rig.connected = False
        rig.learner.p6_zone("z0001", occ=True, count=1)
        rig.learner.p6_zone("l0001", occ=True, count=1)
        rig.push_states()
        rig.run(9)
        self.assertLessEqual(len(attempts), 7)                              # one failing attempt a round, a round every 1.5 s
        rig.connected = True
        rig.burst()                                                         # the connect: everything goes out at once
        self.assertEqual(rig.ha.state("binary_sensor.radar_spot_z0001"), "on")
        self.assertEqual(rig.ha.state("binary_sensor.radar_spot_l0001"), "on")

    def test_what_could_not_be_sent_is_sent_at_the_next_tick(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        rig.connected = False
        rig.learner.p6_zone("z0001", occ=True, count=1)
        rig.push_states()
        rig.run(2)
        self.assertEqual(rig.ha.state(OCC_ID), "off")                        # not delivered: the link is down
        rig.connected = True
        rig.run(2)
        self.assertEqual(rig.ha.state(OCC_ID), "on")


class ContainmentTest(unittest.TestCase):
    def test_a_python_error_in_the_zone_code_never_reaches_the_p5_path(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))

        def boom(*a):
            raise RuntimeError("zone bug")
        rig.layer.zpub.update = boom
        self.assertTrue(rig.layer.on_output(ZoneStates(True, 0, ()), rig.ms))     # consumed, error counted
        self.assertEqual(rig.layer.attributes()["zone_shell_errors"], 1)
        self.assertFalse(rig.layer.on_output(StatusChanged("learning", {}), rig.ms))    # the P5 return value is kept
        self.assertGreaterEqual(rig.layer.attributes()["zone_shell_errors"], 2)
        rig.layer.tick(rig.ms)

    def test_a_core_call_that_fails_gives_none_and_the_layer_keeps_what_it_knew(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        rig.layer.core_call = lambda fn, *a: None                          # the service's core call after a core error
        rig.learner.p5_zones = ()
        rig.run(6)                                                          # the poll gets no answer: the zone stays
        self.assertEqual(rig.layer.attributes()["zone_entities"], 1)
        self.assertIsNotNone(rig.ha.entity("binary_sensor.radar_spot_z0001"))


class EpochHelperTest(unittest.TestCase):
    def test_read_epoch(self):
        L = FakeLearner()
        self.assertEqual(read_epoch(L), 0)
        L.p6_epoch = 3
        self.assertEqual(read_epoch(L), 3)
        self.assertEqual(read_epoch(object()), 0)                           # no zone6 at all
        L.zone6.export = lambda: {"set": {"epoch": -1}}
        self.assertEqual(read_epoch(L), 0)
        L.zone6.export = lambda: {"set": {"epoch": True}}
        self.assertEqual(read_epoch(L), 0)
        L.zone6.export = lambda: []
        self.assertEqual(read_epoch(L), 0)


class StatusAttributesTest(unittest.TestCase):
    def test_counts_only_and_no_name_no_id_no_coordinate(self):
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"), zone_view("l0001", "Secret bed", "bed"))
        attrs = rig.layer.attributes()
        for key in ("zone_entities", "zone_cmds_ignored", "zone_events_sent", "zone_events_dropped", "zone_valve_zones",
                    "zone_shell_errors"):
            self.assertIn(key, attrs)
            self.assertIsInstance(attrs[key], int)
        self.assertEqual(attrs["zone_entities"], 2)
        text = json.dumps(attrs)
        self.assertNotIn("Desk", text)
        self.assertNotIn("Secret", text)
        self.assertNotIn("z0001", text)
        self.assertLess(len(text), 8000)
        # the core's own four additive keys (zones_live and the others) are not shadowed by the shell's
        for key in ("zones_live", "zones_occupied", "zone_events_24h", "zones_over_budget"):
            self.assertNotIn(key, attrs)


if __name__ == "__main__":
    unittest.main()
