# Testing

Step-by-step checks for the firmware, the radar frames stream (phase P1) and the hardware, written to be followed later on any computer (macOS, Linux or Windows with Git Bash). Commands are for bash/zsh.

## 1. One-time setup on a new computer

1. Clone and check out the work branch:

   ```bash
   git clone <repo-url> LedMatrixSensorNode
   cd LedMatrixSensorNode
   git checkout ld2454-zone-learning-p1
   ```

   Line endings and file modes are pinned in `.gitattributes` and in git itself, so a fresh checkout on macOS shows no changes (`git status` is clean). Do not `chmod` tracked files by hand: the scripts that need it are already executable in git.

2. Tools:
   - **Python 3.10+** (macOS: `brew install python`).
   - **PlatformIO Core**: `python3 -m pip install --user platformio`, or the PlatformIO extension in VS Code. The first build downloads the pinned platform (pioarduino 54.03.20) and the libraries.
   - **Mosquitto clients** for `mosquitto_sub` / `mosquitto_pub` (macOS: `brew install mosquitto`; Debian/Ubuntu: `sudo apt install mosquitto-clients`).
   - **paho-mqtt 2.x** for the broker checker, in a virtual environment in the repo (git-ignored):

     ```bash
     python3 -m venv .venv
     . .venv/bin/activate
     python3 -m pip install 'paho-mqtt>=2,<3'
     ```

   - **KiCad 10** for the schematic.

3. Credentials: copy `include/secrets.example.h` to `include/secrets.h` and fill in the MQTT host, user and password, the audio stream URL and the optional backup WiFi. `secrets.h` is git-ignored; never commit it.

4. Broker password for the commands below, without putting it on screen or in the shell history (set it once per terminal):

   ```bash
   read -rs MQTT_PASSWORD; export MQTT_PASSWORD
   ```

## 2. PC-only checks (no hardware)

1. Batching model test of the frames stream and the checker (standard library only):

   ```bash
   python3 -I tools/tests/frames_batcher_model.py
   ```

   Expected: every line `ok`, last line `ALL PASSED`, exit code 0.

2. Firmware build:

   ```bash
   pio run
   ```

   Expected: `[SUCCESS]`, RAM about 17.6 %, flash about 36.4 %, no warnings from `src/` or `include/` (the `FFTReal.h` warnings come from the audio-tools library). If PlatformIO only installs packages and exits, run it again. The image is `.pio/build/seeed_xiao_esp32s3/firmware.bin`.

## 3. Flashing

- **First time, or when OTA is not possible:** USB-C, then `pio run -t upload`. Serial log: `pio device monitor -b 115200`.
- **Over WiFi (OTA):** open `http://<node-ip>/`, choose `firmware.bin`, flash. Or from the command line:

  ```bash
  curl -F firmware=@.pio/build/seeed_xiao_esp32s3/firmware.bin http://<node-ip>/update
  ```

  The node answers `OK, rebooting` and restarts. The web page then shows the MD5 of the running firmware; compare it with `md5 -q .pio/build/seeed_xiao_esp32s3/firmware.bin` (macOS) or `md5sum` (Linux).

## 4. P1 on the device: radar frames stream

Watch all radar topics in one terminal while you do the steps (add `-h <broker>` if it is not local):

```bash
mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -t 'led_matrix_node/radar/#'
```

1. **Boot.** The serial log shows `[MQTT] frames boot id xxxxxxxx`; the id changes at every reboot. No `[MQTT] discovery ... skipped` line.
2. **Empty room.** A message on `led_matrix_node/radar/ld2454/frames` every 5 s with `"n"` about 50 and no `"f"`.
3. **Walk in.** The first message with `"f"` within a fraction of a second, then about 2 messages per second. `"s"` (seq) continues without jumps: each message's `s` equals the previous `s + n`.
4. **Leave.** One more message about 0.5 s after the last one with targets, then the 5 s heartbeats again.
5. **LD2412.** `led_matrix_node/radar/ld2412` (retained, `1`/`0`) only on change and every 60 s, and right after each reconnect.
6. **Legacy LD2454 JSON.** Nothing on `led_matrix_node/radar/ld2454` (only `.../ld2454/frames`).
7. **Home Assistant discovery unchanged.** The device *Led Matrix Node* still has exactly CO2, Temperature, Humidity and Illuminance, with the same entity ids and history, and nothing new or unavailable. Raw check:

   ```bash
   mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -C 4 -W 5      -t homeassistant/sensor/Led_Matrix_Node_co2/config -t homeassistant/sensor/Led_Matrix_Node_temp/config      -t homeassistant/sensor/Led_Matrix_Node_hum/config -t homeassistant/sensor/Led_Matrix_Node_lux/config
   ```

