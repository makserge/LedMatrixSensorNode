# Led Matrix Node

A custom build based on a "LED Music Spectrum Rhythm Lights Voice Sensor 1624 RGB Atmosphere Level Indicator with Clock Display" panel driven by a Seeed XIAO ESP32-S3 Plus. It shows the time, ambient CO₂/temperature, a live audio spectrum analyzer, scrolling alert messages, or MQTT-pushed pixel-art icons; tracks room presence and people via mmWave radar; and is fully controllable and observable over MQTT (with Home Assistant auto-discovery).

---

## Architecture

The firmware is split into independent FreeRTOS tasks, each pinned to a core and communicating through small, mutex/queue-protected shared state rather than globals:

| Task | Core | Responsibility |
|---|---|---|
| `DisplayTask` | 1 | Owns the matrix, view state machine, CO₂ alarm/beep logic, button input, OTA progress rendering |
| `NtpClockTask` | 1 | Keeps wall-clock time via NTP, pushes formatted strings through a queue |
| `MqttTask` | 1 | Connects/reconnects to the broker, publishes sensor + radar data, dispatches incoming commands |
| `SpectrumAnalyzerTask` | 2 (audio priority) | Decodes the audio stream, runs dual FFTs, drives per-band ballistics |
| `Co2Task`, `TempHumTask`, `LightTask`, `LD2412Task`, `LD2450Task` | 0 | Poll their respective sensors on independent intervals |
| `LedMatrix` internal display engine | 1 | Bit-bangs the panel's HUB75-style interface from double-buffered R/G/B column data |

I²C sensors (SCD4x CO₂, HTU21D temp/humidity, BH1750 light) share the bus behind a single `i2cMutex` (`i2c_mutex.h`) — every I²C transaction, including `begin()` calls, must be wrapped in `xSemaphoreTake`/`Give`.

Rendering follows a **Command Pattern**: each screen lives in its own `src/screen_*.cpp` file (`screen_clock`, `screen_temp`, `screen_co2`, `screen_spectrum`, `screen_alert`, `screen_ota`, `screen_icon`), all declared through one `screen_renderers.h` registry and dispatched from a single `switch` in `displayTask`.

**Background task handles** are centralized in `task_registry.h`/`.cpp` so the whole sensor/MQTT layer can be suspended and resumed as a unit (used during OTA — see below).

---

## Hardware / Pinout

Board: Seeed XIAO ESP32-S3 Plus (Arduino-style `Dx` pin aliases used for most GPIO), driving the 1624 RGB Atmosphere Level Indicator panel's HUB75-style interface.

| Function | Pin(s) |
|---|---|
| Panel RGB data | `R1`=42, `G1`=D3, `B1`=10 |
| Panel clock/latch/enable | `CLK`=D4, `LAT`=D6, `OE`=D5 |
| Panel row address | `A`=D0, `B`=38, `C`=39 |
| Panel chip-selects | `L_EN`=40, `M_EN`=41, `R_EN`=D2 |
| I²C bus (CO₂/temp/light sensors) | `SDA`=12, `SCL`=7 |
| Beeper | 13 |
| Mode button | D9 (`INPUT_PULLUP`) |
| Power toggle button | D10 (`INPUT_PULLUP`) |
| LD2412 presence (digital) | D1 |
| LD2450 UART (Serial1) | `RX`=11, `TX`=D7 @ 256000 baud |

The panel is driven by a **hand-rolled HUB75-style engine** (`led_matrix.cpp`/`.h`): it bit-bangs GPIO registers directly, splits the 24-column panel across 3 chip-select groups of 8, and controls brightness with on-time PWM within a fixed 180µs column window. Text is rendered with a hand-authored 4×14 PROGMEM font (`font_4x14.h`) covering `0–9`, `A–Z`, `:`, `.`, and a degree glyph mapped to `*` in source strings.

---

## Configuration

Copy `secrets.example.h` to `secrets.h` and fill in your own values (git-ignored):

```cpp
static const char* const MQTT_HOST = "192.168.x.x";
static const char* const MQTT_USER = "your_mqtt_user";
static const char* const MQTT_PASS = "your_mqtt_password";
static const char* const AUDIO_STREAM_URL = "http://192.168.x.x:8000/stream.wav";
static const char* const WIFI_BACKUP_SSID = "backup-network";
static const char* const WIFI_BACKUP_PASS = "backup-password";
```

`WIFI_BACKUP_SSID`/`PASS` are optional — leave blank to disable the fallback network.

### Library dependencies

`WiFiManager`, `ElegantOTA`, `ESPAsyncWebServer`, `PubSubClient`, `ArduinoJson`, `Preferences`, `SensirionI2cScd4x`, `HTU21D`, `BH1750`, `LD2450`, and `AudioTools` (with `AudioRealFFT`).

