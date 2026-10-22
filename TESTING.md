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