8. **WiFi or broker outage.** Switch the broker off, or block the node at the router, for about 60 s, then restore it. On a WiFi drop the serial log shows `[MQTT] Wi-Fi lost, closing the MQTT connection`; after the reconnect the node publishes `online`, the LD2412 value is re-sent, and about a minute later `[FRAMES] dropped since boot: ... MQTT down` appears. The frames seq jumps once by the frames dropped while offline.
9. **Aborted OTA.** Start an upload on the web page and close the page halfway (or upload a file that is not firmware). The node does not reboot; the serial log shows `[LD2454] resumed after OTA pause, N stale frames kept out of the stream`, and fresh frames continue. Do not do this during the 24 h run (step 12): the checker cannot tell an OTA pause from a hang.
10. **Completed OTA.** Flash normally: a new boot id, and seq starts again near 0.
11. **Unchanged behaviour.** Play the audio stream: the spectrum view enters and leaves as before, including the hold while someone is in the room. The panel does not flicker while someone moves in front of the radar.
12. **24 h checker** on a computer that stays on (venv from section 1 active):

    ```bash
    python3 tools/frames_checker.py --host <broker> --user <user> --gap-log gaps.jsonl
    ```

    It runs 24 h (`--duration` in seconds to change it), prints an interim summary every hour and a final one. Pass: `RESULT: PASS` (exit code 0); exit code 1 means a check failed, 2 a usage error. It needs an MQTT 5 broker (the Home Assistant Mosquitto add-on is one). An unexplained seq gap means MqttTask was blocked for more than about 6 s or a publish failed while connected: look at the serial `[FRAMES]` line from that time. Delete `gaps.jsonl` afterwards or keep it outside the repo.

## 5. Hardware: option A board revision

1. **Schematic.** Open `schematics/LedMatrixSensorNode/LedMatrixSensorNode.kicad_pro` in KiCad 10, run *Inspect → Electrical Rules Checker*, and fix or note every error. Check the new parts below the XIAO: Q1 AO3401A, Q2 AO3400A, R5–R9, C12, C13, and R2 marked *do not fit*. Then *File → Plot* (PDF) and replace `LedMatrixSensorNode.pdf`.
2. **Board rework** (as drawn):
   - Q1 (AO3401A) switches VBUS to a new RADAR_5V rail feeding pin 1 of J5 (LD2454) and J6 (LD2412). Q2 (AO3400A) drives its gate from GPIO9 (D10) through R6 220 Ω; R7 100 k pulls the Q2 gate up to 3V3, so the radars are ON while the ESP is in reset. Soft start: R9 1 k, C13 470 nF. C12 100 µF on VBUS at the Q1 source.
   - Cut the trace from J8 pin 2 to GPIO9. The power/LED-off button (J8 pin 2) goes through R8 10 k to GPIO8; the mode button (J8 pin 3) stays direct to GPIO8; R2 is not fitted.
3. **Bench measurements (P0)** before the P2 firmware, with an oscilloscope and a multimeter:
   - Until the P2 firmware is flashed, the current firmware still treats GPIO9 as the power button input (with pull-up), so the radars stay ON; pulling GPIO9 to GND with a jumper switches them off (and the firmware sees a button press, which toggles the display). With it: both modules' 5 V switch on and off. RADAR_5V rise time (about 1 ms expected) and fall time (about 20–45 ms expected).
   - VBUS at switch-on stays at or above about 4.8 V; the ESP does not reset.
   - The radars are ON while the ESP is held in reset (EN low).
   - GPIO8 voltage: idle about 3.3 V, mode button 0 V, power button about 1.65 V (about 1.8 V while the current firmware still enables the internal pull-up). Until the P2 firmware reads GPIO8 as an analog ladder, the power/LED-off button does nothing on the reworked board (1.65 V is not a digital low); the mode button works as before.
   - LD2412 OUT and the LD2454 UART lines never exceed 3.3 V, with the radars on or off.

## 6. Line endings and file modes

- `.gitattributes` keeps text files LF in the repository and native in the working tree; the add-on (`ha-addon/`), shell scripts and Dockerfiles are always LF because they run on Linux.
- Executable scripts carry the executable bit in git (mode 100755). On Windows, set it with `git update-index --chmod=+x <file>` (Windows does not track it on disk); on macOS and Linux, `chmod +x` before `git add`.
- To check a checkout: `git ls-files --eol` (index column `i/lf` for text) and `git ls-files -s` (100755 for scripts). `git status` should be clean right after a clone.

