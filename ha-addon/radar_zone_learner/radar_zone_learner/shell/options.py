"""App options (/data/options.json) and the MQTT login from the RZL_MQTT_* environment (spec 10.5).

``run.sh`` exports the MQTT login as ``RZL_MQTT_HOST``, ``RZL_MQTT_PORT``, ``RZL_MQTT_USERNAME``,
``RZL_MQTT_PASSWORD`` and ``RZL_MQTT_SSL``, taken either from the app options (when an MQTT user name
is set there) or from the Supervisor's MQTT service. ``load_settings`` reads them once and removes them
from the environment (D32). The password is kept out of ``repr()`` and is never logged.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOG_LEVELS = ("debug", "info", "warning", "error")
DEFAULTS = {
    "log_level": "info",
    "node_prefix": "led_matrix_node",
    "discovery_prefix": "homeassistant",
    "debug_map": True,
    "night_lux": 5.0,
}
DEFAULT_MQTT_HOST = "core-mosquitto"
DEFAULT_MQTT_PORT = 1883
ENV_PREFIX = "RZL_MQTT_"
NIGHT_LUX_MIN = 0.1
NIGHT_LUX_MAX = 500.0
NODE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_/-]{1,64}$")
DISCOVERY_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("", "0", "false", "no", "off")

log = logging.getLogger("radar_zone_learner.options")


class SettingsError(ValueError):
    """Invalid options or MQTT environment; the message never contains the password."""


@dataclass(frozen=True)
class Settings:
    log_level: str
    node_prefix: str
    discovery_prefix: str
    debug_map: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_username: Optional[str]
    mqtt_password: Optional[str] = field(repr=False)
    mqtt_ssl: bool
    data_dir: Path
    night_lux: float = DEFAULTS["night_lux"]


def _port(value, what: str) -> int:
    if isinstance(value, bool):
        raise SettingsError(f"{what} must be a port number 1..65535")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingsError(f"{what} must be a port number 1..65535") from None
    if not 1 <= port <= 65535:
        raise SettingsError(f"{what} must be a port number 1..65535")
    return port


def _bool_text(value: str, what: str) -> bool:
    text = value.strip().casefold()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SettingsError(f"{what} must be true or false")


def _optional_str(value, what: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"{what} must be a string")
    return value if value != "" else None


def _read_options(options_path: Path) -> dict:
    try:
        text = options_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("%s not found: using the default options", options_path)
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(f"cannot read {options_path}: {exc.__class__.__name__}") from None
    try:
        obj = json.loads(text)
    except ValueError:
        raise SettingsError(f"{options_path} is not valid JSON") from None
    if not isinstance(obj, dict):
        raise SettingsError(f"{options_path} is not a JSON object")
    return obj


def load_settings(options_path: Path = Path("/data/options.json"), environ: Optional[dict] = None) -> Settings:
    """Reads the options and the RZL_MQTT_* variables, then deletes those variables from ``environ``.

    ``environ`` defaults to ``os.environ``. Missing option keys take the defaults of spec 3.2; invalid
    values raise ``SettingsError``. The data folder is the folder of ``options_path`` (``/data``).
    """
    options_path = Path(options_path)
    env = os.environ if environ is None else environ
    mqtt_env = {key: env[key] for key in list(env) if key.startswith(ENV_PREFIX)}
    for key in mqtt_env:
        del env[key]
    opts = _read_options(options_path)

    log_level = opts.get("log_level", DEFAULTS["log_level"])
    if not isinstance(log_level, str) or log_level.lower() not in LOG_LEVELS:
        raise SettingsError("log_level must be one of debug, info, warning, error")
    node_prefix = opts.get("node_prefix", DEFAULTS["node_prefix"])
    if not isinstance(node_prefix, str) or not NODE_PREFIX_RE.match(node_prefix):
        raise SettingsError("node_prefix must be 1-64 characters of A-Z a-z 0-9 _ / -")
    discovery_prefix = opts.get("discovery_prefix", DEFAULTS["discovery_prefix"])
    if not isinstance(discovery_prefix, str) or not DISCOVERY_PREFIX_RE.match(discovery_prefix):
        raise SettingsError("discovery_prefix must be 1-64 characters of A-Z a-z 0-9 _ -")
    debug_map = opts.get("debug_map", DEFAULTS["debug_map"])
    if not isinstance(debug_map, bool):
        raise SettingsError("debug_map must be true or false")
    night_lux = opts.get("night_lux", DEFAULTS["night_lux"])
    if (isinstance(night_lux, bool) or not isinstance(night_lux, (int, float)) or not math.isfinite(night_lux)
            or not NIGHT_LUX_MIN <= night_lux <= NIGHT_LUX_MAX):
        raise SettingsError(f"night_lux must be a number from {NIGHT_LUX_MIN:g} to {NIGHT_LUX_MAX:g}")

    if ENV_PREFIX + "HOST" in mqtt_env:
        # run.sh decided between the option override and the Supervisor service.
        host = mqtt_env.get(ENV_PREFIX + "HOST", "").strip() or DEFAULT_MQTT_HOST
        port_text = mqtt_env.get(ENV_PREFIX + "PORT", "").strip()
        port = _port(port_text, "RZL_MQTT_PORT") if port_text else DEFAULT_MQTT_PORT
        username = mqtt_env.get(ENV_PREFIX + "USERNAME") or None
        password = mqtt_env.get(ENV_PREFIX + "PASSWORD") or None
        ssl = _bool_text(mqtt_env.get(ENV_PREFIX + "SSL", ""), "RZL_MQTT_SSL")
    else:
        # Started without run.sh (a local test run): the options' own override, else the defaults.
        host = _optional_str(opts.get("mqtt_host"), "mqtt_host") or DEFAULT_MQTT_HOST
        port = _port(opts["mqtt_port"], "mqtt_port") if opts.get("mqtt_port") is not None else DEFAULT_MQTT_PORT
        username = _optional_str(opts.get("mqtt_username"), "mqtt_username")
        password = _optional_str(opts.get("mqtt_password"), "mqtt_password")
        ssl = False

    return Settings(log_level=log_level.lower(), node_prefix=node_prefix, discovery_prefix=discovery_prefix,
                    debug_map=debug_map, mqtt_host=host, mqtt_port=port, mqtt_username=username,
                    mqtt_password=password, mqtt_ssl=ssl, data_dir=options_path.parent,
                    night_lux=float(night_lux))


def configure_logging(level: str) -> None:
    """Root logger to stdout at ``level``; paho's logger stays at WARNING (spec 10.5)."""
    import sys
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("paho").setLevel(logging.WARNING)
