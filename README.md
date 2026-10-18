# Led Matrix Node

A custom build based on a "LED Music Spectrum Rhythm Lights Voice Sensor 1624 RGB Atmosphere Level Indicator with Clock Display" panel driven by a Seeed XIAO ESP32-S3 Plus. It shows the time, ambient CO₂/temperature, a live audio spectrum analyzer, scrolling alert messages, or MQTT-pushed pixel-art icons; tracks room presence and people via mmWave radar; and is fully controllable and observable over MQTT (with Home Assistant auto-discovery).

---

## Architecture

The firmware is split into FreeRTOS tasks, each pinned to a core. They share state through global objects, each guarded in its own way: the CO₂, temperature/humidity and light readings are atomics (`SensorReading`, `sensor_reading.h`) in global sensor objects, the LD2454 targets sit behind a spinlock and the LD2412 presence is a single `bool`, the view, message, icon and beeper requests sit behind `DisplayManager`'s spinlock, the spectrum levels behind the analyzer's spinlocks, the clock text goes through a queue, and the I²C bus is guarded by a mutex (below).

| Task (name in panics and task lists) | Core | Priority | Stack (bytes) | Runs every | Responsibility |
|---|---|---|---|---|---|
| `MatrixEngine` (`LedMatrix::displayTaskEngine`) | 1 | 5 | 4096 | frame (5 ms) | Bit-bangs the panel's HUB75-style interface from double-buffered R/G/B column data. It busy-waits each column's full 180 µs window (lit part and dark remainder alike, so its CPU use does not depend on brightness) and sleeps one tick per frame: about 4.5 ms of every 5 ms, roughly 90% of core 1 |
| `Ballistics` | 1 | 3 | 3072 | 10 ms | Per-band bar/peak ballistics and sleep fade |
| `DisplayTask` | 1 | 1 | 4096 | 33 ms (10 ms in spectrum, 50 ms panel off / WiFi down, 200 ms during OTA) | Renders the views; view state machine, CO₂ alarm logic, the beeper (alarm and MQTT beeps), button input, OTA screen |
| `NtpClockTask` | 1 | 1 | 3072 | 500 ms | Keeps wall-clock time via NTP, pushes the formatted clock text through a queue |
| `MqttTask` | 1 | 1 | 8192 | 40 ms | Connects/reconnects to the broker, publishes sensor + radar data, dispatches incoming commands |
| `AudioViewSwitch` | 1 | 1 | 4096 | 10 ms | Spectrum auto-entry/return |
| Arduino `loopTask` | 1 | 1 | 8192 | 5 ms | Runs `setup()`, then `loop()`: the web UI / OTA server (`webUi.loop()`) and WiFi link supervision |
| `StreamIngest` | 0 | 4 | 8192 | 1 ms tick | Fetches and parses the WAV audio stream and runs both FFTs (their callbacks run inside its writes to the FFTs) |
| `CO2Task`, `TempHumTask`, `LightTask`, `LD2412Task`, `LD2454Task` | 0 | 1 | 4096 each | 30 s (5 s without a value), 15 s, 2 s, 500 ms, 50 ms | Poll their respective sensors |