## 7. Learner app on Home Assistant (P3, shadow mode)

The app learns where people are in the room from the P1 frames stream. In P3 it only **observes and suggests**: it creates no zones, drives no automations and publishes nothing to `led_matrix_node/#`. Full install and update steps are in [`ha-addon/radar_zone_learner/README.md`](ha-addon/radar_zone_learner/README.md), section 3; the entities are in section 4 and "Mark spot here" in section 5.

1. **Install and start** (README section 3). The log shows `connected to core-mosquitto:1883`, `subscribed (9 topics)`, and `status learning` once frames arrive. Turn on **Watchdog** on the Info tab.
2. **Nothing reaches the node.** The app must not publish to the node's topics:

   ```bash
   mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -t 'led_matrix_node/#' -C 20 -W 60
   ```

   Only the node's own messages should appear (radar, sensors, status); no line may come from the app.
3. **Learner status** (entity "Learner status"). It reads `learning` in the room during the day and `no_data` only when the node or the broker is down. Its attributes give `nights` (0 on the first day, growing by one a night), `covered_days`, `observed_h_24h`, and `ingest` (frame counters, with lost frames and sequence gaps; a few gaps during a Wi-Fi blip are normal).
4. **Debug map** (entity "Learner debug map"). The picture shows the room's radar coordinates and where time was spent. It appears after the first nights; before that it is empty.
5. **Mark spot here** (README section 5). Type "Desk" into "Spot name", sit still at the desk for 20 s or more, press the button, and read `last_mark` in the status attributes: `"result": "ok"` means the label is stored. Before the P2 night schedule the radars run all day, so any hour works for this test.
6. **Bed label (later).** Press "Mark spot here" with "Spot name" set to `Bed` while lying in bed, only after the P2 night schedule is running; the window is 00:00–06:00, so press between 06:00 and 00:00.
7. **Resource check** (the acceptance test for the CM4). Leave the app running for 24 h and read the status attributes:
   - `rss_mb` below **40**;
   - `cpu_pct_1h` below **1.0**;
   - `core_errors` and `nightly_errors` at **0**.

   Over the same 24 h, the node's frames check (section 4, step 12) should still pass.
8. **Updates** (README "Updating"): copy the new files with LF endings, raise `version` in `config.yaml`, then Settings → Apps → Radar Zone Learner → Update, or **Rebuild** on the Info page.

**Not verifiable on a PC:** the image build on the CM4, the Supervisor's watchdog and restart behaviour, and the actual suggestions. Suggestions appear in the simulation around nights 4–7 for the desk, table, counter and sofa, and around night 14 for the bed, with 00:00–06:00 as the window. Real rooms differ, so compare the first suggestions with what you know about the room.

## 8. P2 on the device: radar switch, night schedule, ADC buttons (board revision 2)

P2 adds the radar power switch on GPIO9, the night schedule (default 00:00-06:00, off on a fresh flash), manual radar control over MQTT and Home Assistant, and the two buttons as a resistor ladder on GPIO8. All of it is compiled **only for board revision 2** (build flag `BOARD_REV2`, environment `seeed_xiao_esp32s3_rev2`). The default environment is the P1 firmware, unchanged. The reference for every entity, topic and constant is the README (sections *Build variants*, *Board revision 2* and *Radar control and state*).

> **WARNING: never flash the rev2 image on the unreworked (current) board.** On the current board GPIO9 (D10) is the power / LED-off button: a pull-up and a switch to GND. The rev2 firmware drives GPIO9 as an output (HIGH at boot, so the radars are on), so every press of that button shorts a driven pin to GND, and the mode button on GPIO8 is read as an analog ladder, so the buttons decode wrongly. The other mix does no harm but is useless: the default image on the reworked board keeps the radars on and leaves the power button dead (section 5). `pio run` and `pio run -t upload` without `-e` build and flash the **default** image only (`default_envs`). Check what is on a board before you flash: the rev2 image reports `"board":"rev2"` in `/api/ota/status`, the default image has no `board` field.

Do sections 1 to 5 first (the reworked board and the P0 measurements). Watch the radar state, all in one terminal, while you do the steps (add `-h <broker>` if it is not local):

