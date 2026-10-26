# Radar Zone Learner (Home Assistant app)

A local Home Assistant app ("add-on" in older Home Assistant versions) that learns the places people use
in a room from the LED matrix node's LD2454 radar frames. Version 0.4.0 (phase P4, on top of P3) of the zone-learning
design: **shadow mode**. The P4 additions (night window, bed item, optional Night learning, RF and coverage
sensors, the context blueprint) are described in section 9.

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
5. The log shows `connected to core-mosquitto:1883`, `subscribed (18 topics)` (9 before P4) and, once frames
   arrive, `status learning`. Every later change of the learner status is logged the same way.

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
| `night_lux` | `5.0` | lux below this counts as dark (lights off) for the night-window proposal, 0.1 to 500; measure the node's lux with the lights off and on, then set a value between the two |
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

Clutter learning uses `radar_zones/context/home_empty` (`on`/`off`, retained). Nothing publishes it by
itself; the context blueprint of section 9.6 does. Without it, learning works as usual and only the
clutter (ignore-zone) evidence stays empty.

P4 adds `/data/rf_stats.json`: seconds with the radars on, off and observing in 15-minute buckets of the
last 8 days. It holds no positions and no stays, only what the node's own 'Radar state' history shows
(section 9.3). It is deleted with `/data` when the app is uninstalled.

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

## 9. P4: night window, bed item, optional Night learning, RF sensors

Still shadow mode: there are no zone entities, no room occupancy and no presence entity (those come with
P6). The entities below are added by the P4 shell layer; the proposals and the Night learning rules come
from the learner core. An entity whose source is missing shows `None` (review) or `OFF` (Night learning)
and does nothing.

### 9.1 New entities

