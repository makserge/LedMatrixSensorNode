import json
import unittest
from pathlib import Path

from radar_zone_learner.shell.options import (DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT, SettingsError,
                                              load_settings)
from tests._tmp import tmpdir
from tests.fakes import quiet_logs

SERVICE_ENV = {
    "RZL_MQTT_HOST": "core-mosquitto",
    "RZL_MQTT_PORT": "1883",
    "RZL_MQTT_USERNAME": "addons",
    "RZL_MQTT_PASSWORD": "s3cret-pw",
    "RZL_MQTT_SSL": "false",
}


class OptionsTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self._tmp = tmpdir()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "options.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, obj):
        self.path.write_text(json.dumps(obj), encoding="utf-8")

    def test_defaults_when_keys_missing(self):
        self.write({})
        s = load_settings(self.path, environ={})
        self.assertEqual(s.log_level, "info")
        self.assertEqual(s.node_prefix, "led_matrix_node")
        self.assertEqual(s.discovery_prefix, "homeassistant")
        self.assertIs(s.debug_map, True)
        self.assertEqual((s.mqtt_host, s.mqtt_port), (DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT))
        self.assertIsNone(s.mqtt_username)
        self.assertIsNone(s.mqtt_password)
        self.assertIs(s.mqtt_ssl, False)
        self.assertEqual(s.data_dir, self.dir)

    def test_missing_file_gives_defaults(self):
        s = load_settings(self.dir / "absent.json", environ={})
        self.assertEqual(s.node_prefix, "led_matrix_node")

    def test_values_from_file(self):
        self.write({"log_level": "debug", "node_prefix": "lab/node_2", "discovery_prefix": "ha",
                    "debug_map": False})
        s = load_settings(self.path, environ={})
        self.assertEqual((s.log_level, s.node_prefix, s.discovery_prefix, s.debug_map),
                         ("debug", "lab/node_2", "ha", False))

    def test_environment_read_and_removed(self):
        self.write({})
        env = dict(SERVICE_ENV, PATH="/usr/bin", RZL_MQTT_EXTRA="x")
        s = load_settings(self.path, environ=env)
        self.assertEqual((s.mqtt_host, s.mqtt_port, s.mqtt_username, s.mqtt_password, s.mqtt_ssl),
                         ("core-mosquitto", 1883, "addons", "s3cret-pw", False))
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_ssl_true_from_service(self):
        self.write({})
        s = load_settings(self.path, environ=dict(SERVICE_ENV, RZL_MQTT_SSL="true", RZL_MQTT_PORT="8883"))
        self.assertIs(s.mqtt_ssl, True)
        self.assertEqual(s.mqtt_port, 8883)

    def test_password_not_in_repr(self):
        self.write({})
        s = load_settings(self.path, environ=dict(SERVICE_ENV))
        self.assertNotIn("s3cret-pw", repr(s))

    def test_option_override_without_run_sh(self):
        self.write({"mqtt_host": "192.168.1.10", "mqtt_port": 1884, "mqtt_username": "learner",
                    "mqtt_password": "pw2"})
        s = load_settings(self.path, environ={})
        self.assertEqual((s.mqtt_host, s.mqtt_port, s.mqtt_username, s.mqtt_password),
                         ("192.168.1.10", 1884, "learner", "pw2"))

    def test_environment_wins_over_options(self):
        # run.sh already chose between the option override and the service; its choice is final.
        self.write({"mqtt_host": "elsewhere", "mqtt_username": "x"})
        s = load_settings(self.path, environ=dict(SERVICE_ENV))
        self.assertEqual((s.mqtt_host, s.mqtt_username), ("core-mosquitto", "addons"))

    def test_empty_username_is_none(self):
        self.write({})
        s = load_settings(self.path, environ=dict(SERVICE_ENV, RZL_MQTT_USERNAME="", RZL_MQTT_PASSWORD=""))
        self.assertIsNone(s.mqtt_username)
        self.assertIsNone(s.mqtt_password)

    def test_invalid_values(self):
        cases = [
            {"log_level": "verbose"},
            {"log_level": 3},
            {"node_prefix": "bad prefix"},
            {"node_prefix": ""},
            {"node_prefix": "x" * 65},
            {"discovery_prefix": "home/assistant"},
            {"debug_map": "yes"},
            {"mqtt_port": 70000},
            {"mqtt_port": True},
        ]
        for opts in cases:
            with self.subTest(opts=opts):
                self.write(opts)
                with self.assertRaises(SettingsError):
                    load_settings(self.path, environ={})

    def test_invalid_environment(self):
        self.write({})
        for key, value in (("RZL_MQTT_PORT", "abc"), ("RZL_MQTT_PORT", "0"), ("RZL_MQTT_SSL", "maybe")):
            with self.subTest(key=key, value=value):
                env = dict(SERVICE_ENV, **{key: value})
                with self.assertRaises(SettingsError) as cm:
                    load_settings(self.path, environ=env)
                self.assertNotIn("s3cret-pw", str(cm.exception))
                self.assertFalse(any(k.startswith("RZL_MQTT_") for k in env))

    def test_invalid_json(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.path, environ={})
        self.path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.path, environ={})


if __name__ == "__main__":
    unittest.main()