```bash
mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -t 'led_matrix_node/radar/+/state' -t led_matrix_node/radar/state -t led_matrix_node/radar/state/attr -t led_matrix_node/radar/powered -t led_matrix_node/radar/observing -t led_matrix_node/presence -t led_matrix_node/radar/ld2412 -t homeassistant/status
```

1. **PC-only checks** (no hardware). The new model tests and the extended frames model; every one ends with `ALL PASSED` and exit code 0 (standard library only). The check counts catch a silent change:

   ```bash
   for t in frames_batcher_model radar_power_model ld2454_park_model discovery_payloads_check mqtt_radar_model ladder_model ld2412_presence_model board_gate_check; do
     echo "== $t"; python3 -I -B tools/tests/$t.py | tail -2
   done
   ```

   Expected counts: `radar_power_model` 296, `ld2454_park_model` 141, `discovery_payloads_check` 205, `mqtt_radar_model` 190, `ladder_model` 52, `ld2412_presence_model` 84, `board_gate_check` 197 (`frames_batcher_model` prints only `ALL PASSED`). They mirror the C++ and read its constants and statement order from the sources, so a change in the firmware that the model does not follow fails them. They do not run the C++.

2. **Build both variants.**

   ```bash
   pio run                              # current board (default_envs)
   pio run -e seeed_xiao_esp32s3_rev2   # board revision 2
   ```

   Expected: `[SUCCESS]` twice, no warnings from `src/` or `include/` (the `FFTReal.h` warnings come from the audio-tools library). If PlatformIO only installs packages and exits, run it again. RAM and flash:

   | Image | RAM | Flash |
   |---|---|---|
   | default (`.pio/build/seeed_xiao_esp32s3/firmware.bin`) | 57652 B (17.6 %) | 1143882 B (36.4 %), the P1 numbers exactly |
   | rev2 (`.pio/build/seeed_xiao_esp32s3_rev2/firmware.bin`) | 58972 B (18.0 %), +1320 B | 1178754 B (37.5 %), +34872 B |

   The default image has no radar code at all: `xtensa-esp-elf-nm` on its `firmware.elf` shows no `radarPower*` / `radarStatusSnapshot` symbol, and `strings .pio/build/seeed_xiao_esp32s3/firmware.bin | grep -c radar/power` prints `0` (the rev2 image: 3). To prove that the default image is the P1 firmware, build `git archive HEAD` of the P1 commit into an empty folder and compare every `.pio/build/seeed_xiao_esp32s3/src/*.cpp.o` with `xtensa-esp-elf-size -A` and `xtensa-esp-elf-objdump -dr`: all project objects are identical, `radar_power.cpp.o` has only zero-size sections, and the only difference in `firmware.elf` is the length of build-folder paths inside the audio-tools assert strings (`.rodata`). The numbers in the table are those of the repository folder `C:/dev/my/LedMatrixSensorNode`; another folder changes the flash size by a few bytes for that reason.

3. **Flash the rev2 image** (only on the reworked board). By USB, `pio run -e seeed_xiao_esp32s3_rev2 -t upload`, or over WiFi as in section 3 with the rev2 path:

   ```bash
   curl -F firmware=@.pio/build/seeed_xiao_esp32s3_rev2/firmware.bin http://<node-ip>/update
   ```

   Expected: the serial log starts with `[BOARD] BOARD_REV2 firmware: GPIO9 = radar power enable (output), GPIO8 = button ladder` (open `pio device monitor -b 115200` first, then reset); `curl -s http://<node-ip>/api/ota/status` contains `"board":"rev2"` and a `radar` object; the web page has a *Radars* card. On a fresh flash the radars are on and the schedule is off: 'Radar state' `forced_on` (reason `manual`), 'Radar power' ON, 'Radar observing' ON about 12-15 s after boot.

   Log lines to expect (rev2 only):

   | When | Serial log |
   |---|---|
   | Boot, radars on | `[LD2454] link opened, power epoch 1`, `[LD2454] first frame after power-up - configuring multi-target mode`, `[LD2454] multi-target tracking enabled` |
   | Stack headroom | `[RADAR] stack free: N B` (RadarPower, each time it reaches a new low; keep N at 1024 or more), `[MQTT] stack free: N B` (once, after the first connect; keep N at 1500 or more) |
   | State or reason changes | `[RADAR] state <state> (<reason>)`, for example `[RADAR] state night_off (schedule)`, `[RADAR] state forced_on (pending)` while an OFF waits, `[RADAR] state failsafe_on (ha_unknown)`. Not printed for the boot default (`forced_on (manual)`, or `forced_off (manual)` with a stored manual OFF) |
   | Radars off | `[RADAR] park requested`, `[RADAR] state ... (pending)`, then `[RADAR] parked - radars off (GPIO9 LOW)` and the new state (`forced_off (manual)` or `night_off (schedule)`) |
   | Radars on | `[RADAR] radars on (GPIO9 HIGH)`, the new state, then `[LD2454] link opened, power epoch N` |
   | A setting changes | `[RADAR] en = 0` (`en`, `sched`, `ns`, `ne` in minutes, `offl`, `legacy`); the web page's switch logs `[Web] radar en=0`; a refused payload logs `[MQTT] led_matrix_node/radar/power 'auto' ignored`; a failed NVS write logs `[RADAR] NVS write failed: en`, or `[RADAR] NVS write failed: en (applied to RAM only)` when 'Radars' is switched ON (the radars still come on) |
   | Park refused | `[RADAR] fault park_timeout - the radars stay on` (no acknowledgement for 6 s), `[RADAR] park withdrawn` |
   | Faults | `[LD2454] no frame within the boot timeout - fault, trying the configuration anyway` (8 s after a power-up), `[RADAR] LD2454 sends no frames - power-cycling the radars` |
   | After an aborted OTA | `[LD2454] resumed after OTA pause, N stale frames kept out of the stream` |
   | Never | `[RADAR] TWDT: ...` (a watchdog call failed), `[MQTT] discovery ... skipped` |