---

## Display & View Management

- **Views:** `VIEW_TIME`, `VIEW_TEMP_DATA`, `VIEW_CO2`, `VIEW_SPECTRUM`, `VIEW_CUSTOM_MSG`, `VIEW_CUSTOM_ICON`.
- **Persistence:** the active view *and* the clock color are stored in NVS (`Preferences`, namespace `"display"`) and restored on boot. Transient views (custom message, icon, CO₂ popup) are set via `setView()` and are **never** persisted — only deliberate mode changes via `selectView()` (button/MQTT) update the boot-time default.
- **Auto-return:** temp/CO₂ auto-return after `DISPLAY_AUTO_RETURN_DELAY_MS` (5s). Scrolling messages and icons expire after their own duration or (for messages) 3 full scroll passes.
- **View arbitration:**
  - Spectrum's automatic entry **cannot interrupt** an active custom message or icon still within its display window.
  - Once the user manually switches to a view other than spectrum (button or MQTT `display/mode`), spectrum's automatic re-entry is **suppressed** until the user manually selects spectrum again — audio staying active no longer yanks the display back.
  - Custom messages and MQTT-pushed icons **restore the view that was active before they interrupted it** (including spectrum) once they expire, rather than always falling back to the clock — the same pattern the CO₂ alarm popup already used. Manually cycling to temp/CO₂ via the button still auto-returns to the clock after 5s, unchanged — that's deliberate browsing, not an interruption.