| Entity | Device | What it shows or does |
|---|---|---|
| `binary_sensor.radar_zones_radar_valid` ('Radar data valid') | Radar zone learner | `on` only while the learner observes (state `learning`), the node is online and its 'Radar observing' is not OFF. Topic `radar_zones/radar/valid` (retained `online`/`offline`, the vocabulary of an availability topic: P6's zone entities will use it as a second one). |
| `sensor.radar_zones_scan_rf_on_24h` ('RF on time 24 h') | Radar zone learner | Hours with the radars on in the last 24 h |
| `sensor.radar_zones_scan_rf_saving_7d` ('RF saving 7 d') | Radar zone learner | Share of the last 7 days with the radars off, % |
| `sensor.radar_zones_learner_coverage_7d` ('Coverage 7 d') | Radar zone learner | Share of the last 7 days (168 h) in which the learner observed, % |
| `switch.radar_zones_learner_night_learning` ('Night learning') | Radar zone learner | OFF by default (section 9.5) |
| `select.radar_zones_review_select` ('Review item') | Radar zone review | `None`, `Night window` or `Bed zone`: the item the buttons act on |
| `sensor.radar_zones_review_pending` ('Review pending') | Radar zone review | Number of open items; the attributes list them with their text |
| `sensor.radar_zones_review_item_text` ('Review item text') | Radar zone review | The text of the selected item |
| `button.radar_zones_review_approve`, `..._reject`, `..._later` | Radar zone review | Answers for the selected item |
| `event.radar_zones_review_event` ('Review event') | Radar zone review | `mark_failed` (with `reason`: `not_observing`, `no_stay`, `several_stays` or `too_soon`), `device_not_confirmed` (with `kind`) and `restore_failed` (with `reason`: `no_confirmation` or `overdue`; section 9.5). An event that cannot go out because the broker link is down is kept (at most 20) and sent when the link is back. |

The status attributes gain `node_schedule_seen`, `node_cmds_sent`, `node_cmds_unconfirmed`,
`node_cmds_refused` and `review_items` from the shell, next to the core's `night`, `review` and
`night_learning`.

### 9.2 'Radar data valid'

The sensor is `off` while the radars are off (night window, manual off), warming up or faulty, when no frames
came for 15 s, and when the node is offline (its last will). Zones, room occupancy and the node's presence
are unavailable at the same times (design 6.5; the zone entities arrive with P6). Write state triggers on
them as `to: 'off'` with `not_from: ['unavailable', 'unknown']`, otherwise the morning power-up fires them:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.your_zone
    to: "off"
    not_from: ["unavailable", "unknown"]
    for: "00:15:00"
```

Nothing sees the room between 00:00 and 06:00 (the radars are off), so no automation can react to presence
in that window.

### 9.3 RF and coverage sensors

They are computed from the node's `radar/state` transitions, kept in `/data/rf_stats.json` and refreshed
every 5 minutes and at each radar state change. 'RF on time' and 'RF saving' show `unknown` until an hour of
the window is known (without the P2 firmware there is no `radar/state` and they stay `unknown`).
'Coverage' is the learner's observing time, so it is below 100% by design (at most 75% with the 00:00-06:00
window) and low in the first week. Time with an unknown radar state (node offline, app stopped) counts as
neither on nor off; a gap of more than 10 minutes in the app's own clock is not counted.

### 9.4 The review items (night window and bed zone)

The core proposes two items; the review device shows them (the full review UI comes with P5):

- **Night window**: 'Switch both radars off between 23:30 and 06:45. Radar on-time -30%. No radar detection
  during that time.' It is proposed from the observed onsets and wakes of at least 7 nights and the
  illuminance sensor: one onset per night (the radar's, else the context's, else the lights-off time), so a
  night on which the radar was blind still counts. A radar onset is the first quiet start of the evening; a
  later one replaces it only within 60 min of the first, so a bathroom trip at 03:00 does not move the bedtime. ('From
  lights-off times' when the lights-off times decide the start.) Approve writes the node's
  'Radar night start' and 'Radar night end', and also switches 'Radar night schedule' ON when the answer
  asks for it (`{"op":"approve","sched":true}`; the buttons do not). The item counts as applied only when
  the node echoes the new values within 2 minutes; otherwise the event `device_not_confirmed` fires.
  Reject = Deny (the item is suppressed for 30 days).
- **Bed zone** (while no zone of kind bed exists): 'Bed zone: lie in bed and press Mark spot here once,
  before 00:00 or after 06:00 (recommended; no extra radar-on nights). Otherwise the learner finds it by
  itself, which can take 2 weeks or more while the night schedule runs.' The 'before ... or after ...' part
  is left out while the node's schedule is OFF. Select **Bed zone** in 'Review item', lie in bed, wait 20 s
  and press 'Mark spot here': the zone gets kind bed at once. With any other item selected the press makes
  a manual place. Later = [Later] (shown again in the next daily review); Reject = [No bed in view]. A press
  with the radars off, with no stay of 20 s or more, or with two people in bed creates nothing and fires
  `mark_failed`. One reading of the design is recorded here: a press that is not bound to the bed item keeps
  the P3 name rule, so a spot name that is exactly `bed` or `bett` (trimmed, any case) still gives kind bed,
  whichever item the select shows; every other name gives a manual place. Before the setup review there is no
  item at all, and that rule is how the bed is marked then; a user who typed 'Bed' says what the spot is.

The review buttons send only Approve, Reject and Later in P4. The core also knows `undo` (brings back a bed
item you answered with [No bed in view], or a denied night window) and `forget`, but they are wired to the
'Advanced' part of the review in P5 and cannot be sent from MQTT before that. If you pressed [No bed in view]
by mistake, type `Bed` as the spot name, lie in bed and press 'Mark spot here': that makes the bed zone too.
Zones made by 'Mark spot here' are labels (kind bed or manual); the status attribute `review.zones` counts only
the places you approved or renamed, so it stays 0 after a successful press (P6 reads both).

An answer for a stale revision of an item (the item changed after you saw it) is dropped and logged. Answers
can also be sent by hand: JSON `{"op": "approve" | "reject" | "later", "item"?: "night_window" |
"bed_needs_data", "rev"?: n, "sched"?: true}` to `radar_zones/review/answer`.

### 9.5 Night learning (optional, OFF by default)

'Mark spot here' in bed is the recommended way to get the bed zone; Night learning is not offered in the bed
item. When you switch it on, the learner sets the node's 'Radar night schedule' OFF for 7 nights so that the
radars observe the whole night, and switches it ON again afterwards. It costs 7 x 6 h of extra radar on-time
per radar (the 8-week mean off time falls from 5.89 to 5.14 h/day) and brought the learner's own bed
suggestion forward by 2-7 nights in simulation.

- A fresh install, and an install after the setup review, never sends a schedule OFF unless you switch it on.
- It writes only `<node_prefix>/radar/schedule`, `.../night_start` and `.../night_end`, never retained, and
  never `radar/power`: it cannot power the radars.
- **It does nothing on a node without the P2 firmware.** The P2 node publishes `radar/schedule/state`
  (retained) as soon as the app subscribes; until the app has seen it, or while the node is offline, every
  command is refused and nothing is published under `led_matrix_node/`.
- **It never overrides a change made by you.** The learner switches the schedule back ON only while the
  node still shows the value the learner wrote; if you changed it in the meantime nothing is sent.
- It checks the echo on `radar/schedule/state` for 2 minutes (the event `device_not_confirmed` otherwise). A
  missing echo does not prove that nothing happened: the OFF may still take effect (a broker restart, a slow
  node). So the run is cancelled, but the learner keeps the OFF as its own and watches the echo for 15 more
  minutes: an OFF that shows up late is switched ON again, and an echo that still reads ON ends it without a
  trace. The event is sent when the broker link is back if it was down at that moment.
- **Switching it back ON.** At the window end of the 7th night (or when you switch Night learning off) the
  learner sends ON only while the node still shows its own OFF. If the node is offline or has not shown its
  schedule yet, nothing is sent and it is no try: the learner waits, tries again every 10 minutes and at once
  when the node returns. A restore that was sent and not confirmed is repeated up to 12 times (2 hours). If it
  gives up, or the restore is more than 24 hours overdue (for example the app was down for days, and the OFF may be
  yours by now), nothing more is sent and the review event `restore_failed` fires (`reason` `no_confirmation` or
  `overdue`). After a restart of the app the stored schedule value is not trusted: the learner waits for the
  node's own echo first.
- **If Night learning is interrupted** (the app is stopped or uninstalled while it runs, or it is down for more
  than a day around the end of the 7th night, or you see `restore_failed`), the node keeps the schedule OFF in its
  own memory and the radars run all night every night. Switch 'Radar night schedule' ON yourself then. A restart
  of the app is no interruption: it resumes where it was.

### 9.6 Context blueprint

`blueprints/radar_zone_context.yaml` is an automation blueprint, not an entity. Copy it to
`/config/blueprints/automation/radar_zone_learner/radar_zone_context.yaml`, then Settings > Automations &
scenes > Blueprints > Radar zone learner - home context > Create automation. It publishes retained
`on`/`off` messages:

| Input | Topic | Meaning |
|---|---|---|
| Home is empty | `radar_zones/context/home_empty` | every chosen entity is on, not_home or away: clutter (ignore-zone) learning |
| Guests are present | `radar_zones/context/guests` | any chosen entity is on: learning is paused for guests |
| Lights are off (optional) | `radar_zones/context/lights_off` | every chosen entity is off: night-onset evidence |
| Household is asleep (optional) | `radar_zones/context/sleep_hint` | any chosen entity is on: night-onset evidence |

A context with no entity chosen is never published. An unavailable or unknown entity publishes an empty
(cleared) message. In this installation the node's illuminance sensor is the expected night-onset input;
leave the two optional inputs empty unless something real feeds them. The blueprint needs Home Assistant
2024.10 or newer and has not been run in Home Assistant yet: check the first run of the automation in its
trace.

### 9.7 New topics

The app now subscribes to 18 topics. New inputs: `<node_prefix>/sensors/state` (the illuminance),
`<node_prefix>/radar/state`, `<node_prefix>/radar/observing`,
`radar_zones/context/{lights_off,sleep_hint,guests}`, `radar_zones/learner/night_learning/set`,
`radar_zones/review/select/set` and `radar_zones/review/answer`. A retained message on one of the three
command topics is ignored and deleted, as for 'Mark spot here'. New outputs (retained, QoS 1):
`radar_zones/radar/valid`, `radar_zones/scan/rf_on_24h`, `radar_zones/scan/rf_saving_7d`,
`radar_zones/learner/coverage_7d`, `radar_zones/learner/night_learning` and
`radar_zones/review/{select,pending,pending/attr,item_text}`; not retained: `radar_zones/review/event`.
Commands to the node (not retained; Night learning and an approved night window only): the three topics of
section 9.5.

## Development

Everything except the MQTT client runs with the Python standard library (3.12 or newer). From this
folder:

```
python -I -B -m unittest discover -s tests -t .
python -I -B -m unittest discover -s tests -t . -p "test_service.py"
```

The tests need no broker and no paho-mqtt: `tests/fakes.py` provides a fake client. Set `RZL_TEST_TMP` to
choose the folder for temporary files. The replay harness in `replay/` runs the learner on simulated days
(see `replay/run_replay.py --help`). The P3 gate (G1-G6) runs with `--profile p3` (about 1,333 simulated days, about
22 minutes on 3 workers). The P4 checks (P4-1 to P4-7) run with `--profile p4smoke` (one 9-night run) or
`--profile p4` (47 runs, 707 simulated days, about 12 minutes on 3 workers; the bedroom only, and no run of the
G1-G6 jobs: the report of a p4 invocation says that G1-G6 were not run, so its PASS is about P4-1 to P4-7 only; its job
table lists the jobs, seeds and night counts: 21 nights x 5 seeds for the base job, 14 x 5 for the other windows and
Night learning, 16 x 3 for the mark, approve and reject scenarios, 14 x 2 for the names, 3 x 1 for the node without
P2). The two profiles together exceed the 1,500-day limit of one invocation, so they are two invocations. Single jobs
such as `--jobs bed_w0000_0600_p4,bed_w0000_0600_p4nl --seeds 1-3 --nights 21` also work. The report adds a P4 section
next to the P3 criteria; a check reads INCOMPLETE until all 5 seeds of its jobs have run.
