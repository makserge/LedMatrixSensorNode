# Radar Zone Learner (Home Assistant app)

A local Home Assistant app ("add-on" in older Home Assistant versions) that learns the places people use
in a room from the LED matrix node's LD2454 radar frames. Version 0.3.0 is phase P3 of the zone-learning
design: **shadow mode**.

## 1. What it is

The app subscribes to the node's frames stream, tracks the radar targets, finds the places where people
stay (desk, table, sofa, bed, ...), and every night at 03:30 it works out which of them are reliable enough
to suggest. In P3 the suggestions are **only written down**: in the app's state, in the attributes of the
'Learner status' sensor, and on the debug map.

What it does not do in P3:

- no zone entities, no room occupancy, no automations, no notifications;
- no approval of suggestions (that is P5);
- no commands to the node: it publishes nothing under `led_matrix_node/`.

Entities (MQTT discovery):

| Entity | Device | What it shows |
|---|---|---|
| `sensor.radar_zones_learner_status` ('Learner status') | Radar zone learner | `starting`, `learning`, `not_observing` or `no_data`, with the learner's details as attributes |
| `image.radar_zones_learner_debug_map` ('Learner debug map') | Radar zone learner | the learned map (section 4) |
| `button.radar_zones_review_mark` ('Mark spot here') | Radar zone review | marks the spot where you are as a label (section 5) |
| `text.radar_zones_review_name` ('Spot name') | Radar zone review | the name the next mark gets; "Bed" or "Bett" makes it a bed label |

## 2. Requirements

- Home Assistant 2026.5 or newer, on the CM4 or any aarch64 or amd64 host with the Supervisor.
- The Mosquitto broker app, installed and running. The app takes its MQTT login from the Supervisor's MQTT
  service; it needs no own login.
- The LED matrix node on the P1 firmware or later, which publishes the frames stream on
  `led_matrix_node/radar/ld2454/frames`.
- P2 (radar night switch) is not needed. With P2, the time while the radars are off (00:00-06:00) is
  recognised from the frames stream and left out of learning automatically.

## 3. Install as a local app

1. Copy the folder `ha-addon/radar_zone_learner` of this repository (with `radar_zone_learner/`,
   `config.yaml`, `Dockerfile`, `run.sh`, `requirements.txt` and `translations/`) into the local apps
   folder of the Home Assistant host, as `/local_apps/radar_zone_learner`, through the Samba share or the
   Terminal & SSH app. On older installations this folder is called `addons` and appears as `/addons`.
   `tests/` and `replay/` may be copied too; they are not built into the image.
2. Keep the LF line endings: copy from the git checkout (its `.gitattributes` keeps every file of this
   folder LF, also on Windows). A `run.sh` with CRLF line endings does not start.
3. In Home Assistant: Settings → Apps → Install app (the app store) → ⋮ → Check for updates. Refresh the
   page if needed. "Radar Zone Learner" appears under **Local apps**.
4. Open it and press Install. The host builds the image, which takes a few minutes and downloads the
   Home Assistant Python base image and paho-mqtt. Then press Start and turn on **Watchdog** on the Info
   tab.
5. The log shows `connected to core-mosquitto:1883`, `subscribed (9 topics)` and, once frames arrive,
   `status learning`. Every later change of the learner status is logged the same way.

### Updating

A local app is built from the folder once, at install time. Copying new files over
`/local_apps/radar_zone_learner` changes nothing until the image is built again, and the Supervisor
offers an update only when `version` in `config.yaml` is higher than the installed one:

1. Copy the new files over the folder (LF endings, as in step 2).
2. Raise `version` in `config.yaml` (and `APP_VERSION` in `radar_zone_learner/core/types.py`), then
   Settings → Apps → the app → Check for updates → Update. Without a new version number press
   **Rebuild** on the Info page instead, or run `ha apps rebuild local_radar_zone_learner` (older CLI
   versions: `ha addons rebuild local_radar_zone_learner`).
3. `/data` and the learned state are kept in both cases.

### Configuration

