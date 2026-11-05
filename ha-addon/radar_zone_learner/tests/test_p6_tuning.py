"""The tuning entities, the Forget button and the variant request switches over MQTT (check P6-12, spec 8.2, 8.5, D14, D15, D38,
D23): the commands go through the real mapper, the service and the layer to the (fake) core and come back as echoes."""
import unittest

from radar_zone_learner.core.types import (SaveRequested, StatusChanged, VariantRequest, ZoneForget, ZoneTune)
from radar_zone_learner.shell.p6_types import ZoneTune as ShellZoneTune
from tests.fakes import FakeLearner
from tests.p6_harness import P6Harness, zone_view

NUM = "number.radar_spot_z0001_clear_delay"
MG = "number.radar_spot_z0001_margin"
BTN = "button.radar_spot_z0001_forget"
SW = "switch.radar_spot_z0001_vreq_wd0917"
OCC = "binary_sensor.radar_spot_z0001"
CD_SET = "radar_zones/zone/z0001/clear_delay/set"
CD = "radar_zones/zone/z0001/clear_delay"


class TuningBase(P6Harness):
    def make_learner(self):
        learner = FakeLearner()
        learner.p5_zones = (zone_view("z0001", "Desk"), zone_view("l0001", "Bed", "bed"))
        learner.p6_zone("z0001", variants=("wd0917", "we1022"))
        learner.p6_zone("l0001", delay_s=600)
        return learner

    def up(self):
        """The node is online, the radars are on and observed, the core is valid."""
        node = self.connect(sched=False)
        self.node_says("led_matrix_node/sensors/status", "online")
        self.node_says("led_matrix_node/radar/state", "on")
        self.node_says("led_matrix_node/radar/observing", "ON")
        self.learner.p6_valid = True
        self.learner.tick_outputs = [StatusChanged("learning", {}), self.learner.p6_snapshot()]
        self.run_for(12)
        return node

    def cmds(self, kind):
        return [e for e in self.learner.events if isinstance(e, kind)]