- **Buttons:** mode button cycles `TIME → TEMP → CO2 → SPECTRUM` (300ms debounce); power button blanks/wakes the panel without stopping any background task.
- **Persistent clock color:** MQTT-settable, survives reboots (see [MQTT Topics](#mqtt-topics)).
- **No artificial startup delay** — `displayTask` initializes the matrix immediately rather than waiting a fixed period before starting.

## RLE Icon Display

Pixel-art icons can be pushed to the panel over MQTT as a compact run-length-encoded binary payload:

**Topic:** `led_matrix_node/display/icon`
**Payload:** repeating runs — `[runLength, r, g, b]`, one byte each — covering up to 384 pixels (24×16) in row-major order. Colors are quantized to the panel's native 3-bit palette (each channel thresholds at >127).

Two payload formats are accepted, auto-detected:

- **ASCII hex string** (recommended — plain text, works from MQTT Explorer's payload box or Home Assistant's `mqtt.publish` service with no binary handling needed):
  ```
  C0000000C0FFFFFF
  ```
  (top half of the panel black, bottom half white — 192 black pixels + 192 white pixels = 384 total)

  ```yaml
  # Home Assistant automation action
  action: mqtt.publish
  data:
    topic: led_matrix_node/display/icon
    payload: "C0000000C0FFFFFF"
  ```

- **Raw binary** is also accepted (auto-detected) for programmatic publishing, but the hex-string format above is recommended for all use cases — it works identically from a script, MQTT Explorer, or Home Assistant with no extra encoding step.

The icon is buffered server-side and **redrawn every frame** while `VIEW_CUSTOM_ICON` is active (it does not persist as the boot default, and is protected from spectrum's automatic re-entry the same way custom messages are), then auto-returns to the previous rotation after `ICON_DISPLAY_DURATION_MS` (15s).

> ⚠️ **If using raw binary instead of the hex format above:** `mosquitto_pub -m "\xc0\x00..."` does **not** interpret `\xNN` as byte escapes — it sends those characters literally as ASCII text. Use `printf '\xc0\x00...' | mosquitto_pub -t ... -l` (or `-f` with a file), or build the payload as real bytes in a script. This is exactly the trap the hex-string format above avoids — prefer it unless you have a specific reason to send raw binary.

### Example icons

Two ready-to-use icons for a washing-machine status use case — publish either directly to `led_matrix_node/display/icon`.

**Washing in progress:**
```
.....#############......
.....#...........#......
.....#...........#......
.....#...........#......
.....#...........#......
.....#############......
.....#...........#......
.....#.....#.....#......
.....#....#.#....#......
.....#...#...#...#......
.....#..#.....#..#......
.....#...#GGG#...#......
.....#....#G#....#......
.....#.....#.....#......
.....#...........#......
.....#############......
```
```
050000000D00FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000D00FF000B0000000100FF000B0000000100FF000B0000000100FF00050000000100FF00050000000100FF000B0000000100FF00040000000100FF00010000000100FF00040000000100FF000B0000000100FF00030000000100FF00030000000100FF00030000000100FF000B0000000100FF00020000000100FF00050000000100FF00020000000100FF000B0000000100FF00030000000100FF00030000FF0100FF00030000000100FF000B0000000100FF00040000000100FF00010000FF0100FF00040000000100FF000B0000000100FF00050000000100FF00050000000100FF000B0000000100FF000B0000000100FF000B0000000D00FF0006000000
```

**Washing complete** (same machine, shifted left with a checkmark badge alongside):
```
.#############..........
.#...........#..........
.#...........#..........
.#...........#..........
.#...........#........##
.#############.......##.
.#...........#.......##.
.#.....#.....#......##..
.#....#.#....#......##..
.#...#...#...#..#..##...
.#..#.....#..#..##.##...
.#...#GGG#...#...###....
.#....#G#....#...###....
.#.....#.....#....#.....
.#...........#..........
.#############..........
```
```
010000000D00FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF000B0000000100FF00080000000200FF00010000000D00FF00070000000200FF00020000000100FF000B0000000100FF00070000000200FF00020000000100FF00050000000100FF00050000000100FF00060000000200FF00030000000100FF00040000000100FF00010000000100FF00040000000100FF00060000000200FF00030000000100FF00030000000100FF00030000000100FF00030000000100FF00020000000100FF00020000000200FF00040000000100FF00020000000100FF00050000000100FF00020000000100FF00020000000200FF00010000000200FF00040000000100FF00030000000100FF00030000FF0100FF00030000000100FF00030000000300FF00050000000100FF00040000000100FF00010000FF0100FF00040000000100FF00030000000300FF00050000000100FF00050000000100FF00050000000100FF00040000000100FF00060000000100FF000B0000000100FF000B0000000D00FF000A000000
```

(`#`=green outline, `G`=blue door accent — `.` renders as black/off)

## NTP Clock

Syncs via `configTzTime` against `pool.ntp.org`. Formats `HH:MM` with a blinking colon; shows `SYNC` until the first successful fix.

## Environmental Sensors

- **CO₂ (Sensirion SCD4x):** 0-ppm glitch rejection, watchdog restart on stalled measurement cycles.
- **Temperature/Humidity (HTU21D):** sanity-bounded reads (`hum < 101%`, `temp > -40°C`).
- **Ambient Light (BH1750):** drives automatic matrix brightness via a smoothed exponential curve, low-pass filtered to avoid visible jumps.

## CO₂ Alarm System

Hysteresis band between `CO2_WARNING_THRESHOLD` (1000ppm) and `CO2_ALARM_LOW_THRESHOLD` (900ppm); a recurring red popup with quiet-hours-gated beeping (08:00–22:00), safety-timeout-bounded so it can never get stuck on screen.

## Audio Spectrum Analyzer

Dual-resolution FFT (2048-pt bass / 1024-pt mid-high) driving a 24-column VU-style display with Gaussian-weighted log-frequency bands, noise gating, compression, adaptive AGC, and tilt EQ. Auto-enters `VIEW_SPECTRUM` on active audio (respecting the view-arbitration rules above) and fades to sleep on sustained silence.

**Peak-dot physics** are ported from [`audioMotion-analyzer`](https://github.com/hvianna/audioMotion-analyzer)'s gravity-based fall model: rather than dropping at a fixed rate, each band's peak indicator accelerates continuously after `BALLISTICS_PEAK_HOLD_MS` (700ms hold), governed by `SPECTRUM_PEAK_GRAVITY_STEPS_PER_S2` — tune this constant to make peaks fall faster/slower/more "floaty". The audio transport (HTTP stream ingestion, dual-FFT band mapping) and silence-detection state machine (rolling average, sleep-fade, presence-aware view auto-return) are unchanged from the existing design, which was already a log-frequency, Gaussian-weighted mapping in its own right.

## mmWave Radar

**LD2412** — binary presence on a single GPIO. **LD2450** — up to 3 tracked targets over UART with a sign-fix for its two's-complement quirk and a watchdog that fully re-inits the link on stall (UART desync doesn't self-heal the way the frame-redrawn matrix does).

## WiFi & OTA

- `WiFiManager` captive portal (180s timeout) for first-time setup, followed by an automatic **backup network** attempt (`WIFI_BACKUP_SSID`/`PASS`, `WIFI_BACKUP_TIMEOUT_MS` = 15s) if the primary/portal path fails, before finally restarting.
- Post-connect, the panel shows the assigned IP address (green, 15s) — protected from being interrupted by spectrum auto-entry.
- **OTA updates (`ElegantOTA` at `/update`)** now take over the whole node:
  - The panel shows a static **"OTA"** for the entire transfer, then automatically resumes whatever view was active beforehand — no explicit "restore" logic needed, since the underlying view state is simply untouched while the OTA flag is set.
  - All background tasks are suspended for the duration of the update and resumed afterward — sensors/MQTT (`task_registry.h`) *and* the audio pipeline (stream ingestion, ballistics, spectrum view-switching), freeing CPU and I²C/UART/network bandwidth for the flash write.
  - Buttons, CO₂ alarm logic, and normal view rendering are all paused until the update finishes.
  - **`LedMatrix::displayTaskEngine` is `IRAM_ATTR`, and its pin toggling uses `gpio_ll_set_level()` instead of `digitalWrite()`.** ESP32 flash writes briefly disable both cores' instruction cache; `IRAM_ATTR` on the function alone isn't sufficient, since `digitalWrite()` is a real call out to code that may itself live in flash — if that call stalls mid-refresh with OE left blanked, the panel visibly blinks for the stall's duration. `gpio_ll_set_level()` is a `static inline` register accessor with no external call, so it inherits the caller's IRAM residency and stays safe throughout an OTA write.
- `/wifi-reset` clears saved WiFi credentials and reboots into the config portal.

---

## MQTT Topics

### Commands (subscribe)

| Topic | Payload | Effect | Example |
|---|---|---|---|
| `led_matrix_node/display/mode` | `time` \| `temp` \| `co2` \| `spectrum` | Selects a view, persists across reboots | `mosquitto_pub -t led_matrix_node/display/mode -m "co2"` |
| `led_matrix_node/display/message` | text (≤31 chars, auto-uppercased) | Scrolling red alert, up to 15s or 3 passes | `mosquitto_pub -t led_matrix_node/display/message -m "Package delivered"` |
| `led_matrix_node/display/power` | `1` \| `0` | Panel on/off | `mosquitto_pub -t led_matrix_node/display/power -m "0"` |
| `led_matrix_node/display/color` | `RRGGBB` / `#RRGGBB` / `r,g,b` | Sets and persists the clock color | `mosquitto_pub -t led_matrix_node/display/color -m "FF8800"` |
| `led_matrix_node/display/icon` | RLE hex string (see above) | Shows a pixel-art icon for 15s | see examples above |
| `led_matrix_node/beeper` | any | Single 100ms beep | `mosquitto_pub -t led_matrix_node/beeper -m "1"` |

### State (publish)

| Topic | Format | Interval | Notes |
|---|---|---|---|
| `led_matrix_node/sensors/state` | `{"co2":812,"temp":21.4,"hum":47.2,"lux":18.6}` | 10s | Skipped while `co2 == 0` |
| `led_matrix_node/sensors/status` | `online` / `offline` | on connect / LWT | Retained |
| `led_matrix_node/radar/ld2412` | `1` / `0` | 1s | Retained |
| `led_matrix_node/radar/ld2450` | `{"targets":[...],"count":N}` | 1s | Up to 3 targets |

```bash
mosquitto_sub -v -t 'led_matrix_node/#'
```

### Home Assistant Auto-Discovery

Retained discovery configs are published under `homeassistant/sensor/Led_Matrix_Node_{id}/config` for `co2`, `temp`, `hum`, `lux` on every broker connection.

---

## Key Tunables (`constants.h`)

| Category | Constant | Value |
|---|---|---|
| Display | `DISPLAY_AUTO_RETURN_DELAY_MS` | 5000ms |
| WiFi | `WIFI_CONFIG_TIMEOUT` / `WIFI_BACKUP_TIMEOUT_MS` | 180s / 15000ms |
| WiFi | `WIFI_IP_DISPLAY_DURATION_MS` | 15000ms |
| OTA | `OTA_TEXT_DISPLAY_MS` | 2000ms |
| Icon | `ICON_DISPLAY_DURATION_MS` | 15000ms |
| CO₂ Alarm | `CO2_WARNING_THRESHOLD` / `CO2_ALARM_LOW_THRESHOLD` | 1000 / 900 ppm |
| CO₂ Alarm | `CO2_ALARM_START_HOUR`–`END_HOUR` | 08:00–22:00 |
| Matrix | `MATRIX_WIDTH` / `MATRIX_HEIGHT` | 24 / 16 |
| MQTT | `MQTT_PUBLISH_INTERVAL` / `MQTT_RADAR_PUBLISH_INTERVAL` | 10000ms / 1000ms |
| Radar | `LD2450_DATA_TIMEOUT_MS` | 10000ms |
| Spectrum | `SPECTRUM_AGC_TARGET_LED`, `SPECTRUM_TILT_DB_PER_OCTAVE` | 8.0, 3dB/oct |
| Spectrum | `SPECTRUM_PEAK_GRAVITY_STEPS_PER_S2` | 220.0 (steps/s²) |

See `constants.h` for the full list.

---

## Security Notes

- `secrets.h` contains live credentials (primary + backup WiFi, MQTT, audio stream URL) — keep it out of version control.
- The web UI (`/`, `/update`, `/wifi-reset`) is unauthenticated on the local network.