| Option | Default | Meaning |
|---|---|---|
| `log_level` | `info` | `debug`, `info`, `warning` or `error` |
| `node_prefix` | `led_matrix_node` | MQTT prefix of the LED matrix node |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix of Home Assistant (for the discovery config topics only; the app listens for Home Assistant's birth message on `homeassistant/status`, the default birth topic, which does not follow the prefix) |
| `debug_map` | `true` | publish the debug map image; `false` removes the image entity |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | unset | optional own broker login instead of the Mosquitto app service; used only when `mqtt_username` is set (host default `core-mosquitto`, port 1883) |

The app has no web UI and opens no port on the host. Port 8099 inside the container is only for the
Supervisor watchdog.

## 4. Entities and how to read the shadow suggestions

### Learner status

| State | Meaning |
|---|---|
| `starting` | the app has not received anything yet |
| `learning` | frames arrive and the radar observes the room |
| `not_observing` | frames arrive, but the radars are off or warming up (night window, manual off), the node is offline, or the frames stopped for more than 15 s |
| `no_data` | no frames message for 75 s: the node is not on the P1 firmware, the prefix is wrong, or the node or broker is down |

The attributes (updated on every state change, after each nightly run and mark, otherwise every 5 minutes)
contain, among others:

- `suggestions`: the shadow place suggestions (up to 20, by confidence) with `id`, `type`
  (`dwell_zone` for places where people stay, `short_task_zone` for short repeated visits), `kind`
  (`dwell`, `short_task` or `bed`), `name` (from a label or "bed"), `night` (first night suggested),
  `conf` (confidence 0-1), the centre `x_cm`/`y_cm` in radar coordinates, `state` and the number of
  time-of-day `variants`;
- `candidates`, `reflections`, `doors`, `outline`, `ignore_zones`: counts of last night's other results;
- `nights`, `last_nightly`, `covered_days` (days with at least 1 h observed), `observed_h_24h`;
- `labels`, `label_recall`, `label_precision`, `last_mark` (section 5);
- `ingest`: frame and message counters since the app started (lost frames, sequence gaps, parse errors);
- `schedule`: the node's night window as last reported (P2);
- the app's own health: `rss_mb`, `cpu_pct_1h`, `queue_dropped`, `payload_oversize` (messages dropped
  for their size), `mqtt_reconnects`, `state_saved`, `state_bytes`, `core_errors`, `nightly_errors`
  (nightly runs that raised an error; the log names where).

A place normally needs about a week of normal use before it is suggested: it must be used on at least 4
different days and be seen at the same spot on 3 nights in a row.

### Debug map

A 280 × 160 pixel map of the radar's view from above, 0.2 m per cell (4 pixels), 14 m wide and 8 m deep.
The radar sits at the bottom centre; x runs across the image and y away from the radar. Left and right
follow the radar's own x axis: check them once with 'Mark spot here'.

| Colour | Meaning |
|---|---|
| dark blue → yellow | how much time people stay there (log scale) |
| dark grey edge | the walking envelope (where people walk), also the room outline |
| light grey outline | a candidate that failed a check, or a stale suggestion (not seen for 7 nights) |
| cyan outline | a place suggestion |
| pink outline | a place suggestion of kind bed |
| purple outline | a reflection (a mirror image of a real place, not a place) |
| orange outline | an ignore-zone suggestion (a radar ghost) |
| green squares and outlines | doors |
| white squares | 'Mark spot here' labels |
| pink squares | bed labels |
| light grey lines | the radar marker and its ±60° field of view |

The map is published after the app starts, after each nightly run and each label, and when Home Assistant
restarts. It is not retained on the broker, and Home Assistant subscribes to the image only after it has
created the entity from the discovery config, so the app publishes it a second time 10 seconds after
every discovery burst (at the start and after each Home Assistant restart). Until then the image entity
may show nothing.

## 5. 'Mark spot here'

Labels are ground truth for the learner's checks; in P3 they do not create zones.

1. Type the name of the place into 'Spot name', for example "Desk". "Bed" or "Bett" (any case) makes a
   label of kind bed, which also names a bed suggestion at that spot.
2. Sit or lie still at the spot while the radars observe: outside 00:00-06:00 when the node runs the P2
   night schedule.
3. After at least 20 seconds, press 'Mark spot here'.
4. Read `last_mark` in the status attributes:
   - `"result": "ok"`: the label is stored and appears on the debug map;
   - `no_stay`: the radar sees nobody staying still right now. The LD2454 often drops a person who lies
     still, so stay a little longer, move slightly, and press again;
   - `several_stays`: more than one person is staying still in view; ask the others to move away;
   - `too_soon`: the previous mark was less than 10 seconds ago;
   - `not_observing`: the radars are off or the frames stream stopped.

The name stays set after a mark, so a retry needs only the button. A press at a spot that already has a
label of the same name (within 0.4 m) adds nothing and answers `ok` with the existing label. The app keeps
at most 100 labels (the oldest manual one goes first).

## 6. Privacy

What is stored where:

- `/data/state.json` (inside the app, in every Home Assistant backup): the learned grids, the
  co-visibility matrix, coverage counters, candidates, suggestions, labels and a **14-day stay list**
  (start and end times rounded to 5 minutes, durations to 10 s; at most 28 calendar days). It is saved
  every 30 minutes, after each nightly run, after each label or name change, and when the app stops.
- `/data/snapshots/`: one daily copy of the state, 7 kept.
- In memory only: raw tracks and stay fragments (24 h at most), the LD2412 and context timelines. A
  restart forgets them.
- Nothing is logged with positions at the `info` level.
- The status attributes contain place centres, kinds and counts, never stay times. The debug map is a
  place map without times.

The radar sees everyone in the room. Tell the other people in the household what the radar and this app
record.