class TuningTest(TuningBase, unittest.TestCase):
    def test_a_clear_delay_command_reaches_the_core_and_is_echoed_as_the_delay_in_force(self):
        self.up()
        self.assertEqual(self.ha.state(NUM), "300")
        self.ha.number_set(NUM, 90)
        self.pump()
        self.assertEqual([(e.zid, e.field, e.value) for e in self.cmds(ZoneTune)], [("z0001", "cd", 90)])
        self.assertEqual(self.ha.state(NUM), "90")
        self.assertEqual(self.last(CD), "90")
        self.assertEqual(self.published(CD)[-1][2:], (1, True))                                  # retained
        self.assertEqual(self.learner.p6_zones["z0001"].delay_src, "manual")
        self.assertEqual(self.learner.zone_tune("z0001"), {"cd": 90})

    def test_a_margin_command_with_a_negative_value(self):
        self.up()
        self.ha.number_set(MG, -25)
        self.pump()
        self.assertEqual([(e.field, e.value) for e in self.cmds(ZoneTune)], [("mg", -25)])
        self.assertEqual(self.ha.state(MG), "-25")
        self.assertEqual(self.last("radar_zones/zone/z0001/margin"), "-25")

    def test_a_label_zone_can_be_tuned_too(self):
        self.up()
        self.client.fire_message("radar_zones/zone/l0001/clear_delay/set", "120")
        self.pump()
        self.assertEqual([(e.zid, e.value) for e in self.cmds(ZoneTune)], [("l0001", 120)])
        self.assertEqual(self.ha.state("number.radar_spot_l0001_clear_delay"), "120")

    def test_the_range_ends_are_accepted(self):
        self.up()
        for value in (3, 900):
            self.ha.number_set(NUM, value)
        self.ha.number_set(MG, -50)
        self.ha.number_set(MG, 100)
        self.pump()
        self.assertEqual([e.value for e in self.cmds(ZoneTune)], [3, 900, -50, 100])

    def test_out_of_range_non_numeric_and_unknown_zone_commands_never_reach_the_core(self):
        self.up()
        for topic, payload in ((CD_SET, "2"), (CD_SET, "901"), (CD_SET, "abc"), (CD_SET, ""), (CD_SET, "1e2"),
                               ("radar_zones/zone/z0001/margin/set", "101"), ("radar_zones/zone/z0001/margin/set", "-51"),
                               ("radar_zones/zone/z0099/clear_delay/set", "60"), ("radar_zones/zone/foo/forget", "PRESS"),
                               ("radar_zones/zone/z0001/forget", "ON"), ("radar_zones/zone/z0001/wd9999/entity/set", "ON"),
                               ("radar_zones/zone/z0001/we1111/entity/set", "ON"), ("radar_zones/zone/l0001/wd0917/entity/set", "ON"),
                               ("radar_zones/zone/z0001/wd0917/entity/set", "maybe")):
            self.client.fire_message(topic, payload)
        self.pump()
        self.assertEqual([e for e in self.learner.events if isinstance(e, (ZoneTune, ZoneForget, VariantRequest))], [])
        self.assertEqual(self.layer.attributes()["zone_cmds_ignored"], 14)           # the mapper's drops and the layer's
        self.assertEqual(self.mqtt.mapper.zone_cmds_ignored, 14)
        self.assertEqual(self.ha.state(NUM), "300")

    def test_a_retained_command_is_ignored_and_deleted(self):
        self.up()
        self.client.published.clear()
        self.client.fire_message(CD_SET, "60", retain=True)
        self.pump()
        self.assertEqual(self.cmds(ZoneTune), [])
        self.assertEqual(self.published(CD_SET), [(CD_SET, b"", 1, True)])
        self.assertEqual(self.ha.state(NUM), "300")

    def test_a_tuning_command_is_accepted_while_the_radars_are_off(self):
        self.up()
        self.node_says("led_matrix_node/radar/state", "night_off")
        self.node_says("led_matrix_node/radar/observing", "OFF")
        self.learner.p6_valid = False
        self.learner.tick_outputs = [self.learner.p6_snapshot()]
        self.run_for(2)
        self.assertEqual(self.ha.state(OCC), "unavailable")
        self.assertEqual(self.ha.state(NUM), "300")                                               # a config entity stays available
        self.ha.number_set(NUM, 45)
        self.pump()
        self.assertEqual([(e.field, e.value) for e in self.cmds(ZoneTune)], [("cd", 45)])
        self.assertEqual(self.ha.state(NUM), "45")
        self.assertEqual(self.ha.state(OCC), "unavailable")

    def test_a_forget_press_reaches_the_core_and_the_override_is_gone(self):
        self.up()
        self.ha.number_set(NUM, 90)
        self.pump()
        self.assertEqual(self.ha.state(NUM), "90")
        self.ha.press(BTN)
        self.pump()
        self.assertEqual([e.zid for e in self.cmds(ZoneForget)], ["z0001"])
        self.assertEqual(self.ha.state(NUM), "300")                                               # back to the default
        self.assertEqual(self.last(CD), "300")
        self.assertEqual(self.learner.p6_zones["z0001"].delay_src, "default")
        self.assertEqual(self.learner.zone_tune("z0001"), {})
        self.assertIsNotNone(self.ha.entity(OCC))                                                  # the zone stays (D15)

    def test_a_forget_press_while_the_radars_are_off_is_applied_too(self):
        self.up()
        self.node_says("led_matrix_node/radar/observing", "OFF")
        self.pump()
        self.ha.press(BTN)
        self.pump()
        self.assertEqual(len(self.cmds(ZoneForget)), 1)

    def test_the_zone_save_reason_is_requested_after_a_change(self):
        self.up()
        self.ha.number_set(NUM, 90)
        self.pump()
        self.assertTrue((self.dir / "state.json").exists())

    def test_the_tuning_state_is_retained_and_comes_back_after_a_home_assistant_restart(self):
        self.up()
        self.ha.number_set(NUM, 90)
        self.pump()
        self.ha.stop()
        self.ha.start()                                                                           # the birth message reaches the app
        self.run_for(14)
        self.assertEqual(self.ha.state(NUM), "90")