4. **Home Assistant setup** (README, *Setup (Home Assistant)*):
   1. Home Assistant 2026.5 or newer (the night start and end are MQTT `time` entities).
   2. Settings -> Devices & services -> MQTT -> Configure -> MQTT options: enable **retain** for the birth message and the will message. Check:

      ```bash
      mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -t homeassistant/status -C 1 -W 3
      ```

      Expected: `homeassistant/status online` at once (a retained message). Nothing within 3 s means retain is off; the node then never learns HA's state and, after 5 minutes inside the night window, keeps the radars on with reason `ha_unknown` (the web page shows a hint).
   3. **Discovery.** Under the existing device *Led Matrix Node*, with no YAML: Radars, Radar night schedule, Radar power, Radar observing, Radar state, Radar presence, Radar schedule without HA, Legacy radar JSON, Radar night start and Radar night end, with the entity ids of the README table (`switch.led_matrix_node_radars`, `switch.led_matrix_node_radar_night_schedule`, `binary_sensor.led_matrix_node_radar_power`, ...; the README marks the ids that are predicted, so write down the real ones). The four sensors keep their entity ids and history. Raw check, 14 retained configs (4 sensors, 10 radar entities) and no 'discovery ... skipped' line in the serial log:

      ```bash
      mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -t 'homeassistant/+/+/config' -W 5 | grep -c Led_Matrix_Node_
      ```

      Expected: `14`.

      The sensor configs are byte for byte what P1 sent. To prove it, save them with the default image on, flash rev2, save them again and compare:

      ```bash
      mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -C 4 -W 5 -t homeassistant/sensor/Led_Matrix_Node_co2/config -t homeassistant/sensor/Led_Matrix_Node_temp/config -t homeassistant/sensor/Led_Matrix_Node_hum/config -t homeassistant/sensor/Led_Matrix_Node_lux/config | sort > sensors_before.txt
      # flash rev2, then the same command into sensors_after.txt
      diff sensors_before.txt sensors_after.txt && echo identical
      ```
   4. Switch **'Radar night schedule' ON**. Outside the window the state becomes `on` and `next` in the attributes reads `00:00`.