Recommended recorder excludes, if you do not want this history in Home Assistant's database (keep them
recorded during the 24 h resource check of section 7):

```yaml
recorder:
  exclude:
    entities:
      - sensor.radar_zones_learner_status
      - image.radar_zones_learner_debug_map
```

**Deleting everything in P3**: stop the app and uninstall it; this removes `/data`. Backups made before
keep their copy. A 'Forget all learned data' button arrives in P5. Uninstalling does not remove what the
app left outside `/data`:

- the retained MQTT messages `radar_zones/learner/state`, `radar_zones/learner/attr` (place centres and
  counts), `radar_zones/learner/status` and `radar_zones/review/name` (the last spot name you typed)
  stay in the Mosquitto broker's own storage (and in its backups). Clear them with an empty retained
  message each, for example `mosquitto_pub -r -n -t radar_zones/learner/attr`, or stop the app first
  and publish from Developer tools → Actions → `mqtt.publish` with an empty payload and `retain: true`;
- the devices 'Radar zone learner' and 'Radar zone review' and their entities stay in Home Assistant's
  entity registry until you delete them (Settings → Devices & services → MQTT → Devices);
- Home Assistant's database (the recorder) keeps up to 10 days of the status sensor's attributes; purge
  the two entities in Developer tools → Actions → `recorder.purge_entities`.

The frames stream itself is plaintext MQTT on the local network; every MQTT user of the Mosquitto app can
read it (see the design's privacy section).

Clutter learning uses `radar_zones/context/home_empty` (`on`/`off`, retained). Nothing publishes it in
P3; the P4 blueprint will. Without it, learning works as usual and only the clutter (ignore-zone) evidence
stays empty.

## 7. Resource check on the CM4

The P3 targets are less than 40 MB of memory and less than 1% of one CM4 core.

1. Keep the app running with frames flowing for 24 hours (the status sensor recorded).
2. In the history of `sensor.radar_zones_learner_status`, the attributes `rss_mb` (resident memory, MiB)
   and `cpu_pct_1h` (the app's CPU share over the last hour, percent of one core) must stay below 40 and
   1.0 at every update.
3. Cross-check on the app's Info page (CPU and memory), or with the HA CLI's stats command for the app id
   `local_radar_zone_learner` (`ha addons stats local_radar_zone_learner`; newer CLI versions call the
   command group `apps`).

Expected: about 20-25 MB and well under 0.1%.

## 8. Troubleshooting

| What you see | What to check |
|---|---|
| No "Radar Zone Learner" under Local apps | Settings → System → Logs → Supervisor: a YAML error in `config.yaml`, CRLF line endings, or the folder copied one level too deep (`/local_apps/radar_zone_learner/config.yaml` must exist). |
| The app stops at once with `No MQTT service: start the Mosquitto broker app, or set mqtt_host, ...` | Install and start the Mosquitto broker app, then start this app again; or give the app its own broker in the options. |
| `broker refused the connection` in the log | With the option override: wrong `mqtt_username`/`mqtt_password`. Without it: the login the Supervisor handed out when this app started is no longer valid, for example after the Mosquitto app was reinstalled: **restart this app** (it asks the Supervisor again). After 5 minutes of refusals the app exits (code 72) and the Supervisor's watchdog restarts it by itself. |
| Status `no_data` | The node is not on the P1 firmware (no frames stream), `node_prefix` does not match the node's prefix, or the node is offline. |
| Status `not_observing` between 00:00 and 06:00 | Expected with the P2 night schedule: the radars are off. |
| The app restarts and the log shows `stall: worker loop has not run for ...` | The watchdog ended a stuck worker loop and the Supervisor restarted the app. The state of the last save is kept. Please keep the log. |
| `more than 100 errors in 600 s: exiting with 71` | A learner error repeats; the Supervisor restarts the app. Please keep the log (it has the tracebacks). |
| `nightly run ... failed: ...` | The nightly extraction raised an error and was skipped for that night; the app goes on and tries again the next night. The log names the error and where it happened; `nightly_errors` counts them. |
| `time zone ... not available: using UTC` | The night run then happens at 03:30 UTC and hours are counted in UTC. Check the time zone in Settings → System → General. |
| `state file unusable ... kept as state.json.corrupt-...` | The state could not be read; the newest valid snapshot was used, or the learner started fresh. The bad file stays in `/data` for inspection. |

## Development

Everything except the MQTT client runs with the Python standard library (3.12 or newer). From this
folder:

```
python -I -B -m unittest discover -s tests -t .
python -I -B -m unittest discover -s tests -t . -p "test_service.py"
```

The tests need no broker and no paho-mqtt: `tests/fakes.py` provides a fake client. Set `RZL_TEST_TMP` to
choose the folder for temporary files. The replay harness in `replay/` runs the learner on simulated days
(see `replay/run_replay.py --help`).