class VariantRequestTest(TuningBase, unittest.TestCase):
    def test_the_switch_asks_for_the_variant_sensor_and_off_removes_it(self):
        self.up()
        self.assertEqual(self.ha.state(SW), "off")
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))
        self.ha.switch_set(SW, True)
        self.pump()
        self.assertEqual([(e.zid, e.variant, e.on) for e in self.cmds(VariantRequest)], [("z0001", "wd0917", True)])
        self.assertEqual(self.ha.state(SW), "on")
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))
        self.ha.switch_set(SW, False)
        self.pump()
        self.assertEqual(self.ha.state(SW), "off")
        self.assertIsNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))

    def test_the_request_survives_a_home_assistant_restart(self):
        self.up()
        self.ha.switch_set(SW, True)
        self.pump()
        self.ha.stop()
        self.ha.start()
        self.run_for(14)
        self.assertEqual(self.ha.state(SW), "on")
        self.assertIsNotNone(self.ha.entity("binary_sensor.radar_spot_z0001_v_wd0917"))

    def test_the_sensor_is_on_while_the_zone_is_occupied_in_that_variant(self):
        self.up()
        self.ha.switch_set(SW, True)
        self.pump()
        self.learner.p6_zone("z0001", occ=True, count=1, variant="wd0917")
        self.learner.tick_outputs = [self.learner.p6_snapshot()]
        self.run_for(1)
        self.assertEqual(self.ha.state("binary_sensor.radar_spot_z0001_v_wd0917"), "on")
        self.learner.p6_zone("z0001", variant="")
        self.learner.tick_outputs = [self.learner.p6_snapshot()]
        self.run_for(1)
        self.assertEqual(self.ha.state("binary_sensor.radar_spot_z0001_v_wd0917"), "off")
        self.assertEqual(self.ha.state(OCC), "on")


class SaveRateTest(TuningBase, unittest.TestCase):
    def test_the_zone_save_is_written_at_once_and_a_flood_waits_for_the_interval(self):
        self.up()
        self.svc._execute(SaveRequested("zone"))
        first = self.svc.state_saved_ms
        self.assertIsNotNone(first)
        self.clock.advance(2)
        self.svc._execute(SaveRequested("zone"))                                                  # within 10 s: deferred
        self.assertEqual(self.svc.state_saved_ms, first)
        self.assertTrue(self.svc._save_pending)
        self.clock.advance(9)
        self.svc.step(0)
        self.assertGreater(self.svc.state_saved_ms, first)                                        # written together after 10 s
        self.assertFalse(self.svc._save_pending)

    def test_a_review_save_is_still_never_delayed(self):
        self.up()
        self.svc._execute(SaveRequested("zone"))
        first = self.svc.state_saved_ms
        self.clock.advance(1)
        self.svc._execute(SaveRequested("review"))
        self.assertGreater(self.svc.state_saved_ms, first)


class CommandCheckTest(unittest.TestCase):
    """The layer's own check, on the worker thread (the mapper checked it already in the paho thread)."""

    def test_a_value_outside_its_range_or_an_unknown_field_is_dropped_by_the_layer_too(self):
        from tests.p6_harness import LayerRig
        rig = LayerRig(self)
        rig.ready(zone_view("z0001", "Desk"))
        L = rig.layer
        self.assertEqual(len(L.on_event(ZoneTune("z0001", "cd", 3, 0), 0)), 1)
        self.assertEqual(L.on_event(ZoneTune("z0001", "cd", 2, 0), 0), [])
        self.assertEqual(L.on_event(ZoneTune("z0001", "mg", 101, 0), 0), [])
        self.assertEqual(L.on_event(ZoneTune("z0001", "zz", 5, 0), 0), [])
        self.assertEqual(L.on_event(ZoneTune("z0001", "cd", True, 0), 0), [])
        self.assertEqual(L.on_event(ZoneTune("z0002", "cd", 60, 0), 0), [])
        self.assertEqual(L.attributes()["zone_cmds_ignored"], 5)
        self.assertIs(ShellZoneTune, ZoneTune)


if __name__ == "__main__":
    unittest.main()