Priorities and stack sizes are the `*_TASK_PRIORITY` / `*_TASK_STACK_SIZE` constants in `constants.h` (the Arduino core sets `loopTask`'s). The other tasks on core 1 only run while `MatrixEngine` sleeps between frames, roughly 0.5 ms of every 5 ms. ESP-IDF's own WiFi, TCP/IP and `esp_timer` tasks run on core 0, above all of these. `setup()` starts `DisplayTask` (which starts `MatrixEngine`) first, then blocks in the WiFi setup (the captive portal can take up to 180 s), and only then creates the audio, sensor, clock and MQTT tasks.

I²C sensors (SCD4x CO₂, HTU21D temp/humidity, BH1750 light) share the bus behind a single `i2cMutex` (`i2c_mutex.h`) — every I²C transaction, including `begin()` calls, must be wrapped in `xSemaphoreTake`/`Give`.

Rendering follows a **Command Pattern**: each screen lives in its own `src/screen_*.cpp` file (`screen_clock`, `screen_temp`, `screen_co2`, `screen_spectrum`, `screen_alert`, `screen_icon`), all declared through one `screen_renderers.h` registry and dispatched from a single `switch` in `displayTask`. The OTA screen (a static "OTA") is drawn by `displayTask` itself before that switch; `screen_ota.cpp`'s percentage/progress-bar renderer (`renderOtaProgressView`) is not called anywhere.

**OTA pausing** is centralized in `task_registry.h`/`.cpp`: every sensor, clock, MQTT and audio task calls `waitWhileOtaPaused()` at a safe point of its loop, so that whole layer parks as a unit during an update (see [WiFi & OTA](#wifi--ota)). `DisplayTask` switches to its OTA screen instead, and `MatrixEngine` keeps scanning.

---

## Hardware / Pinout

Board: Seeed XIAO ESP32-S3 Plus (Arduino-style `Dx` pin aliases used for most GPIO), driving the 1624 RGB Atmosphere Level Indicator panel's HUB75-style interface.

| Function | Pin(s) |
|---|---|
| Panel RGB data | `R1`=42, `G1`=D3, `B1`=10 |
| Panel clock/latch/enable | `CLK`=D4, `LAT`=D6, `OE`=D5 |
| Panel address (column within a chip-select group of 8) | `A`=D0, `B`=38, `C`=39 |
| Panel chip-selects | `L_EN`=40, `M_EN`=41, `R_EN`=D2 |
| I²C bus (CO₂/temp/light sensors) | `SDA`=12, `SCL`=7 |
| Beeper | 13 |
| Mode button | D9 (`INPUT_PULLUP`) |
| Power toggle button | D10 (`INPUT_PULLUP`) |
| LD2412 presence (digital) | D1 (`INPUT_PULLDOWN`: high = someone present; an unplugged radar reads as nobody) |
| LD2454 UART (Serial1) | `RX`=11, `TX`=D7 @ 256000 baud |
| LD2454 power | VBUS (5 V) — the LD2454 manual specifies DC 5 V, >200 mA |

The radar header's pin order is not the LD2454's J1 order (1 `5V`, 2 `G`, 3 `T`, 4 `R`), so the cable is **not** 1:1: header 1 (VBUS) → J1-1, header 4 (GND) → J1-2, header 3 (`RADAR_T`, to GPIO11) → J1-3, header 2 (`RADAR_R`, from D7) → J1-4. The schematic (`schematics/`) carries the same note; its PDF render predates these changes, so re-plot it from KiCad.

The panel is driven by a **hand-rolled HUB75-style engine** (`led_matrix.cpp`/`.h`): it bit-bangs GPIO registers directly, splits the 24-column panel across 3 chip-select groups of 8, and controls brightness with on-time PWM within a fixed 180µs column window. Each pixel has one bit per color channel, so the panel shows **8 colors** (black, red, green, blue, yellow, cyan, magenta, white): every RGB color the firmware is given (clock color, icons, messages) is reduced by switching a channel on when it is above 127 (`LedMatrix::color565`), so `FF8800` shows as yellow and `7F7F7F` as black. The shift loop's set/clear masks for `R1`/`G1`/`B1`/`CLK` are computed at compile time from `pins.h` (each pin in its own register bank, GPIO 0–31 or 32–48), so moving one of those pins in `pins.h` is enough. Text is rendered with a hand-authored 4×14 PROGMEM font (`font_4x14.h`) covering `0–9`, `A–Z` (lower case is drawn as upper case), `:`, `.`, `-`, `!`, `?`, `/`, `%`, `+`, `,`, `'`, and a degree glyph mapped to `*` in source strings. Any other character is drawn as a small solid block, never dropped, so `-5` can't turn into `5` and a text keeps its width.

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

`AUDIO_STREAM_URL` must be a plain `http://host[:port]/path` URL serving a live **WAV stream of 16-bit PCM** (see [Audio Spectrum Analyzer](#audio-spectrum-analyzer) for the exact requirements).

### Library dependencies

`WiFiManager`, `PubSubClient`, `ArduinoJson`, `Preferences`, `SensirionI2cScd4x`, `HTU21D`, `BH1750`, and `AudioTools` (with `AudioRealFFT`). The web UI / OTA uses the core's synchronous `WebServer` and `Update` (no extra library). The LD2454 radar driver is built in (`ld2454_sensor.cpp`) — no external library.

---

## Display & View Management

- **Views:** `VIEW_TIME`, `VIEW_TEMP_DATA`, `VIEW_CO2`, `VIEW_SPECTRUM`, `VIEW_CUSTOM_MSG`, `VIEW_CUSTOM_ICON`.
- **Persistence:** the clock color and the spectrum *manual override* (see *View arbitration*) are stored in NVS (`Preferences`, namespace `"display"`) and restored on boot. The view itself is not: the node always boots to the clock, temp/CO₂ are 5s peeks, and spectrum comes up by itself when audio plays (unless the override is set). A deliberate choice via `selectView()` (button/MQTT `display/mode`) writes NVS only when it changes the override; transient views (custom message, icon, CO₂ popup) never do.
- **Auto-return:** temp/CO₂ auto-return after `DISPLAY_AUTO_RETURN_DELAY_MS` (5s). Icons end after `ICON_DISPLAY_DURATION_MS` (15s), scrolling messages after their whole passes (below).
- **Scrolling messages** move 1 px every `MSG_SCROLL_STEP_MS` (154ms), timed from the previous step so slow frames don't stretch the scroll; a pass takes (25 + 5 × characters) steps, e.g. 16.9s for `PACKAGE DELIVERED`. A message runs as many **whole passes** as come closest to its duration (`MQTT_MSG_DURATION_MS`, 15s; the IP address: `WIFI_IP_DISPLAY_DURATION_MS`, 15s), at least one, so even a 31-character text is shown to its end, and at most `MSG_MAX_SCROLL_PASSES` (3): `HI` runs 3 passes (16.2s), `DOOR OPEN` one (10.8s). A backstop `MSG_EXPIRY_MARGIN_MS` (2s) after the last pass is due ends a message that couldn't scroll (panel off, WiFi down). A new message starts from the right edge with all of its passes, also when it replaces one that is still scrolling. An empty message is ignored rather than blanking the panel.
- **View arbitration:**
  - Spectrum's automatic entry **cannot interrupt** an active custom message, icon or CO₂ popup: it waits and switches to spectrum once the overlay has ended (if audio is still active). View changes go through a compare-and-set (`DisplayManager::trySetView()`), so a message or icon arriving at the same moment is never overwritten by spectrum's entry or return.
  - As soon as audio is no longer active (see *Activity and sleep* under [Audio Spectrum Analyzer](#audio-spectrum-analyzer)), spectrum returns to the clock, but not before it has been on screen for `MIN_SPECTRUM_DISPLAY_MS` (5s) since it was entered; if someone is present (LD2412/LD2454) it stays until audio has been inactive for `SPECTRUM_PRESENCE_OVERRIDE_MS` (5s). Transient views (temp/CO₂/message/icon) are never the return target.
  - Selecting spectrum manually (button or MQTT `display/mode spectrum`) shows it for at least `MIN_SPECTRUM_DISPLAY_MS` from the selection; with no audio it then falls back to the clock like an automatic entry, and it re-enables automatic entry.
  - Once the user manually switches to a view other than spectrum (button or MQTT `display/mode`, temp/CO₂ included), spectrum's automatic re-entry is **suppressed** until the user manually selects spectrum again — audio staying active no longer yanks the display back. This override is persisted, so it survives a reboot.
  - Custom messages and MQTT-pushed icons **restore the view that was active before they interrupted it** (including spectrum) once they expire, rather than always falling back to the clock — the same pattern the CO₂ alarm popup already used. The exception is a temp/CO₂ peek or the CO₂ popup: its time is up by then, so the message or icon hands back to the clock instead (spectrum re-enters by itself if audio is active). A message or icon that the CO₂ popup interrupts is replayed from the start (with its full duration) after the popup. Manually cycling to temp/CO₂ via the button still auto-returns to the clock after 5s, unchanged — that's deliberate browsing, not an interruption.
- **Buttons:** mode button cycles `TIME → TEMP → CO2 → SPECTRUM`; power button blanks/wakes the panel without stopping any background task. Each press acts **once**, as soon as a poll (every 10–50ms) sees it; holding a button does not repeat, and it is re-armed only after reading released for `BUTTON_DEBOUNCE_MS` (30ms), so contact bounce can't fire it twice. A button held while the node boots acts only after it has been let go and pressed again.
- **Timers** (view auto-return, message/icon duration, CO₂ popup length and interval) are kept as start time + length and compared as elapsed time, so they stay correct across the 49.7-day `millis()` rollover.
- **Persistent clock color:** MQTT-settable, survives reboots (see [MQTT Topics](#mqtt-topics)). It is shown in the nearest of the panel's 8 colors (see [Hardware / Pinout](#hardware--pinout)).
- **No artificial startup delay** — `displayTask` initializes the matrix immediately rather than waiting a fixed period before starting.

## RLE Icon Display

Pixel-art icons can be pushed to the panel over MQTT as a compact run-length-encoded binary payload:

**Topic:** `led_matrix_node/display/icon`
**Payload:** repeating runs — `[runLength, r, g, b]`, one byte each — covering up to 384 pixels (24×16) in row-major order. Colors are quantized to the panel's native 3-bit palette (each channel thresholds at >127). Pixels the runs don't cover are black (never left over from the previous icon); a payload whose runs cover more than 384 pixels, or none, is rejected. The icon is decoded into a separate buffer and swapped in whole, together with the view switch, so no frame mixes the old and new icon.

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

  Whitespace anywhere in the hex text is ignored, so the final newline that `mosquitto_pub -f icon.txt` or a YAML block scalar adds, or a line-wrapped string, still works. The payload counts as hex when it holds nothing but hex digits and whitespace; the digit count must be a multiple of 8 (4 bytes per run).

- **Raw binary** is also accepted (auto-detected) for programmatic publishing, but the hex-string format above is recommended for all use cases — it works identically from a script, MQTT Explorer, or Home Assistant with no extra encoding step.

An empty payload (e.g. clearing a retained icon) is ignored. A payload of plain text with a non-hex character in it is rejected rather than drawn as raw bytes (it could only come out black). Every rejection is logged on the serial console with its reason (`[ICON] rejected: …`).

The icon is kept in RAM on the node and **redrawn every frame** while `VIEW_CUSTOM_ICON` is active (it is not stored across reboots, and is protected from spectrum's automatic re-entry the same way custom messages are — spectrum waits until the icon has ended). After `ICON_DISPLAY_DURATION_MS` (15s) the panel returns to the view the icon interrupted (see *View arbitration*).

> ⚠️ **If using raw binary instead of the hex format above:** `mosquitto_pub -m "\xc0\x00..."` does **not** interpret `\xNN` as byte escapes — it sends those characters literally as ASCII text. Don't pipe binary into `mosquitto_pub -l` either: it sends one message per line and measures each line with `strlen()`, so the message ends at the first `0x00` byte, which nearly every icon has (black is `000000`). The node then gets only the bytes before it (usually just one) and rejects the payload, or draws a fragment. Send all of stdin as one message with `-s`, or a file with `-f icon.bin`:
>
> ```bash
> printf '\300\000\000\000\300\377\377\377' | mosquitto_pub -t led_matrix_node/display/icon -s
> ```
>
> (Octal escapes work in every `printf`; `\xNN` needs bash's or GNU coreutils' `printf`, dash — `/bin/sh` on Debian/Ubuntu — doesn't know it.) This is exactly the trap the hex-string format above avoids — prefer it unless you have a specific reason to send raw binary.

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

Syncs via `configTzTime` against `pool.ntp.org` (`TIME_ZONE`: CET/CEST). Shows 24-hour time without a leading zero, `H:MM` before 10:00 and `HH:MM` from 10:00, with the colon blinking once a second; shows `SYNC` until the first successful fix. The local time is read without waiting (`readLocalTime()` in `ntp_clock.cpp`): the core's `getLocalTime()` polls for up to 5s while the time is unset, which used to freeze the display task on every CO₂ popup.

## Environmental Sensors

Every value has a validity and freshness check: a sensor keeps its last *good* value plus the time it was read, and a value that was never read, was invalidated, or is older than the sensor's stale limit counts as **unknown**. The sensor task drops a value once it is stale, so it stays unknown even when the 32-bit `millis()` age wraps after 49.7 days. An unknown value is never shown as a number (the CO₂ view shows `----`, the temperature view `--*C`), never published over MQTT, and never drives the CO₂ alarm, so a dead or unplugged sensor can't pass off a frozen or error value as live.

- **CO₂ (Sensirion SCD4x):** 0-ppm glitch rejection. Start-up checks the stop/start commands and logs the Sensirion error text; the start is tried `CO2_START_ATTEMPTS` (3) times, `CO2_START_RETRY_MS` (1s) apart. Polled every `CO2_SENSOR_INTERVAL` (30s), but every `CO2_FAST_POLL_MS` (5s) while there is no fresh value, so the first reading arrives 5–10s after boot instead of 30s. Watchdog: no successful read for `CO2_DATA_TIMEOUT_MS` (90s) makes the value unknown and restarts the measurement (logging the last I²C error). A good sample older than `CO2_DATA_STALE_MS` (120s) is unknown as well.
- **Temperature/Humidity (HTU21D):** temperature and humidity are checked separately: the library's error value 255 (I²C NACK, short read, CRC) is rejected, and so is anything outside −40…125°C or 0…100%. A failure streak is logged once. Unknown after `TEMP_HUM_DATA_STALE_MS` (60s) without a good read; a sensor missing at boot is picked up as soon as it answers. The temperature view shows the rounded value with its sign (`-5*C`; a five-character value such as `-15*C` uses the full 24 columns).
- **Ambient Light (BH1750):** drives automatic matrix brightness. The lux value is clamped to `LUX_MIN_THRESHOLD`–`LUX_MAX_THRESHOLD` (2–30 lx) and mapped onto `MATRIX_MIN_BRIGHTNESS`–`MATRIX_MAX_BRIGHTNESS` (20–255) along a power curve (normalized lux^0.6, so it brightens fastest in the dark range); each reading then moves the brightness a fifth of the way to that target, so it glides instead of jumping. The library's error values (−1 failed read, −2 not configured) are never used as lux. A failed `begin()` is retried every `LIGHT_SENSOR_RETRY_MS` (30s), and `LIGHT_SENSOR_MAX_FAILED_READS` (3) failed reads in a row re-run it. Unknown after `LIGHT_DATA_STALE_MS` (10s) without a good read; brightness then glides to `MATRIX_FALLBACK_BRIGHTNESS` (128) instead of the minimum.

## CO₂ Alarm System

Hysteresis band between `CO2_WARNING_THRESHOLD` (1200ppm) and `CO2_ALARM_LOW_THRESHOLD` (1000ppm); a red popup repeated every `CO2_ALARM_INTERVAL_MS` (10 min) while the alarm is active, with quiet-hours-gated beeping (3 × 500ms, only 08:00–21:00). A beeping popup closes when its beeps end (about 2.5s), a silent one after `CO2_ALARM_DISPLAY_DURATION_MS` (10s), and the view's own timer `CO2_ALARM_POPUP_SAFETY_MS` (20s) is a backstop, so it can never get stuck on screen. The CO₂ reading is drawn red for as long as the alarm is active (down to 1000ppm), not only above 1200ppm.

Quiet hours **fail closed**: the popup beeps only while the local time is known and inside the window. Without an NTP fix since boot (the clock shows `SYNC`) the popup still shows every 10 min, silently (for `CO2_ALARM_DISPLAY_DURATION_MS`, 10s). The display task is the beeper's only driver: an MQTT beep waits until an alarm sequence has finished, and an alarm takes over a running MQTT beep. When an OTA upload starts, the beeper is switched off (the alarm logic is paused for the upload).

An unknown CO₂ value (see [Environmental Sensors](#environmental-sensors)) ends the alarm: the popup closes and the beeper stops. If a fresh reading at or above 1000ppm (still in the alarm band) comes back before any reading below 1000ppm, the alarm resumes and its next popup keeps the 10-min schedule the alarm already had (it shows at once only if that time has passed), so a sensor that keeps dropping out can't beep on every recovery.

## Audio Spectrum Analyzer

Dual-resolution FFT (2048-pt bass / 1024-pt mid-high) driving a 24-column VU-style display with Gaussian-weighted log-frequency bands, compression, adaptive AGC, and tilt EQ. Auto-enters `VIEW_SPECTRUM` on active audio (respecting the view-arbitration rules above) and fades to sleep on sustained silence.

**Stream source.** `StreamIngest` fetches `AUDIO_STREAM_URL` with an `HTTP/1.0` GET (so compliant servers send the body as-is; `Host` carries the port when it isn't 80) and expects:

- status `HTTP/1.x 200` or `ICY 200` — anything else (404 of an offline Icecast mount, redirects, 401 …) is logged and retried, never fed to the FFT;
- a plain or `Transfer-Encoding: chunked` body — chunked framing is decoded before the WAV parser and the FFT see any byte (ffmpeg's built-in HTTP server, `-listen 1`, always answers chunked, even to an HTTP/1.0 request); malformed framing is logged and retried;
- a **RIFF/WAVE header** (RF64 accepted) — the chunks are walked up to `data`, so `LIST`/`JUNK`/… chunks (ffmpeg adds a `LIST`) are fine — with **16-bit PCM** (`WAVE_FORMAT_PCM`, or `EXTENSIBLE` with a PCM sub-format), **1–8 channels** (downmixed to mono) at **8–48 kHz**. The FFT strides follow the stream's sample rate (30 bass / 100 mid-high frames per second). Other formats are rejected with one log line, e.g. `[Audio] unsupported WAV format (tag 3, 2 ch, 32-bit, 48000 Hz) - need 16-bit PCM`.

The connection is dropped and re-established if the HTTP and WAV headers aren't complete within `STREAM_HEADER_TIMEOUT_MS` (5s; checked on every byte, so a peer that keeps sending non-HTTP data can't hold it open) or no data arrives for `STREAM_RX_TIMEOUT_MS` (5s) — a stalled or half-open socket would otherwise hang forever, as the node only receives. Failed attempts (connect error, bad status/headers/format) back off from `STREAM_RETRY_MIN_MS` (2s) doubling to `STREAM_RETRY_MAX_MS` (30s); when a valid stream ends or goes quiet the node reconnects after 2s. TCP reads may end mid-sample; the partial frame is carried over to the next read. The reader drains up to `STREAM_READ_BURST_BYTES` (2 KB) per 1 ms tick before yielding (≈2 MB/s, well above the 768 kB/s of 8 ch × 48 kHz), so even the largest accepted format never falls behind real time. Each connection starts with empty FFT windows, so nothing of the previous stream leaks into it.

**Activity and sleep.** Each 2048-point frame's level (mean bin magnitude, 22 Hz–16 kHz; `SPECTRUM_ABS_SILENCE_MAG` = 0.15 ≈ −43 dBFS RMS) drives both:

- *Audio active* (auto-entry and holding the view): `SPECTRUM_ACTIVE_MIN_FRAMES` (3, ≈100ms) loud frames in a row start it, any loud frame keeps it, and it ends `AUDIO_SILENCE_TIMEOUT_MS` (2s) after the last loud frame — or as soon as the bars have faded out. It is judged only on frames that actually arrive, so a stream that stops sending (or reconnects) can't keep the view on a blank spectrum; a disconnect resets it.
- *Sleep fade*: the bars fade out over `SPECTRUM_SLEEP_FADE_MS` (2s) when the 2s average level drops below `SPECTRUM_SLEEP_ENTER_MAG` (0.12) or no frame arrived for `SPECTRUM_FRAME_STALE_MS` (200ms), and fade back in over `SPECTRUM_WAKE_FADE_MS` (250ms) once it rises above `SPECTRUM_SLEEP_EXIT_MAG` (0.18), always from the current brightness.

**Band mapping.** Both FFTs use a tabulated Hann window and precomputed Gaussian band weights (rebuilt only when the sample rate changes). The 1024-point FFT is scaled by √(Σw²₂₀₄₈/Σw²₁₀₂₄) so broadband content reads the same on both sides of the 400/500 Hz split. Band levels are dB relative to the AGC reference (not dBFS), tilted by `SPECTRUM_TILT_DB_PER_OCTAVE`, compressed 2:1 above `SPECTRUM_COMPRESSION_KNEE_DB` (−12 dB) and mapped at `SPECTRUM_DB_PER_STEP` (2.5 dB) per LED row from `SPECTRUM_FLOOR_DB` (−20 dB = 0 rows): 0 dB → 5.6 rows, +12 dB → 8, +52 dB → 16.

**Peak-dot physics** are ported from [`audioMotion-analyzer`](https://github.com/hvianna/audioMotion-analyzer)'s gravity-based fall model: rather than dropping at a fixed rate, each band's peak indicator accelerates continuously after `BALLISTICS_PEAK_HOLD_MS` (700ms hold), governed by `SPECTRUM_PEAK_GRAVITY_STEPS_PER_S2` — tune this constant to make peaks fall faster/slower/more "floaty". Frames that arrive in a TCP burst faster than the 10 ms ballistics tick are merged by keeping each band's maximum, so short transients still show.

## mmWave Radar

**LD2412** — binary presence on a single GPIO (its OUT pin, high while someone is present, the factory polarity). The input has an internal pulldown, as the board has no pull resistor: an unplugged or unpowered radar reads as "nobody" instead of a floating pin that flaps the presence topic. **LD2454** — up to 3 tracked targets over UART (256000 baud, 10 frames/s), implemented against Hi-Link's *HLK-LD2454 serial communication protocol V1.00* (LD2450-compatible frames: `AA FF 03 00 … 55 CC`, sign-magnitude coordinates in mm, speed in cm/s). The radar's factory default is **single-target** tracking and the protocol has no command to read the mode back, so the driver sends enable-config → multi-target (`0x90`) → end-config and only reports multi-target mode once every step is ACKed. It re-applies the mode whenever the gap between two frames reaches `LD2454_RESTART_GAP_MS` (a radar reboot would otherwise silently revert it; the pause around its own configuration is ignored), retries a failed attempt with backoff from `LD2454_CONFIG_RETRY_MIN_MS` up to `LD2454_CONFIG_RETRY_MS` (immediately once a silent radar starts reporting again), resyncs on every frame header so a truncated frame can't swallow the next frame or ACK, and its watchdog clears the targets and fully re-inits the link on a stall — UART desync doesn't self-heal the way the frame-redrawn matrix does. Multi-target mode needs the ESP's TX (`D7`) wired to the radar's `R` pin; receiving targets only needs `RX`=11 ← radar `T`.

### LD2454 room mapping

The node turns each LD2454 target into room coordinates before publishing it, so Home Assistant gets positions in cm from a corner you choose, not raw radar coordinates. Three features run on the node:

- **Room outline and boundary:** targets are tested against a polygon (up to 16 points). A target outside it has `in_boundary: false`.
- **Presence gating:** with `gates_presence` on (the default), a target outside the outline does not count toward presence. This stops a target seen through a wall from turning on a light.
- **Occupancy areas:** up to three polygons (up to 12 points each), for example a desk, bed, or door. An area is occupied while a target stays in it, with hysteresis, a confirm delay, a clear delay, and speed gating so a passing target does not count.

The transform and the area logic are ported from [mmwave-component](https://github.com/zomco/mmwave-component) (MIT). Source: `include/room_mapping.h`, `include/room_calibration.h`, `src/room_calibration.cpp`.

**Calibration keys** (all optional; send only the ones you change):

| Key | Unit / type | Default | Meaning |
|---|---|---|---|
| `radar_x`, `radar_y` | cm | 0, 0 | Radar position from the room origin (range ±2000) |
| `radar_z` | cm | 150 | Mounting height (stored; the mapping is 2D, range 50–400) |
| `yaw` | degrees | 0 | Angle from the room +Y axis, clockwise positive (range ±180). 0 = radar faces +Y |
| `pitch`, `roll` | degrees | 0 | Tilt of the radar (range ±90) |
| `polygon` | `x,y;x,y;…` cm | empty | Room outline, up to 16 points. Empty = no boundary filter |
| `area_1`, `area_2`, `area_3` | `x,y;x,y;…` cm | empty | Occupancy area outlines, up to 12 points each. Empty = unused |
| `gates_presence` | boolean | true | Count only targets inside the room toward presence |
| `presence_timeout_s` | s | 5 | Presence stays on this long after the last counted target (range 0.5–60) |
| `area_hysteresis` | cm | 50 | How far a target may leave an area before it is released (range 0–200) |
| `area_still_speed` | cm/s | 15 | Targets slower than this keep their area |
| `area_confirm_s` | s | 0.4 | Delay before an area turns occupied (range 0–10) |
| `area_clear_s` | s | 2 | Delay before an occupied area clears (range 0–30) |
| `area_pass_speed` | cm/s | 80 | Fast targets crossing an unoccupied area do not turn it on (range 0–300) |

Numbers are clamped to their ranges. A malformed polygon point is skipped and logged as `[ROOM] ... malformed points ignored`; the valid points are still applied.

Changes take effect on the next radar frame, so a calibration change never lands in the middle of a frame. Every accepted command is saved to flash (NVS namespace `ld2454cal`) and survives a reboot.

## WiFi & OTA

- `WiFiManager` captive portal (180s timeout) for first-time setup, followed by an automatic **backup network** attempt (`WIFI_BACKUP_SSID`/`PASS`, `WIFI_BACKUP_TIMEOUT_MS` = 15s) if the primary/portal path fails, before finally restarting.
  - The backup is only ever a temporary fallback. Before joining it (at boot, or later through link supervision below) the driver's WiFi config is switched to RAM-only (`esp_wifi_set_storage(WIFI_STORAGE_RAM)`), so the primary network that WiFiManager saved stays in NVS and the next boot tries it first. `WiFi.persistent(false)` would not be enough here: core 3.2.0 applies it only when the driver is first initialised, which `autoConnect()` has already done.
  - While on the backup, the node scans for the primary's SSID every `WIFI_PRIMARY_RETRY_MIN_MS` (5 min). The scan keeps the backup link up. When the primary is in range the node switches back to it and shows the new IP. If the switch hasn't connected within `WIFI_PRIMARY_CONNECT_TIMEOUT_MS` (20s), the node returns to the backup and doubles the scan interval, up to `WIFI_PRIMARY_RETRY_MAX_MS` (1 h).
- **Link supervision** (`updateWiFiManagement()`, called from `loop()`): the core's auto-reconnect retries only some disconnect reasons. It does not retry `ASSOC_LEAVE`, `BSS_TRANSITION_DISASSOC`, or `AUTH_FAIL` after the first connect (for example while the router reboots), so one of those used to leave the node offline until it was power-cycled.
  - After `WIFI_RECONNECT_INTERVAL_MS` (30s) without a connection, the node starts a fresh connect attempt (`esp_wifi_disconnect()` + `esp_wifi_connect()`, which also aborts a stuck attempt) and repeats it every 30s.
  - With a backup network configured, after `WIFI_SWITCH_NETWORK_AFTER_MS` (10 min) offline the node moves to the other network in place, without a restart: from the primary to the backup (RAM-only, as above, and the primary scan brings it back), or from a dead backup to the primary. It keeps alternating every 10 min while the outage lasts. Without a backup it just keeps up the 30s attempts.
  - A lost link never restarts the node. A reboot would block in the WiFi setup for the whole outage: no sensor or MQTT tasks, no CO₂ alarm, and the open setup AP broadcast. Offline, the sensors and the CO₂ alarm beeper keep running (the panel shows `WIFI`). To re-enter credentials after a router password change, power-cycle the node: a boot whose saved network doesn't connect opens the setup portal.
  - Supervision is skipped while an OTA upload is running.
- Post-connect, the panel shows the assigned IP address (green, scrolling in whole passes for about 15s — one pass for a typical address) — protected from being interrupted by spectrum auto-entry.
- **Web UI at `http://<node-ip>/`** (`web_server_ui.cpp`, the same design as the gps-logger firmware): a *Firmware update (OTA)* panel and a *Reset WiFi* button. The server is the core's synchronous `WebServer`, serviced by the Arduino `loop()` (`webUi.loop()` every 5 ms). The panel also shows the **MD5 and size of the running firmware image** plus IP and uptime (`GET /api/ota/status`) — the MD5 changes with every flashed build, so after an update it shows at a glance whether the new firmware is really running.
- **OTA updates** (`POST /update`, multipart file `firmware`, streamed straight into `Update` chunk by chunk) take over the whole node:
  - OTA mode starts only when firmware data actually arrives (`UPLOAD_FILE_START`), never on a bare request. The panel then shows a static **"OTA"** for the transfer, at the fixed `DISPLAY_BRIGHTNESS` (20) since the light sensor is paused, blanking briefly for each flash erase/write (see below). On success the device answers `OK, rebooting` and **restarts into the new firmware** right away (the page confirms via `/api/ota/status`, then reloads after 8 s). On failure it resumes whatever view was active beforehand — no explicit "restore" logic needed, since the underlying view state is simply untouched while the OTA flag is set.
  - The background tasks park themselves for the duration of the update — sensors, clock and MQTT *and* the audio pipeline (stream ingestion, ballistics, spectrum view-switching), freeing CPU and I²C/UART/network bandwidth for the flash write. They pause cooperatively via `waitWhileOtaPaused()` (`task_registry.h`) rather than being `vTaskSuspend()`-ed, because a task frozen mid-call can hold the lwIP core lock and stall the upload.
  - An upload that is cut off (browser tab closed, WiFi drop, client stalled past the `WebServer` read timeout) reaches the handler as `UPLOAD_FILE_ABORTED`: the update is discarded, the boot partition is untouched and normal operation resumes — no reboot.
  - Buttons, CO₂ alarm logic, and normal view rendering are all paused until the update finishes. The beeper is switched off when the upload starts (a beep that was sounding would otherwise last until the reboot), and pending MQTT beeps are dropped.
  - **The panel stops refreshing while flash is erased or written.** This ESP-IDF 5.4 build has no SPI-flash auto-suspend, so every flash erase or write (each OTA chunk, and NVS commits such as a new clock color or spectrum override) suspends the scheduler on both cores and parks the other core in a busy-wait with its flash cache off. `MatrixEngine` is an ordinary task, so it stops for that time: milliseconds per write, tens to hundreds of milliseconds per erase (`Update` erases the OTA partition in 64 KB blocks). The panel goes dark meanwhile; if the operation started on core 0 and caught the engine in the middle of a column, that one column stays lit instead. `displayTaskEngine` being `IRAM_ATTR`, with `gpio_ll_set_level()` pin writes instead of `digitalWrite()`, doesn't change this: it keeps the scan loop free of flash-cache misses, so the column timing is steady, but a task can't run while the scheduler is suspended, wherever its code lives. Refreshing through a flash operation would take an IRAM-safe interrupt (this core's GPTimer driver isn't built IRAM-safe) or a DMA-driven refresh.
- **Reset WiFi** (`POST /api/reset-wifi`) clears the saved WiFi credentials and reboots into the config portal; the reply is sent before the restart. It switches WiFi storage back to flash first, so the erase also reaches NVS while the node is on the backup network.
- **Cross-site / DNS-rebinding guard** (no login, by design): every request's `Host` must be an IP literal, a single-label name or a local-only suffix (`.local`, `.lan`, `.home`, `.home.arpa`, `.internal`, `.fritz.box`, …; add your own with `-D WEB_EXTRA_HOST_SUFFIX=\".corp.example.com\"`), and state-changing requests (`/update`, `/api/reset-wifi`) are refused when their `Origin`/`Referer` names another host — so a web page open in a browser on your LAN cannot flash firmware or wipe WiFi. Non-browser clients (curl, scripts) send neither header and stay allowed.

---

## MQTT Topics

### Commands (subscribe)

| Topic | Payload | Effect | Example |
|---|---|---|---|
| `led_matrix_node/display/mode` | `time` \| `temp` \| `co2` \| `spectrum` | Selects a view (`temp`/`co2`: for 5s, then the clock). `time`/`temp`/`co2` turn spectrum's automatic entry off, `spectrum` turns it back on; that choice persists across reboots | `mosquitto_pub -t led_matrix_node/display/mode -m "co2"` |
| `led_matrix_node/display/message` | text (≤31 chars, auto-uppercased; surrounding whitespace dropped; a UTF-8 `°` shows as the degree sign, other non-ASCII characters as a block) | Scrolling red alert in whole passes, about 15s (1–3 passes, a long text always to its end); empty payload ignored | `mosquitto_pub -t led_matrix_node/display/message -m "Package delivered"` |
| `led_matrix_node/display/power` | `1` \| `0` | Panel on/off | `mosquitto_pub -t led_matrix_node/display/power -m "0"` |
| `led_matrix_node/display/color` | `RRGGBB` / `#RRGGBB` / `r,g,b` | Sets and persists the clock color. The panel shows 8 colors: each channel is on above 127, off otherwise, so use `00`/`FF` per channel (`FF8800` would show as yellow) | `mosquitto_pub -t led_matrix_node/display/color -m "00FFFF"` |
| `led_matrix_node/display/icon` | RLE hex string, or raw RLE bytes (see above) | Shows a pixel-art icon for 15s; empty payload ignored | see examples above |
| `led_matrix_node/radar/ld2454/calibration/set` | JSON with any subset of the calibration keys (see [LD2454 room mapping](#ld2454-room-mapping)) | Applies the calibration, saves it to flash, and republishes the calibration state | `mosquitto_pub -t led_matrix_node/radar/ld2454/calibration/set -m '{"radar_x":200}'` |
| `led_matrix_node/beeper` | any | Single 100ms beep, at any hour (quiet hours apply only to the CO₂ alarm). Waits for a running alarm sequence and for 100ms of silence after the last beep; requests arriving meanwhile merge into one | `mosquitto_pub -t led_matrix_node/beeper -m "1"` |

### State (publish)

| Topic | Format | Interval | Notes |
|---|---|---|---|
| `led_matrix_node/sensors/state` | `{"co2":812,"temp":21.4,"hum":47.2,"lux":18.6}` | 10s | A field is left out while its sensor has no fresh value (missing, failing, still starting); sent even if that leaves `{}` |
| `led_matrix_node/sensors/status` | `online` / `offline` | on connect / LWT | Retained |
| `led_matrix_node/radar/ld2412` | `1` / `0` | 1s | Retained |
| `led_matrix_node/radar/ld2454` | `{"presence":true,"multi_target":true,"areas":[false,false,false],"targets":[{"id":0,"x":-25.1,"y":59.8,"speed":34,"distance":64.9,"angle":-22.8,"room_x":-25.1,"room_y":59.8,"in_boundary":true}],"count":1}` | 1s | Up to 3 targets. `x`, `y`, `distance`, `room_x`, `room_y` in cm; `speed` in cm/s; `angle` in degrees from the radar front, clockwise. `presence` after the room gate and timeout; `areas` = occupancy of areas 1–3; `multi_target` = radar ACKed multi-target mode |
| `led_matrix_node/radar/ld2454/calibration` | full calibration JSON (all keys) | on connect and after each change | Retained |

```bash
mosquitto_sub -v -t 'led_matrix_node/#'
```

### Home Assistant Auto-Discovery

Retained discovery configs are published under `homeassistant/sensor/Led_Matrix_Node_{id}/config` for `co2`, `temp`, `hum`, `lux` on every broker connection. Each has:

- `state_class: measurement`, so Home Assistant keeps long-term statistics for it.
- a value template that turns a missing field into `None`, e.g. `{{ value_json.co2 if 'co2' in value_json else None }}`, which HA shows as **unknown**. A plain `{{ value_json.co2 }}` would render an empty string, which HA ignores, so the old value would stay.
- `expire_after` = `MQTT_EXPIRE_AFTER_S` (30s, 3 publish intervals): if no state arrives at all (node hung or gone), the sensor turns **unavailable**.

The LD2454 adds, on the same connection:

- **Presence** (occupancy), **Target Count**, **Multi-Target Mode** (diagnostic), and **Area 1–3 Occupied**, read from `led_matrix_node/radar/ld2454`. Their `expire_after` is `MQTT_RADAR_EXPIRE_AFTER_S` (5s, the radar publishes every second).
- **Target 1–3**: X, Y, Speed, Distance, Angle, Room X, Room Y, and In Boundary.
- **Calibration** (config entities, writing `led_matrix_node/radar/ld2454/calibration/set`): Mount X/Y/Height/Yaw/Pitch/Roll, the presence timeout and area tuning as numbers, a *Gate Presence To Room* switch, and text entities for the room outline and the three area outlines.

### Connection

When the broker is unreachable or refuses the login, the node retries after `MQTT_RECONNECT_MIN_MS` (2s), doubling the gap up to `MQTT_RECONNECT_MAX_MS` (60s), instead of every 40ms. A dropped connection is retried at once, but never sooner than 2s after the previous attempt, so a broker that kicks the node right away (e.g. a duplicate client ID) doesn't get a reconnect storm. `MQTT_SOCKET_TIMEOUT_S` (5s) bounds the wait for the broker's CONNACK, and that wait sleeps instead of busy-spinning on core 1.

---

## Key Tunables (`constants.h`)

| Category | Constant | Value |
|---|---|---|
| Display | `DISPLAY_AUTO_RETURN_DELAY_MS` | 5000ms |
| Display | `MQTT_MSG_DURATION_MS` / `MSG_MAX_SCROLL_PASSES` | 15000ms / 3 (message: whole passes closest to 15s, 1–3) |
| Display | `MSG_SCROLL_STEP_MS` / `MSG_EXPIRY_MARGIN_MS` | 154ms per px / 2000ms (backstop after the last pass) |
| Display | `BUTTON_DEBOUNCE_MS` | 30ms (released this long before a button can act again) |
| Display | `DISPLAY_BRIGHTNESS` | 20 (of 255; at boot until the light sensor takes over, and during OTA) |
| Tasks | `MATRIX_ENGINE_TASK_PRIORITY` / `STREAM_TASK_PRIORITY` / `BALLISTICS_TASK_PRIORITY` | 5 / 4 / 3 (every other task 1; see [Architecture](#architecture)) |
| Beeper | `BEEP_DURATION_MS` | 100ms (MQTT beep, and the silence before it) |
| WiFi | `WIFI_CONFIG_TIMEOUT` / `WIFI_BACKUP_TIMEOUT_MS` | 180s / 15000ms |
| WiFi | `WIFI_IP_DISPLAY_DURATION_MS` | 15000ms |
| WiFi | `WIFI_RECONNECT_INTERVAL_MS` / `WIFI_SWITCH_NETWORK_AFTER_MS` | 30000ms / 600000ms offline (reconnect attempt / primary ↔ backup switch) |
| WiFi | `WIFI_PRIMARY_RETRY_MIN_MS` → `WIFI_PRIMARY_RETRY_MAX_MS` | 300000ms doubling to 3600000ms (primary scan while on the backup) |
| WiFi | `WIFI_PRIMARY_CONNECT_TIMEOUT_MS` | 20000ms |
| Icon | `ICON_DISPLAY_DURATION_MS` | 15000ms |
| CO₂ Alarm | `CO2_WARNING_THRESHOLD` / `CO2_ALARM_LOW_THRESHOLD` | 1200 / 1000 ppm |
| CO₂ Alarm | `CO2_ALARM_INTERVAL_MS` / `CO2_ALARM_BEEP_COUNT` | 600000ms / 3 |
| CO₂ Alarm | `CO2_ALARM_START_HOUR`–`END_HOUR` | 08:00–21:00 |
| CO₂ Alarm | `CO2_ALARM_BEEP_ON_MS` / `CO2_ALARM_BEEP_OFF_MS` | 500ms / 500ms |
| CO₂ Alarm | `CO2_ALARM_DISPLAY_DURATION_MS` / `CO2_ALARM_POPUP_SAFETY_MS` | 10000ms (silent popup) / 20000ms (backstop) |
| CO₂ sensor | `CO2_SENSOR_INTERVAL` / `CO2_FAST_POLL_MS` | 30000ms / 5000ms (poll; fast while there is no fresh value) |
| CO₂ sensor | `CO2_DATA_TIMEOUT_MS` / `CO2_DATA_STALE_MS` | 90000ms (watchdog: value unknown, restart) / 120000ms (good sample older → unknown) |
| CO₂ sensor | `CO2_START_ATTEMPTS` / `CO2_START_RETRY_MS` | 3 / 1000ms |
| Temp/humidity | `TEMP_HUM_SENSOR_INTERVAL` / `TEMP_HUM_DATA_STALE_MS` | 15000ms / 60000ms |
| Light | `LIGHT_SENSOR_INTERVAL` / `LIGHT_DATA_STALE_MS` | 2000ms / 10000ms |
| Light | `LIGHT_SENSOR_RETRY_MS` / `LIGHT_SENSOR_MAX_FAILED_READS` | 30000ms / 3 |
| Light | `MATRIX_FALLBACK_BRIGHTNESS` | 128 (of 255, without a fresh light reading) |
| Light | `LUX_MIN_THRESHOLD` / `LUX_MAX_THRESHOLD` | 2 lx / 30 lx (clamp range of the brightness curve) |
| Light | `MATRIX_MIN_BRIGHTNESS` / `MATRIX_MAX_BRIGHTNESS` | 20 / 255 |
| Matrix | `MATRIX_WIDTH` / `MATRIX_HEIGHT` | 24 / 16 |
| MQTT | `MQTT_PUBLISH_INTERVAL` / `MQTT_RADAR_PUBLISH_INTERVAL` | 10000ms / 1000ms |
| MQTT | `MQTT_EXPIRE_AFTER_S` | 30s (HA `expire_after`) |
| MQTT | `MQTT_RECONNECT_MIN_MS` → `MQTT_RECONNECT_MAX_MS` | 2000ms doubling to 60000ms |
| MQTT | `MQTT_SOCKET_TIMEOUT_S` | 5s |
| Radar | `LD2454_DATA_TIMEOUT_MS` | 10000ms |
| Radar | `LD2454_CONFIG_RETRY_MIN_MS` → `LD2454_CONFIG_RETRY_MS` | 2000ms doubling to 60000ms |
| Radar | `LD2454_ACK_TIMEOUT_MS` / `LD2454_RESTART_GAP_MS` | 1500ms / 1000ms |
| Radar | `MQTT_RADAR_EXPIRE_AFTER_S` | 5s (radar entities turn unavailable after 5 missed reports) |
| Radar | `MQTT_LD2454_CAL_STATE_TOPIC` / `MQTT_LD2454_CAL_SET_TOPIC` | `led_matrix_node/radar/ld2454/calibration` / `.../calibration/set` |
| Room | `ROOM_MAX_VERTS` / `AREA_MAX_VERTS` / `AREA_COUNT` | 16 / 12 / 3 (room outline points / area outline points / number of areas) |
| Spectrum | `SPECTRUM_FLOOR_DB` / `SPECTRUM_DB_PER_STEP` / `SPECTRUM_COMPRESSION_KNEE_DB` | −20 dB / 2.5 dB / −12 dB (relative to the AGC reference) |
| Spectrum | `SPECTRUM_TILT_DB_PER_OCTAVE` | 3dB/oct |
| Spectrum | `SPECTRUM_PEAK_GRAVITY_STEPS_PER_S2` | 220.0 (steps/s²) |
| Spectrum | `SPECTRUM_ABS_SILENCE_MAG` / `SPECTRUM_ACTIVE_MIN_FRAMES` | 0.15 / 3 frames |
| Spectrum | `SPECTRUM_SLEEP_ENTER_MAG` / `SPECTRUM_SLEEP_EXIT_MAG` | 0.12 / 0.18 |
| Spectrum | `SPECTRUM_SLEEP_FADE_MS` / `SPECTRUM_WAKE_FADE_MS` / `SPECTRUM_FRAME_STALE_MS` | 2000ms / 250ms / 200ms |
| Spectrum | `AUDIO_SILENCE_TIMEOUT_MS` / `MIN_SPECTRUM_DISPLAY_MS` / `SPECTRUM_PRESENCE_OVERRIDE_MS` | 2000ms / 5000ms / 5000ms |
| Audio stream | `STREAM_HEADER_TIMEOUT_MS` / `STREAM_RX_TIMEOUT_MS` | 5000ms / 5000ms |
| Audio stream | `STREAM_READ_BURST_BYTES` | 2048 bytes per 1 ms tick |
| Audio stream | `STREAM_RETRY_MIN_MS` → `STREAM_RETRY_MAX_MS` | 2000ms doubling to 30000ms |

See `constants.h` for the full list.

---

## Setup Guide: LD2454 room mapping and Home Assistant

This guide takes the node from the original firmware to a calibrated LD2454 room map in Home Assistant. Follow the steps in order. Step 8 (calibration) comes after the Home Assistant steps, so the dashboard and automations are ready when you measure the room.

Requirements: PlatformIO, `patch`, Mosquitto client tools (`brew install mosquitto` on macOS), a reachable MQTT broker, and Home Assistant 2024.10 or newer for the automation syntax in step 9.

### Step 1: Back up the project

```bash
cp -R . ../led_matrix_node_backup_before_patch
```

The project is not a git repository, so this copy is the only safe rollback point until you are satisfied. Check that the backup contains `platformio.ini`, `src/`, and `include/`.

### Step 2: Apply the patches

Apply the patches in this order from the project folder. Do a dry run first; it must print one `patching file` line per file and no `FAILED` or `hunk` messages.

```bash
patch -p1 --dry-run < ld2454_room_mapping.patch
patch -p1 < ld2454_room_mapping.patch
patch -p1 --dry-run < fix_chinese_comments.patch
patch -p1 < fix_chinese_comments.patch
patch -p1 --dry-run < readme_update.patch
patch -p1 < readme_update.patch
```

Check the result:

```bash
ls include/room_mapping.h include/room_calibration.h src/room_calibration.cpp
grep -c publishRadarConfig src/mqtt_manager.cpp     # must print 0
```

Do not apply `ld2454_homeassistant.patch` from the earlier round. It is superseded by `ld2454_room_mapping.patch`.

### Step 3: Build

```bash
pio run -e seeed_xiao_esp32s3
```

The build must end with `[SUCCESS]`. The first build downloads the toolchain and can take several minutes. Do not flash a failing build.

### Step 4: Flash

```bash
pio run -e seeed_xiao_esp32s3 -t upload
```

If the upload cannot find a serial port, hold BOOT, tap RESET, release BOOT, then run the command again.

### Step 5: Check the serial log

```bash
pio device monitor -b 115200
```

Press RESET once and look for these lines within about 10 seconds:

- `MQTT connected`
- `[LD2454] multi-target tracking enabled`
- `[LD2454] calibration applied: radar (0,0) yaw 0.0, room 0 pts` on a fresh device, or your saved values after a reboot

A `[ROOM] ... ignored` line means a calibration command was malformed. Stop the monitor with `Ctrl+C`.

### Step 6: Check the MQTT output

In a second terminal, replace the broker address and credentials with yours:

```bash
mosquitto_sub -h <broker-ip> -u <user> -P <password> -t 'led_matrix_node/radar/#' -v
```

- `led_matrix_node/radar/ld2454/calibration` arrives once, retained, with all calibration keys.
- `led_matrix_node/radar/ld2454` arrives about once a second. Stand in front of the radar: `presence` becomes `true` and a target appears in `targets`. After you leave, `presence` returns to `false` about 5 seconds later.

### Step 7: Check the Home Assistant entities

1. Open **Settings → Devices & services → MQTT → Led Matrix Node**.
2. Confirm the presence, area, and target entities exist.
3. In **Developer tools → States**, filter on `ld2454`. Presence and target values should update while you walk.
4. If an entity has a suffix such as `_2`, an older entity with the same name still exists. Delete the old one.

Entity names used in the rest of this guide:

| Purpose | Entity |
|---|---|
| Presence | `binary_sensor.led_matrix_node_ld2454_presence` |
| Target count | `sensor.led_matrix_node_ld2454_target_count` |
| Area 1 occupied | `binary_sensor.led_matrix_node_ld2454_area_1_occupied` |
| Target 1 room X | `sensor.led_matrix_node_ld2454_target_1_room_x` |
| Mount X | `number.led_matrix_node_ld2454_mount_x` |
| Gate presence to room | `switch.led_matrix_node_ld2454_gate_presence_to_room` |
| Room outline | `text.led_matrix_node_ld2454_room_outline` |

### Step 8: Calibrate the room (do this after steps 9 and 10)

All distances are in cm from one corner of the room that you choose as the origin. Measure before you start:

- **Room size**: width (X) and depth (Y).
- **Radar position**: distance from the origin along X and Y.
- **Facing**: the direction the radar's front points, measured as an angle from the room's +Y axis. Yaw is positive when it turns clockwise seen from above. Yaw 0 means the radar faces the room's +Y direction, which is toward the wall opposite the origin if the origin is the near corner. Facing the right wall is +90; facing the left wall is −90.

Enter the values in Home Assistant (**Calibration** tab on the LD2454 dashboard), or send them over MQTT:

```bash
mosquitto_pub -h <broker-ip> -u <user> -P <password> \
  -t 'led_matrix_node/radar/ld2454/calibration/set' \
  -m '{"radar_x":120,"radar_y":50,"yaw":0,"polygon":"0,0;400,0;400,300;0,300"}'
```

Replace the numbers with your measurements. Each key is optional, and the command is applied immediately and saved to flash.

Then verify with a walk test:

1. Stand at a known spot in the room. Check that **Target 1 room X / room Y** matches your position in the room coordinates.
2. If the path is mirrored, flip the sign of `yaw`. If it is rotated, adjust `yaw`. If it is offset, adjust `radar_x` / `radar_y`.
3. Walk outside the outline. **In boundary** must turn off, and presence must stay off while **Gate presence to room** is on.
4. Draw an area outline (for example, the desk) with `area_1`, then sit in it. **Area 1 occupied** should turn on after the confirm delay and off after the clear delay.

If the area flickers, raise `area_clear_s` or `area_hysteresis`. If it reacts too slowly, lower `area_confirm_s`.

### Step 9: Home Assistant dashboard and automations

**Dashboard.** Create a new dashboard so your existing dashboards stay untouched:

1. **Settings → Dashboards → Add dashboard → New dashboard from scratch**. Title: `LD2454 Radar`.
2. Open it, click the pencil (Edit), then **⋮ → Raw configuration editor**.
3. Replace the contents with the YAML below, then **Save**.

```yaml
title: LD2454 Radar
views:
  - title: Room
    cards:
      - type: entities
        title: Presence
        entities:
          - binary_sensor.led_matrix_node_ld2454_presence
          - sensor.led_matrix_node_ld2454_target_count
          - binary_sensor.led_matrix_node_ld2454_multi_target_mode
          - binary_sensor.led_matrix_node_ld2454_area_1_occupied
          - binary_sensor.led_matrix_node_ld2454_area_2_occupied
          - binary_sensor.led_matrix_node_ld2454_area_3_occupied
      - type: entities
        title: Target 1
        entities:
          - sensor.led_matrix_node_ld2454_target_1_x
          - sensor.led_matrix_node_ld2454_target_1_y
          - sensor.led_matrix_node_ld2454_target_1_distance
          - sensor.led_matrix_node_ld2454_target_1_speed
          - sensor.led_matrix_node_ld2454_target_1_room_x
          - sensor.led_matrix_node_ld2454_target_1_room_y
          - binary_sensor.led_matrix_node_ld2454_target_1_in_boundary
      - type: entities
        title: Target 2
        entities:
          - sensor.led_matrix_node_ld2454_target_2_x
          - sensor.led_matrix_node_ld2454_target_2_y
          - sensor.led_matrix_node_ld2454_target_2_room_x
          - sensor.led_matrix_node_ld2454_target_2_room_y
          - binary_sensor.led_matrix_node_ld2454_target_2_in_boundary
      - type: entities
        title: Target 3
        entities:
          - sensor.led_matrix_node_ld2454_target_3_x
          - sensor.led_matrix_node_ld2454_target_3_y
          - sensor.led_matrix_node_ld2454_target_3_room_x
          - sensor.led_matrix_node_ld2454_target_3_room_y
          - binary_sensor.led_matrix_node_ld2454_target_3_in_boundary
  - title: Calibration
    cards:
      - type: entities
        title: Mount
        entities:
          - number.led_matrix_node_ld2454_mount_x
          - number.led_matrix_node_ld2454_mount_y
          - number.led_matrix_node_ld2454_mount_height
          - number.led_matrix_node_ld2454_mount_yaw
          - number.led_matrix_node_ld2454_mount_pitch
          - number.led_matrix_node_ld2454_mount_roll
      - type: entities
        title: Room and areas
        entities:
          - switch.led_matrix_node_ld2454_gate_presence_to_room
          - text.led_matrix_node_ld2454_room_outline
          - text.led_matrix_node_ld2454_area_1_outline
          - text.led_matrix_node_ld2454_area_2_outline
          - text.led_matrix_node_ld2454_area_3_outline
      - type: entities
        title: Tuning
        entities:
          - number.led_matrix_node_ld2454_presence_timeout
          - number.led_matrix_node_ld2454_area_hysteresis
          - number.led_matrix_node_ld2454_area_still_speed
          - number.led_matrix_node_ld2454_area_confirm_delay
          - number.led_matrix_node_ld2454_area_clear_delay
          - number.led_matrix_node_ld2454_area_pass_speed
```

If a card says "Entity not available", the entity ID differs on your system. Open **Developer tools → States**, find the correct ID, and replace it in the raw editor.

**Test automation.** Confirm the trigger works before controlling real devices. Create a new automation, open **⋮ → Edit in YAML**, and paste:

```yaml
alias: LD2454 test - presence notification
description: Creates a notification when presence turns on.
triggers:
  - trigger: state
    entity_id: binary_sensor.led_matrix_node_ld2454_presence
    to: "on"
conditions: []
actions:
  - action: persistent_notification.create
    data:
      title: Radar
      message: Presence detected
mode: single
```

Walk in front of the radar. A notification titled **Radar** should appear. If not, open the automation's **Traces** to see whether the trigger fired.

**Desk lamp automation.** Save this only after calibration (step 8) has set the Area 1 outline and you have confirmed it reacts. Replace `light.desk_lamp` with your light's entity ID:

```yaml
alias: Desk lamp when someone is at the desk
description: Turns the desk lamp on when Area 1 is occupied and off when it clears.
triggers:
  - trigger: state
    entity_id: binary_sensor.led_matrix_node_ld2454_area_1_occupied
    to: "on"
    id: occupied
  - trigger: state
    entity_id: binary_sensor.led_matrix_node_ld2454_area_1_occupied
    to: "off"
    id: clear
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: occupied
        sequence:
          - action: light.turn_on
            target:
              entity_id: light.desk_lamp
      - conditions:
          - condition: trigger
            id: clear
        sequence:
          - action: light.turn_off
            target:
              entity_id: light.desk_lamp
mode: restart
```

For Home Assistant versions before 2024.10, use the legacy keys. Replace `triggers:` with `trigger:`, `trigger: state` with `platform: state`, `actions:` with `action:`, and `conditions:` with `condition:`. Keep the same `id:` values and the `choose` block, but write each trigger condition as `platform: state` with `id:`, and match it with `trigger.id`.

### Step 10: Backup, reset, and rollback

**Keep the backup.** The copy from step 1 is your rollback point. Keep it until the room map has been stable for a few days.

**Reset the calibration.** Sending defaults restores the uncalibrated state. Use this to clear the outlines and tuning:

```bash
mosquitto_pub -h <broker-ip> -u <user> -P <password> \
  -t 'led_matrix_node/radar/ld2454/calibration/set' \
  -m '{"radar_x":0,"radar_y":0,"radar_z":150,"yaw":0,"pitch":0,"roll":0,"gates_presence":true,"presence_timeout_s":5,"area_hysteresis":50,"area_still_speed":15,"area_confirm_s":0.4,"area_clear_s":2,"area_pass_speed":80,"polygon":"","area_1":"","area_2":"","area_3":""}'
```

**Roll back the firmware.** Restore the backup and reflash:

```bash
cd ../led_matrix_node_backup_before_patch
pio run -e seeed_xiao_esp32s3 -t upload
```

Calibration is stored in NVS under the namespace `ld2454cal`. Older firmware ignores it, so leaving it in flash is harmless. To remove everything, including Wi-Fi and display settings, run `pio run -t erase`. This erases the whole flash, so flash the firmware again afterwards (`pio run -t upload`).

**Remove Home Assistant leftovers.** If the rollback removes the room entities, they stay in Home Assistant as unavailable. Delete them under **Settings → Devices & services → MQTT → Led Matrix Node**, and delete the **LD2454 Radar** dashboard and the test automations.

---

## Security Notes

- `secrets.h` contains live credentials (backup WiFi, MQTT, audio stream URL; the primary WiFi network is entered in the setup portal and kept in NVS) — keep it out of version control.
- MQTT is plain TCP on port 1883 (`MQTT_PORT`) without TLS, so the broker login and every command travel unencrypted on the LAN; anyone who can publish to the broker can drive the panel and the beeper.
- The RLE icon generator (`RLE icon generator/`) saves the MQTT password only when its *Remember* box is ticked, and then in plain text in the browser's storage (see its README).
- The web UI (`/`, `/update`, `/api/reset-wifi`) has no login: any device on the local network can flash firmware or reset WiFi (e.g. with curl). Other *websites* are blocked by the Origin/Host guard above, but treat the LAN itself as trusted. The WiFiManager setup access point is open (no password) while it runs — only at boot, when the saved network doesn't connect; a link lost later never reopens it.