5. **Night schedule on the bench.** With the schedule ON, the radars on for at least 60 s, and the clock valid, shorten the window to the next minutes (plain `HH:MM` is accepted; never publish with `-r`). Replace `hh:mm` with the time two minutes from now, and the end time with five minutes from now:

   ```bash
   mosquitto_pub -h <broker> -u <user> -P "$MQTT_PASSWORD" -t led_matrix_node/radar/night_start -m hh:mm
   mosquitto_pub -h <broker> -u <user> -P "$MQTT_PASSWORD" -t led_matrix_node/radar/night_end -m hh:mm
   ```

   Expected: the states echo as `HH:MM:00`. At the start minute (at most about 1.3 s after it) 'Radar state' becomes `night_off`, the log shows `park requested` and `parked - radars off (GPIO9 LOW)`, 'Radar power' turns OFF, and the frames carry `"pw":0,"sm":"N"` (item 9). At the end minute the radars come back: state `on`, 'Radar observing' ON no earlier than 12 s after GPIO9 rises. Then restore the defaults: `-m 00:00` and `-m 06:00` (HA's time entities show `00:00:00` and `06:00:00`, and round-trip a value such as `23:00:00`).

6. **Bench with a scope** (the P2 list of the design; probes on GPIO9, TX = GPIO44 / D7, RADAR_5V at J5 and J6, GPIO8, VBUS):
   - **Park order.** Send a manual OFF (item 7, test 2). TX goes high-Z before GPIO9 falls; RADAR_5V is below 0.5 V at J5 and J6 within `RADAR_OFF_SETTLE_MS` (50 ms; the P0 fall time of section 5 sets the limit, raise the constant if the fall is slower) of GPIO9 falling; TX stays high-Z and nothing is back-fed while the radars are off.
   - **Switch on.** RADAR_5V starts about 0.2 ms after GPIO9 rises and ramps at 6-8 V/ms; VBUS stays at or above about 4.8 V and the ESP does not reset; the I2C sensors (CO2, temperature, illuminance) stay error-free during switching.
   - **Boot with a stored manual OFF.** After a power cycle or an EN reset: GPIO9 shows the R7-driven HIGH through reset, then the LOW from NVS, with no extra pulse when the output is enabled; GPIO44 is high-Z before GPIO9 falls. After a software restart (`ESP.restart()`, for example the web page's OTA restart): GPIO9 stays LOW throughout, with no ON pulse. With the radars on, GPIO9 stays HIGH through either kind of reset.
   - **Software restart during `night_off`** (the radars are off, the restart keeps GPIO9 LOW until `setup()` drives it HIGH): that LOW time is the boot time and must be well above the measured fall time.
   - **OTA during `night_off`.** A 60 s upload completes with no watchdog panic and no `[RADAR] TWDT: ...` line; GPIO9 does not change during the upload; the task rejoins the watchdog afterwards (a debug build with `esp_task_wdt_status(NULL)` shows it).
   - **No visible panel flicker** while the display task polls the ladder every 10-50 ms and on press edges; repeat with a camera at 240 fps if in doubt. The audio stream keeps playing unchanged through a manual OFF, a `night_off` and the following power-ups. LD2412 OUT (GPIO2) reads LOW at every poll while the radars are off, and with them on 'LD2412 presence: DETECTED / CLEAR' still tracks OUT within 500 ms.

7. **Manual control over MQTT** (design 5.6 j, tests 1 to 10). 'Radars' is `led_matrix_node/radar/power`; its state is `led_matrix_node/radar/power/state`. Use the watch terminal from above.
   1. **Discovery:** step 4 (the three entities, no 'discovery ... skipped' line).
   2. **Manual OFF.** Outside the 60 s minimum ON time and with no LD2454 configuration running: `mosquitto_pub -h <broker> -u <user> -P "$MQTT_PASSWORD" -t led_matrix_node/radar/power -m 0`. `power/state` becomes `0` within 1 s; on the scope TX is high-Z before GPIO9 falls and RADAR_5V is below 0.5 V at J5 and J6 within `RADAR_OFF_SETTLE_MS`; 'Radar power' turns OFF, 'Radar state' is `forced_off` with `reason: manual`, and the frames heartbeat carries `"pw":0,"sm":"X"`.
   3. **Manual ON.** `-m 1`, at least 10 s later: outside the window GPIO9 rises, RADAR_5V starts about 0.2 ms later and ramps at 6-8 V/ms, and 'Radar observing' turns ON after the warm-up, no earlier than 12 s after GPIO9 rose; inside the window the state is `night_off`.
   4. **Payloads.** `ON`, `off` and `On` are accepted. `auto`, `2` and an empty payload are ignored and logged (`[MQTT] ... ignored`), and NVS is not written (no `[RADAR] en = ...` line for them).
   5. **Persistence.** Send `0`, then power-cycle the board (USB): the radars are on through reset (R7), then off again at `setup()`, with TX parked first. Restart it from the web page (`ESP.restart()`): GPIO9 stays LOW throughout, with no ON pulse (scope).
   6. **Reconnect.** Cut WiFi or stop the broker for a minute. After the reconnect the node republishes the discovery and every state, HA shows the right values, and the attribute `ha` of 'Radar state' is `online` right after the reconnect (HA's retained birth). Stalled broker: right after a reconnect freeze the broker host (or drop the node's packets with a firewall rule) for a minute. The node must not block: the serial log shows at most one `[MQTT] <step> refused, closing the connection` line per attempt (step = discovery, command clear, subscribe, states, online, birth discovery or birth states), the frames stream drops frames instead of stalling for minutes, HA shows the entities unavailable, and when the broker answers again the next attempt republishes the whole burst (14 configs, the states, `online` last). Repeat with a broker restart without persistence: `ha` is `online` once HA has reconnected and sent its birth again. Then, inside the window with 'Radar schedule without HA' OFF, switch the access point off for 6 minutes: GPIO9 rises and the log shows `[RADAR] state failsafe_on (ha_unknown)` 5 minutes after the node lost WiFi, not 5 minutes after the reconnect (with HA's retained `offline` the reason is `failsafe`).
   7. **Stray retained command.** With the radars on, `mosquitto_pub -h <broker> -u <user> -P "$MQTT_PASSWORD" -r -t led_matrix_node/radar/power -m 0`: the radars go off once, as for a live command. Switch them on from HA and force a reconnect (briefly stop the broker): they stay on. `mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -t led_matrix_node/radar/power -C 1 -W 3` then receives nothing: the retained message is gone. This also proves that an empty retained publish before the subscribe works on the real broker.
   8. **Rapid toggling.** (a) Outside the window, with the radars on for more than 60 s, send `0` and `1` alternately, 10 commands within 10 s, the last one `1`. The first `0` switches the radars off after the park; the rest are deferred by the 10 s minimum OFF time. GPIO9 goes LOW once and HIGH once, about 10 s after it fell (the last command wins); `power/state` follows the stored value and ends at `1`; while the ON waits, 'Radar state' is `forced_off` with `reason: pending`. (b) Optional throttle run, about 15 minutes: inside the window with 'Radars' ON, switch 'Radar night schedule' OFF for 62 s, then ON for 12 s, repeatedly. After the 12th power-up within the hour the next schedule OFF is refused: 'Radar state' `failsafe_on` with `reason: throttle` and `thr` 1. A manual `0` then still switches the radars off once the 60 s minimum ON time has passed.
   9. **OTA, with the radars manually OFF.** Send `1` during an upload of under about 20 s that is then aborted: it is applied after the tasks resume. Send `1` during an upload of more than 30 s that is then aborted: the broker drops the node after about 22 s (keepalive), every entity turns `unavailable`, and the command is lost; after the reconnect HA shows `0` again. Complete an upload with `0` stored: GPIO9 stays LOW across the restart with no ON pulse, and HA shows `0` again after the reconnect.
   10. **HA restart.** Restart Home Assistant. The discovery and the states are re-sent 0-2 s after its birth (one burst, not two), and the three entities show the right state.
   11. **Flapping client.** Publish `0` and `1` alternately to `led_matrix_node/radar/power` about 20 times within one second (a `mosquitto_pub` loop with a 40 ms sleep, ending on `1`). At most two `[RADAR] en = ...` lines appear (the first at once, the last value about 1 s later), the switch in HA ends on the last payload and the panel blanks at most twice. Publishing the same payload repeatedly produces no `[RADAR] en = ...` line.
   12. **Multi-command update.** With the schedule on, a window that does not contain the current time and the radars on, publish night start, night end and schedule within 100 ms so that the intermediate window would contain the current time and the final one does not. The radars never switch off ('Radar observing' stays ON, GPIO9 stays HIGH, the frames stream does not pause) and 'Radar state' stays `on` (reason `pending` for at most half a second). A window that really starts by the clock still parks without delay. A failing NVS write cannot be provoked on the device; it is covered by `tools/tests/radar_power_model.py` only.

   Also: **toggle latency** in HA (after 60 s ON or 10 s OFF, no LD2454 configuration in progress) is under 1 s; the bound otherwise is the remaining guard time plus the park of at most about 4.6 s; a command is never lost while it waits. After an HA restart and after a broker restart (with and without persistence) every entity reappears with the right state.

8. **The web page and `/api/radar`.** The *Radars* card shows state, reason, window, schedule, power-ups, HA state and the ladder voltage; *Radars OFF* asks for a confirmation and switches off like MQTT's `0`, *Radars ON* hands control back to the schedule.

   ```bash
   curl -s -d en=0 http://<node-ip>/api/radar            # {"en":0}
   curl -s -d en=1 http://<node-ip>/api/radar            # {"en":1}
   curl -s -H 'Content-Type: application/json' -d '{"en":0}' http://<node-ip>/api/radar
   curl -s -d en=2 http://<node-ip>/api/radar            # 400 expected {"en":0} or {"en":1}
   curl -s -i -H 'Origin: http://evil.example' -d en=0 http://<node-ip>/api/radar   # 403 forbidden: request from another website
   curl -s -i -H 'Host: evil.example.com' -d en=0 http://<node-ip>/api/radar        # 403 forbidden: unknown host name
   ```

   Expected: the first three answer 200 and the radars follow (end with `en=1`), the last three are refused and change nothing. This is also the way back while Home Assistant is down.

9. **Frames while the radars are off** (the stream is a 30 s heartbeat). With the radars off (a manual OFF or `night_off`):

   ```bash
   mosquitto_sub -h <broker> -u <user> -P "$MQTT_PASSWORD" -v -t led_matrix_node/radar/ld2454/frames -t led_matrix_node/radar/ld2412
   ```

   Expected: one message every 30 s, such as `{"v":1,"b":"9f3c21aa","t":81244567,"s":1234615,"n":0,"pw":0,"sm":"X"}` (`"sm":"N"` for `night_off`), with no `mt`, `p2` or `f`, and `s` unchanged from message to message (`n` is 0). `led_matrix_node/radar/ld2412` is a retained `None`. From the moment the radars are powered again the stream is 5 s again. `pw` is 0 for the first 12 s, then 3 once both radars observe (2 while the LD2454 is not ready, for example after a fault); `mt` appears with bit 0 and `p2` with bit 1. The long run:

   ```bash
   python3 tools/frames_checker.py --host <broker> --user <user> --phase p2 --gap-log gaps.jsonl
   ```

   It runs 24 h (venv of section 1 active); `--phase p2` allows the 30 s heartbeat with `pw` 0 and requires `mt` / `p2` only with their `pw` bit. Pass: `RESULT: PASS`. Do not run an OTA upload during it (section 4, step 9: the checker cannot tell an OTA pause from a hang).

10. **Buttons on the GPIO8 ladder** (rev2). `curl -s http://<node-ip>/api/ota/status` shows `radar.ladderMv` (the card shows it as `buttons: N mV`): idle about 3000-3300, mode button about 0, power button about 1650. The bands are `LADDER_*` in `include/radar_constants.h` (mode up to 500 mV, power 1150-2150 mV, idle from 2600 mV); change them only if the real cable reads outside them. Expected: the mode button cycles the views as before, the power button blanks and wakes the panel, both pressed together act as the mode button, a button held during boot acts only after it is released and pressed again, and 600 presses (300 each, count them on screen) give 0 misdecodes and no double actions.

11. **Default image regression.** Flash the default image on the current board (section 3 with the default `firmware.bin`): the buttons and the power toggle work as in P1, the radars stay on, and the LD2454 and the frames stream behave as in section 4. Nothing new appears: no `[BOARD]` or `[RADAR]` line, no `board` field in `/api/ota/status`, no `/api/radar` route, and `mosquitto_sub -v -t 'homeassistant/+/+/config' -W 5` still lists only the four sensors from this node. The retained radar configs and states of a rev2 run stay on the broker (and as unavailable entities in HA) until you delete them, with the entities in HA or:

    ```bash
    for c in switch/radar_power switch/radar_schedule binary_sensor/radar_powered binary_sensor/radar_observing sensor/radar_state binary_sensor/radar_presence switch/radar_sched_offline switch/radar_legacy time/radar_night_start time/radar_night_end; do
      mosquitto_pub -h <broker> -u <user> -P "$MQTT_PASSWORD" -r -n -t "homeassistant/${c%%/*}/Led_Matrix_Node_${c##*/}/config"
    done
    ```

12. **One live week** (rev2, the schedule ON, the window 00:00-06:00). In the history of 'Radar state' there is exactly one OFF and one ON per night, at 00:00 and 06:00 (each within a couple of seconds); no unexpected reset (the web page's uptime and the `b` boot id in the frames); no misdecoded button press; the 24 h frames check of step 9 passes; and, if the learner of section 7 runs, it treats those hours as censored (no stays, no empty-room evidence) and shows its zones `unavailable` while the radars are off.

**Not verifiable on a PC** (so these are the bench items above): the C++ itself (only the Python models and the source-link checks ran), GPIO9 across `ESP.restart()`, the real RADAR_5V fall time against `RADAR_OFF_SETTLE_MS`, the stack headroom of RadarPower and MqttTask, the watchdog across a 60 s upload, the empty retained publish on the real broker, the ladder levels on the real cable, and the HA entity ids.
