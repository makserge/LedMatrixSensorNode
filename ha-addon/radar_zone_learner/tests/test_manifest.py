"""Packaging checks (spec 3, D30): manifest values by line, Dockerfile, run.sh, LF endings, file names."""
import os
import re
import unittest
from pathlib import Path, PurePosixPath

ADDON = Path(__file__).resolve().parents[1]

CONFIG_LINES = [
    'name: "Radar Zone Learner"',
    'version: "0.5.0"',
    'slug: "radar_zone_learner"',
    "description: \"Learns places from the LED matrix node's LD2454 radar frames (P5: review and "
    "suggestions, shadow mode).\"",
    "arch:",
    "  - aarch64",
    "  - amd64",
    "init: false",
    "startup: application",
    "boot: auto",
    "timeout: 20",
    "services:",
    '  - "mqtt:need"',
    'watchdog: "tcp://[HOST]:[PORT:8099]"',
    "backup: hot",
    "backup_exclude:",
    '  - "capture/*"',
    "options:",
    "  log_level: info",
    "  node_prefix: led_matrix_node",
    "  discovery_prefix: homeassistant",
    "  debug_map: true",
    "  night_lux: 5.0",
    "schema:",
    '  log_level: "list(debug|info|warning|error)"',
    "  node_prefix: 'match(^[A-Za-z0-9_/-]{1,64}$)'",
    "  discovery_prefix: 'match(^[A-Za-z0-9_-]{1,64}$)'",
    "  debug_map: bool",
    '  night_lux: "float(0.1,500)"',
    '  mqtt_host: "str?"',
    '  mqtt_port: "port?"',
    '  mqtt_username: "str?"',
    '  mqtt_password: "password?"',
]
FORBIDDEN_KEYS = ("ports", "host_network", "hassio_api", "homeassistant_api", "map", "privileged", "image")


def addon_files():
    for root, dirs, files in os.walk(ADDON):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for name in files:
            yield Path(root) / name


class ManifestTest(unittest.TestCase):
    def test_config_yaml_lines(self):
        lines = (ADDON / "config.yaml").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, CONFIG_LINES)
        top_keys = {line.split(":", 1)[0] for line in lines if line and not line.startswith(" ")}
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, top_keys)

    def test_backup_exclude_matches_the_capture_files_and_nothing_else(self):
        # The Supervisor tests each exclude entry with PurePath.match against the file's full path below its own data
        # folder (relative patterns match from the right, absolute ones need the whole path): '/data/capture/**'
        # matches nothing there, 'capture/*' matches the capture files and not the state.
        lines = (ADDON / "config.yaml").read_text(encoding="utf-8").splitlines()
        i = lines.index("backup_exclude:")
        entries = []
        for ln in lines[i + 1:]:
            if not ln.startswith("  - "):
                break
            entries.append(ln[4:].strip().strip('"'))
        self.assertEqual(len(entries), 1)
        pattern = entries[0]
        self.assertFalse(pattern.startswith("/"))
        data = PurePosixPath("/data/apps/data/local_radar_zone_learner")
        self.assertTrue((data / "capture" / "capture-20260101-00.jsonl").match(pattern))
        for name in ("state.json", "rf_stats.json", "options.json"):
            self.assertFalse((data / name).match(pattern), name)
        self.assertFalse((data / "snapshots" / "state-2026-01-01.json").match(pattern))

    def test_options_match_translations(self):
        text = (ADDON / "translations" / "en.yaml").read_text(encoding="utf-8")
        keys = re.findall(r"^  ([a-z_]+):$", text, re.M)
        self.assertEqual(keys, ["log_level", "node_prefix", "discovery_prefix", "debug_map", "night_lux",
                                "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password"])
        self.assertEqual(text.count("    name: "), 9)
        self.assertEqual(text.count("    description: "), 9)

    def test_dockerfile(self):
        lines = (ADDON / "Dockerfile").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24")
        self.assertEqual([ln for ln in lines if ln.startswith("FROM")], [lines[0]])
        self.assertNotIn("BUILD_FROM", "\n".join(lines))
        self.assertIn("RUN pip3 install --no-cache-dir --require-hashes -r /tmp/requirements.txt "
                      "&& rm /tmp/requirements.txt", lines)
        self.assertIn('CMD [ "/run.sh" ]', lines)
        self.assertIn("    S6_KILL_GRACETIME=10000 \\", lines)
        self.assertFalse((ADDON / "build.yaml").exists())        # deprecated (D2)

    def test_run_sh(self):
        lines = (ADDON / "run.sh").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#!/command/with-contenv bashio")
        self.assertEqual(lines[-1], "exec python3 -u -B -m radar_zone_learner")
        text = "\n".join(lines)
        self.assertIn('bashio::services.available "mqtt"', text)
        for key in ("host", "port", "username", "password", "ssl"):
            self.assertIn(f"bashio::services mqtt '{key}'", text)
        self.assertNotIn("log.info \"$RZL_MQTT_PASSWORD", text)
        self.assertNotIn("set -x", text)

    def test_no_carriage_returns(self):
        checked = [ADDON / "run.sh", ADDON / "Dockerfile", ADDON / "config.yaml"]
        checked += [p for p in addon_files() if p.suffix == ".py"]
        self.assertGreater(len(checked), 10)
        bad = [str(p.relative_to(ADDON)) for p in checked if b"\r" in p.read_bytes()]
        self.assertEqual(bad, [])

    def test_no_second_manifest(self):
        names = {"config.yaml", "config.yml", "config.json"}
        found = [p.relative_to(ADDON).as_posix() for p in addon_files() if p.name in names]
        self.assertEqual(found, ["config.yaml"])

    def test_requirements_pinned_with_two_hashes(self):
        text = (ADDON / "requirements.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("paho-mqtt==2.1.0 \\\n"))
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})\b", text)
        self.assertEqual(len(hashes), 2)
        self.assertEqual(len(set(hashes)), 2)

    def test_small_files(self):
        self.assertEqual((ADDON / ".gitattributes").read_text(encoding="utf-8").splitlines(),
                         ["* text=auto eol=lf", "*.png binary"])
        ignored = (ADDON / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for entry in ("tests/", "replay/", "*.md", "**/__pycache__", ".git*"):
            self.assertIn(entry, ignored)
        self.assertIn("__pycache__/", (ADDON / ".gitignore").read_text(encoding="utf-8").splitlines())
        notices = (ADDON / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("paho-mqtt 2.1.0", notices)
        self.assertIn("No code from these projects is included.", notices)
        self.assertIn("0.3.0 - P3 shadow mode", (ADDON / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIn("0.4.0 - P4 night window", (ADDON / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIn("0.5.0 - P5 setup review", (ADDON / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_version_consistent(self):
        from radar_zone_learner.core.types import APP_VERSION
        self.assertIn(f'version: "{APP_VERSION}"', (ADDON / "config.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
