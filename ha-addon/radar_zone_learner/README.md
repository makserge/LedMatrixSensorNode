# Radar Zone Learner (Home Assistant app)

A local Home Assistant app ("add-on" in older Home Assistant versions) that learns the places people use
in a room from the LED matrix node's LD2454 radar frames. Version 0.7.0 (phase P7, on top of P3, P4, P5 and P6) of the
zone-learning design. The P4 additions (night window, bed item, optional Night learning, RF and
coverage sensors, the context blueprint) are described in section 9; the P5 additions (the review of the suggestions on
a device in Home Assistant, notifications, the privacy controls) in section 10; the P6 additions (occupancy, count and
events of the zones you confirmed, tuning, and the spot-light blueprint) in section 11; the P7 additions (a place that
moved, a place nobody uses any more, a radar that was moved or turned, and the tuning data you can export) in section 12.
The app itself still switches nothing in your home.

## 1. What it is

The app subscribes to the node's frames stream, tracks the radar targets, finds the places where people
stay (desk, table, sofa, bed, ...), and every night at 03:30 it works out which of them are reliable enough
to suggest. The suggestions are written down in the app's state, in the attributes of the 'Learner status' sensor and
on the debug map; since P5 you can also approve, rename or reject them on the review device (section 10).

What it does not do:

- no room occupancy entity, no presence entity and no automations of its own: a light or a scene is switched only by
  the optional blueprint of section 11.6, and the only notification is the optional review push of section 10.4;
- it has no Home Assistant API access (the manifest asks for none), so it cannot call a service itself;
- the only things it writes to the node are the three schedule topics of the night window (section 9.4: an approved
  night window; section 9.5: Night learning); it never touches `radar/power`.

Approved places become zone **records** in the app's state (a name, a kind and an outline); since P6 every zone record and
every 'Mark spot here' label of the kinds dwell, short_task, bed and manual is also published as a device with an
occupancy sensor, an event entity and tuning entities (section 11).

Entities (MQTT discovery):

| Entity | Device | What it shows |
|---|---|---|
| `sensor.radar_zones_learner_status` ('Learner status') | Radar zone learner | `starting`, `learning`, `not_observing` or `no_data`, with the learner's details as attributes |
| `image.radar_zones_learner_debug_map` ('Learner debug map') | Radar zone learner | the learned map (section 4) |
| `button.radar_zones_review_mark` ('Mark spot here') | Radar zone review | marks the spot where you are as a label (section 5) |
| `text.radar_zones_review_name` ('Spot name') | Radar zone review | the name the next mark gets; "Bed" or "Bett" makes it a bed label |

P4 adds the entities of section 9.1, P5 those of sections 10.2 and 10.6, P6 those of section 11.1 (one device for each zone),
P7 two on the learner device (section 12.6: a button and a diagnostic sensor for the tuning data; no per-zone entity).

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
5. The log shows `connected to core-mosquitto:1883`, `subscribed (34 topics)` (30 before P6, 18 before P5, 9 before P4) and, once
   frames arrive, `status learning`. Every later change of the learner status is logged the same way.

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
| `debug_map` | `true` | publish the debug map image; `false` removes the debug map and, since P5, the review map and the push map (the live view and 'You are in' still work as text) |
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
| lime outline | a confirmed zone (P5: a place you approved or renamed; a bed zone is pink) |
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
- The status attributes contain place centres, kinds and counts, never stay times (P5 adds only counts: `zones_total`,
  `pending_places`, `learning_paused` and the on/off state of the four settings; P6 adds only counts too: `zone_entities`,
  `zone_cmds_ignored` and the zone event counters, never a name, an id or a coordinate). The debug map is a place map
  without times.
- **Since P6 per-zone topics exist** (section 11.5): occupancy, count, events and six attributes of every confirmed zone.
  Zone names appear only in the entity and device names of the discovery configs (not retained), never in a topic or an
  event payload. The attributes include the hours a zone is usually used and its outline. Occupancy, events and hours are
  a location and routine log once Home Assistant's recorder keeps them: see the excludes below.
- **Since P7 `/data/state.json` also holds** (in every backup) a per-zone **usual-hours profile** (24 shares of the zone's
  usual use by hour of day, the same class of data as the stay list and coarser), the dates a zone was last used and the
  dates of a 'Keep' answer, the position of a place that moved or was removed (the *moved-from* mask, kept so the old spot is
  not offered as a new place), the radar-moved state and counters of your answers (section 12). None of it is published; the
  review map draws only the open item and the zones.
- **The tuning data (P7, section 12.6)**: `/data/tuning/nights.jsonl` holds one line of counts and fixed-length histograms for
  each of the last 28 nights. It has no date, no position, no name, no id and no hour of day, but as a day-by-day series it
  would show a night with nobody home, so it is **local only**: `config.yaml` excludes `tuning/*` from backups, nothing
  publishes it, and 'Forget all learned data' deletes the folder. Only when you press 'Export tuning data' does the app
  publish **one pooled record** (sums over the kept nights, no per-night list) once, on a topic that is not retained.

The radar sees everyone in the room. Tell the other people in the household what the radar and this app
record.

Recommended recorder excludes, if you do not want this history in Home Assistant's database (keep them
recorded during the 24 h resource check of section 7):

```yaml
recorder:
  exclude:
    entities:
      - sensor.radar_zones_learner_status
      - sensor.radar_zones_review_you_are_in
      - sensor.radar_zones_review_item_text
      - sensor.radar_zones_review_pending
      - sensor.radar_zones_review_push
      - event.radar_zones_review_event
    entity_globs:
      - image.radar_*
```

P5 adds entities whose history would say more than you want recorded: the three images (every published frame of the
live view is a new state, up to 150 per session), 'You are in' (a live position while 'Show me live' runs), the review
texts (which can carry a place name and, with 'Detailed review text', hours of use) and the review events. The README
sections 10.2 to 10.6 list every entity.

P6 adds, for every zone, an occupancy sensor whose history is when someone was at that place, its event entity (the same
history with the time in the second) and the other zone entities of section 11.1. Together with 'Radar state' of the
node they form a sleep and occupancy history that every Home Assistant user can read. Exclude what you do not want
recorded, for example the zone events and the node's radar state (the occupancy sensors are the data you asked for; keep or
exclude them as you like):

```yaml
recorder:
  exclude:
    entity_globs:
      - event.radar_spot_*
      - sensor.led_matrix_node_radar_state
```

The zone count sensors (disabled by default; if you enable them each change is a row) and the tuning numbers
(`number.radar_spot_*`) can be excluded the same way. The default `purge_keep_days` of 10 days applies to all of it;
shorten it if that is longer than you want.

P7 adds the diagnostic sensor 'Tuning data' (`sensor.radar_zones_learner_tuning`: `none` or `<n> nights`, nothing else) and
review items whose texts say that the radar was moved. Excluding them is optional advice, not a need: add
`sensor.radar_zones_learner_tuning` to the `entities` list above if you want no row of it in the database. A P7 text carries no
zone name, hour or date unless 'Detailed review text' is on.

**Deleting everything**: since P5 the learner device has 'Forget all learned data' (section 10.6): it deletes the state,
its snapshots, the raw-frame capture and (since P7) the tuning folder and starts a fresh learner, and it overwrites the retained topics below with their
empty versions. To remove the app itself, stop it and uninstall it; this removes `/data`. Backups made before keep their
copy. Uninstalling does not remove what the app left outside `/data`:

- the retained MQTT messages (QoS 1) `radar_zones/learner/{status,state,attr,coverage_7d,night_learning,guests_pause,pets,
  detail_text,debug_capture,forget_confirm}`, `radar_zones/radar/valid`, `radar_zones/scan/{rf_on_24h,rf_saving_7d}`,
  `radar_zones/review/{name,select,pending,pending/attr,item_text,advanced,live,nw_start,nw_end,approve_sched,push}` and, since
  P6, `radar_zones/zone/availability`, `radar_zones/zone/<id>/{clear_delay,margin}` and
  `radar_zones/zone/<id>/<variant>/entity` and, since P7, `radar_zones/learner/tuning` (`none` or `<n> nights`) stay in the
  Mosquitto broker's own storage (and in its backups). Not retained, so
  nothing to clear: the discovery configs, `radar_zones/learner/debug_map`, `radar_zones/learner/tuning/export` (P7),
  `radar_zones/review/{map,push_map,event,you_are_in,you_are_in/attr}`, the commands to the node and, since P6,
  `radar_zones/zone/<id>/{occupancy,count,event,attributes}` and `radar_zones/zone/<id>/<variant>/occupancy`. A zone that is
  removed (or Forget all) has its own retained topics cleared by the app. Clear a retained one with an empty retained
  message, for example
  `mosquitto_pub -r -n -t radar_zones/learner/attr`, or stop the app first and publish from Developer tools → Actions →
  `mqtt.publish` with an empty payload and `retain: true`. 'Forget all learned data' already overwrites the ones that
  carry learned content (`attr`, `pending`, `pending/attr`, `item_text`, `name`, `select`, `push`);
- the devices 'Radar zone learner' and 'Radar zone review', since P6 one device for each zone, and their entities stay in
  Home Assistant's entity registry until you delete them (Settings → Devices & services → MQTT → Devices; 'Forget all
  learned data' removes the zone entities from Home Assistant, but the registry keeps their names and history);
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
| 'Review item' shows an old list of places, or `unknown` | Home Assistant did not apply the changed option list (section 10.2): reload the MQTT integration or restart Home Assistant once, and if it repeats set `SELECT_MODE = "slots"` in `shell/select_model.py`. |
| An answer does nothing | Read 'Review event': `answer_ignored` names the reason (`stale`: the item changed after you looked, press again; `no_such_item`: it was answered already). |
| No notification arrives | The learner makes at most one request a day, between 08:00 and 21:00 local time, and only for something new. Check 'Review push request' (its second field below its first means no phone acknowledged yet), the trace of the automation and that the automation is for this phone. The request is issued again the next day when nobody acknowledged it. |
| 'Forget all learned data' does nothing | The confirmation text must be `FORGET` and the button pressed within 60 s; read the event `forget_refused` (`not_confirmed`, `night_learning_active` - Night learning runs or has not yet put the node's night schedule back ON, which can take days if the node is offline - or `command_pending`). |

## 9. P4: night window, bed item, optional Night learning, RF sensors

There is still no room occupancy and no presence entity; the zone entities came with P6 (section 11). The
entities below are added by the P4 shell layer; the proposals and the Night learning rules come from the learner
core. An entity whose source is missing shows `No item` (review; `None` before P5) or `OFF` (Night
learning) and does nothing.

### 9.1 New entities

| Entity | Device | What it shows or does |
|---|---|---|
| `binary_sensor.radar_zones_radar_valid` ('Radar data valid') | Radar zone learner | `on` only while the learner observes (state `learning`), the node is online and its 'Radar observing' is not OFF. Topic `radar_zones/radar/valid` (retained `online`/`offline`, the vocabulary of an availability topic). The P6 zone entities do **not** use it: it does not know whether the learner's zones have resumed, so they have their own availability topic (section 11.3). |
| `sensor.radar_zones_scan_rf_on_24h` ('RF on time 24 h') | Radar zone learner | Hours with the radars on in the last 24 h |
| `sensor.radar_zones_scan_rf_saving_7d` ('RF saving 7 d') | Radar zone learner | Share of the last 7 days with the radars off, % |
| `sensor.radar_zones_learner_coverage_7d` ('Coverage 7 d') | Radar zone learner | Share of the last 7 days (168 h) in which the learner observed, % |
| `switch.radar_zones_learner_night_learning` ('Night learning') | Radar zone learner | OFF by default (section 9.5) |
| `select.radar_zones_review_select` ('Review item') | Radar zone review | `No item`, `Night window`, `Bed zone` and since P5 one option per open place (section 10.2): the item the buttons act on |
| `sensor.radar_zones_review_pending` ('Review pending') | Radar zone review | Number of open items; the attributes list them with their text |
| `sensor.radar_zones_review_item_text` ('Review item text') | Radar zone review | The text of the selected item |
| `button.radar_zones_review_approve`, `..._reject`, `..._later` | Radar zone review | Answers for the selected item |
| `event.radar_zones_review_event` ('Review event') | Radar zone review | `mark_failed` (with `reason`: `not_observing`, `no_stay`, `several_stays` or `too_soon`), `device_not_confirmed` (with `kind`) and `restore_failed` (with `reason`: `no_confirmation` or `overdue`; section 9.5). An event that cannot go out because the broker link is down is kept (at most 20) and sent when the link is back. |

The status attributes gain `node_schedule_seen`, `node_cmds_sent`, `node_cmds_unconfirmed`,
`node_cmds_refused` and `review_items` from the shell, next to the core's `night`, `review` and
`night_learning`.

### 9.2 'Radar data valid'

The sensor is `off` while the radars are off (night window, manual off), warming up or faulty, when no frames
came for 15 s, and when the node is offline (its last will). The zone entities of section 11 are unavailable at the
same times, and also until the learner has resumed after a power-up or a restart (section 11.3; there is no room
occupancy entity). Write state triggers on them as `to: 'off'` with `not_from: ['unavailable', 'unknown']`, otherwise
the morning power-up fires them:

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

The core proposes two items first (tier 1); the review device shows them, and since P5 the places after them
(section 10):

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

`undo` (brings back a bed item you answered with [No bed in view], or a denied night window) and `forget` are
wired since P5: the 'Advanced item' select with 'Apply advanced', and the buttons 'Re-suggest night window' and
'Forget rejections' (section 10.3). If you pressed [No bed in view] by mistake, pick 'Undo: No bed in view' there.
Zones made by 'Mark spot here' are labels (kind bed or manual); the status attribute `review.zones` counts only
the places you approved or renamed, so it stays 0 after a successful press (P6 evaluates both, section 11; `Learner.zones()` lists
both).

An answer for a stale revision of an item (the item changed after you saw it) is dropped; since P5 it is also shown as
the review event `answer_ignored` (section 10.3). Answers can also be sent by hand, see section 10.8.

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
| Guests are present | `radar_zones/context/guests` | any chosen entity is on: learning is paused for guests (a real pause since P5, section 10.6) |
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
section 9.5. P5 adds 12 more input topics (30 in all) and the outputs of section 10.7; P6 adds 4 command patterns (34 in all, section 11.7).

## 10. P5: review device, push notifications, privacy controls

P5 turns the learner's suggestions into something you can answer: the review device shows what is
waiting (a night window, the bed, the places) with a map, you approve, rename or reject, and the learner remembers
what you said. An approved place becomes a **zone record** in the app's state (a name, a kind and an outline). Since P6
every zone record is also published as entities (section 11); the learner publishes a zone name only in the entity and
device names of those zone entities and in the review texts that you asked for (the Advanced labels, the merge and modify
texts, 'You are in' while the live view runs).

### 10.1 The first setup, in taps

The review starts at night 7 of the learner (a review list is empty before). In the bedroom with the night window
00:00-06:00 and the schedule ON, do it in the evening, **in bed with the phone** (the radars observe until 00:00):

| # | Tap | What happens |
|---|---|---|
| 1 | Approve, with 'Review item' on `Night window` | the node's night window is written (and the schedule switched ON only if 'Approve also switches schedule ON' is on); the item leaves and the select falls to the next item |
| 2 | 'Mark spot here', with `Bed zone` selected | after 20 s in bed it makes the bed zone (kind bed, frozen while the schedule is ON); the bed item leaves |
| 3-7 | Approve, once per place | each place becomes a zone record named `Place N` (or `Bed` for the learner's own bed place); to name it, type the name into 'Spot name' first and press Rename instead of Approve |

That is 7 taps for 5 places (6 for 4; 8 if you first have to pick the bed item). If you are not in bed, [Later] on the bed
item costs one tap and it comes back after the next nightly run. A text entry (the name) is not a tap.

### 10.2 The review device

| Entity | What it shows or does |
|---|---|
| `select.radar_zones_review_select` ('Review item') | `No item`, `Night window`, `Bed zone`, `Radar moved` (since P7, only while that item is open), then `Place A`, `Place B`, ... (a place), `Extend A` (a place that touches a zone you have and is used at similar hours), `Moved A` (a zone whose place seems to have moved) and, since P7, `Retire A` (a zone nobody has used for weeks). The letters are persistent tags: a tag never changes its place while the item exists, and a name never appears in the select. At most 15 options. |
| `button.radar_zones_review_approve`, `..._rename`, `..._reject`, `..._later` | the answer for the selected item (the app fills in the item and its revision) |
| `text.radar_zones_review_name` ('Spot name') and `button.radar_zones_review_mark` ('Mark spot here') | as before (section 5); the name is also what Approve and Rename use: **an applied answer consumes the text** (it is cleared), so the next place does not get the same name |
| `sensor.radar_zones_review_pending` ('Review pending') | the number of open items (tier 1 and tier 2); the attributes list them with `id`, `rev`, `type`, `tier`, `label` and `text`, plus `selected` and `more` (open places that did not fit under the cap of 12) |
| `sensor.radar_zones_review_item_text` ('Review item text') | the text of the selected item, `No item` when nothing is selected |
| `image.radar_zones_review_map` ('Review map') | the map of the selected item (section 10.5) |
| `image.radar_zones_review_push_map` ('Review push map', diagnostic) | the map of the item a notification is about (fixed per request; geometry only, no dwell heat) |
| `switch.radar_zones_review_show_live` ('Show me live') and `sensor.radar_zones_review_you_are_in` ('You are in') | section 10.5 |
| `time.radar_zones_review_nw_start`, `time.radar_zones_review_nw_end` ('Night window start/end (edit)') | the times Approve writes for the night-window item. They show the proposal, or your edit, or (with no proposal) the node's current window. An edit counts only for the proposal it was made on; a window outside 1 h to 16 h is refused. |
| `switch.radar_zones_review_approve_sched` ('Approve also switches schedule ON') | OFF by default; when ON the next night-window approval also switches 'Radar night schedule' ON, and the box then switches itself OFF again |
| `select.radar_zones_review_advanced` ('Advanced item') and `button.radar_zones_review_apply_advanced` ('Apply advanced') | section 10.3 |
| `sensor.radar_zones_review_push` ('Review push request', diagnostic) | what the push blueprint reads (section 10.4) |
| `event.radar_zones_review_event` ('Review event') | `mark_failed`, `device_not_confirmed`, `restore_failed` (section 9), and since P5 `answer_ignored` (with `reason`, `op`, `item`), `forget_refused` (with `reason`) and `forgotten` |

When the list of options changes (a new place, an answered one) the app publishes the select's discovery config again and
sends the state twice, 2 s apart, because Home Assistant applies a changed option list in a task and drops a state for an
option it does not know yet. If a real installation still shows a stale list, set `SELECT_MODE = "slots"` in
`radar_zone_learner/shell/select_model.py`: the options are then fixed (`Place 1` .. `Place 12`, slot k being the k-th place
in the list) and the app republishes the state at every change.

### 10.3 Answering, Advanced, and what an answer does

| Answer | Night window | Bed zone | Place | Extend (merge) | Moved (modify) |
|---|---|---|---|---|---|
| Approve | writes the node's window (the two times of the time entities) | use 'Mark spot here' | a zone record, kind from the name (`bed`/`bett` makes a bed) | the zone grows by the place | the zone takes the new place |
| Rename | - | - | the same with the name from 'Spot name'; no name: `name_needed` | 'no, it's a new place': a new zone | the old zone stays, a new zone is made |
| Reject | Deny: not asked again for 30 days | [No bed in view] | rejected: a region that overlaps it by half, or has its centre inside, is not suggested again until its evidence has doubled and 30 nights have passed | the place is rejected as a place | the question is not asked again for this change |
| Later | comes back after the next nightly run | the same | the same | the same | the same |

Merge and modify items are made by the learner since P7 (their detectors, the new `Retire` and `Radar moved` items and what
every answer does for them are in section 12; the P5 scaffolding behind `P5Params.zone_change_items` stays in the code and
is used only by a learner that has the P7 detectors switched off, which the app never is).

A place whose zone exists, or that a 'Mark spot here' mark covers, is not asked about again. The review event
`answer_ignored` says why an answer changed nothing: `no_such_item`, `stale`, `not_pending`, `not_applicable`,
`name_needed`, `zone_limit`, `times_invalid`, `no_item` and, since P7, `zone_busy` (section 12.1).

**Advanced** is a separate list that is never counted, never pushed and never auto-selected. It holds what you answered
and the reflections: 'Undo: No bed in view', 'Undo: Night window denied until <date>', 'Undo: Place A rejected', 'Real
place: Reflection A' (permanent: the next nightly run may suggest that region as a place), and for each zone and each
'Mark spot here' mark 'Rename zone/mark: <name>' and 'Remove zone/mark: <name>'. Pick one in 'Advanced item' and press
'Apply advanced'. Removing a zone brings its place back as an open item; the learner's silent findings (the outline, doors,
ignore zones, clutter) have no entry because there is nothing to revert in P5.

The buttons 'Forget rejections' (every rejection and every [Later] is cleared; zones, marks, permanent answers and settings
stay) and 'Re-suggest night window' are on the learner device.

### 10.4 Push notifications

`blueprints/radar_zone_review_push.yaml` is an automation blueprint, not an entity. Copy it to
`/config/blueprints/automation/radar_zone_learner/radar_zone_review_push.yaml`, then Settings > Automations & scenes >
Blueprints > Radar zone learner - review notifications > Create automation, and **create one automation per phone** (each
phone acknowledges separately, so one phone's acknowledgement never hides the request from another). Inputs: the phone
(a Companion app device), the push sensor and the map image (the defaults are right), the page a tap on the notification
opens, and 'Show the item text' (off).

- **When**: the learner decides, not Home Assistant. One notification once the setup review is ready (night 7 or later,
  at least 2 items waiting), then at most one a day, only for an item or a revision that was never announced, and only
  between 08:00 and 21:00 local time. A [Later] item that comes back is listed again but is not announced a second time.
  A request that no phone acknowledged is issued again the next day, inside the same window. That covers a notify action
  that raises (the phone's notify service does not exist) and a blueprint that was installed after the request was made.
  The acknowledgement means that Home Assistant **accepted the notify call**, not that the phone received it: a push
  that the Companion app's remote service fails to deliver is only logged by Home Assistant and is not retried. The retry
  has no upper limit: while no automation acknowledges and an item stays open, one request a day is made (one retained
  state), and a notify action that always raises leaves one error in the log and one failed trace a day until you fix it.
- **What it says**: 'Radar setup review is ready (3 items)' or 'Radar zone review: 3 item(s) to look at', the title
  'Radar zone review', the map of the first place as image (geometry only: the room, the outlines and that place highlighted, **no dwell heat**, because the phone fetches the image from Home Assistant and it may leave your network), and the actions Approve, Rename (with a text prompt) and
  Reject for that place. Nothing else: no time and no name. The night-window and bed items are never answered from a push
  (Approve on the window writes the radar schedule, and the bed needs a physical press). A tap on an answer goes back to the
  app with the item, its revision and the notification's id; a repeated tap is applied once; a tap on a place that was
  answered meanwhile gives the event `answer_ignored`.
- **Opt-in**: the item text (typical hours of use, a place name in a merge text) is in the notification only when
  **both** the app's switch 'Detailed review text' and the blueprint's input are on. With the app's switch off, no hour
  range exists in any published payload.
- **Restarts**: the request is a retained sensor state with eight fields (`seq|acked|kind|count|rev|item|acks|text`); the
  blueprint acknowledges with `<seq>:<phone>` after the notify call returned. A restart of Home Assistant or of the app neither loses a
  request nor repeats one that was acknowledged; the notification has a fixed tag, so a repeat replaces the first copy.
- Since P7 a request can also be **'Radar needs attention'** (the kind `attention`): the learner believes the radar was moved or
  turned (section 12.4). It is generic like the others (no name, no time, no distance), has no actions (open the review device
  and answer the `Radar moved` item there), and follows the same rules (one a day at most, 08:00 to 21:00, issued again the
  next day if no phone acknowledged). A `Retire` item is never the item a push offers and is never answered from a push: a
  removal needs a look at the map first. **Copy `radar_zone_review_push.yaml` again** after you update to 0.7.0: an older copy
  does not know the kind `attention` and ignores that request.

The blueprint has never been loaded by Home Assistant: check the first trace (the request, a tap, the clear), see 10.9.

### 10.5 'Show me live', 'You are in' and the maps

'Show me live' (switch) makes the review map follow the radar: a frame every 2 s with a red 7 x 7 px marker for each
confirmed person (orange when the person has stayed still), and 'You are in' names the place at your position (`Place A`,
a zone's name, `Marked spot`, `In no known place`, `No one in view`, `Several people in view`, `Radar not observing`).
It switches itself off after 5 minutes (a second ON extends it to 5 minutes from then), says `Live view off` otherwise, and
publishes no coordinate and logs none. While the live view runs, 'Mark spot here' still needs exactly one stay of at
least 20 s: the attribute `stay_ready` of 'You are in' says when a press would work.

The review map is 280 x 160 px like the debug map and has the same radar, heat and walking envelope. Extra colours: yellow
outline plus a small square = the selected item; steel blue = the other open items; lime = a confirmed zone (pink for a
bed); red and orange squares = the live positions. It follows the option `debug_map`.

### 10.6 Privacy controls (learner device)

| Control | What it does |
|---|---|
| `button.radar_zones_learner_forget_all` ('Forget all learned data') and `text.radar_zones_learner_forget_confirm` | Deletes the state, its snapshots and quarantined copies, the zone records and marks, the rejections, **the sleep log (onsets and wakes)** and the raw-frame capture, and starts a fresh learner. It does nothing unless 'Forget all confirmation' was set to `FORGET` (any case) **within the last 60 s**; every press and the 60 s clear the text. Refused (the event `forget_refused`) without the text, while Night learning runs **or is still putting the node's night schedule back ON** (a reset would drop that bookkeeping and leave the schedule OFF), and while an approved night window waits for the node's echo. Kept: the node's window, the four settings below, the push counters, `rf_stats.json` (what the node's own 'Radar state' history shows) and `options.json`. |
| `switch.radar_zones_learner_guests_pause` ('Guests present - pause learning') | While ON, **or** while the guests context (section 9.6) is on, nothing from the paused time feeds the model: no coverage, no stays, no walking, no doors. Tracking, the status, 'Radar data valid' and 'Mark spot here' go on. It is judged at the time of the evidence, so a stay that is committed later is still dropped. The status attribute `learning_paused` shows it. |
| `switch.radar_zones_learner_pets` ('Pets in home') | OFF by default. With it ON a silent ignore-zone suggestion that was used mostly while the home was empty gets the kind `pet` (counted in `pet_spots`); nothing else changes. |
| `switch.radar_zones_learner_detail_text` ('Detailed review text') | OFF by default. ON adds 'Used mostly 09-12 h and 13-17 h.' to a place text; it is the app's half of the push opt-in. |
| `switch.radar_zones_learner_debug_capture` ('Debug capture') | OFF at every start, never resumed after a restart. ON writes the frames topic (raw positions) to `/data/capture/capture-<UTC date-hour>.jsonl`, one line per message, for `replay/capture_replay.py`; no other topic is recorded. It switches itself off after 48 h or 100 MiB, the files are deleted after 72 h, 'Forget all learned data' deletes them, and `config.yaml` excludes `capture/*` (relative to the app's data folder) from backups. The status attribute `capture` counts files and bytes. |
| `button.radar_zones_learner_forget_rejections`, `button.radar_zones_learner_resuggest_night_window` | section 10.3 |

All switches here persist in the state (the capture does not) and are published retained. 'Forget all learned data' cannot
reach Home Assistant's recorder history, the entity registry, backups that contain an earlier `state.json`, and the
broker's own persistence file (Mosquitto rewrites it on its own schedule). The household consent text of section 6 applies
as before.

### 10.7 New topics

The 12 new inputs (QoS 1, all commands: a retained message on one is ignored and deleted):
`radar_zones/review/{advanced,live,nw_start,nw_end,approve_sched}/set`, `radar_zones/review/push_ack`,
`radar_zones/learner/{guests_pause,pets,detail_text,debug_capture,forget_confirm}/set` and `radar_zones/learner/forget_all`.
Outputs: retained `radar_zones/review/{select,advanced,live,nw_start,nw_end,approve_sched,push,pending,pending/attr,
item_text}` and `radar_zones/learner/{guests_pause,pets,detail_text,debug_capture,forget_confirm}`; not retained
`radar_zones/review/{map,push_map,you_are_in,you_are_in/attr,event}`. The review `select` and `item_text` say `No item`
(not `None`) when nothing is selected. 35 discovery messages (and, since P6, 6 for each zone) are published at every connect and 0-10 s after Home
Assistant's birth message; the maps and the not retained states are sent again 10 s after each burst, because Home
Assistant creates its entities only after it has read the configs.

### 10.8 Answers by hand

A hand-written automation or a script sends JSON to `radar_zones/review/answer` (QoS 1, not retained, at most 512 bytes):

```json
{"op": "approve", "item": "s0005-1", "rev": 1, "sched": false, "name": "Desk", "aid": "01HV..."}
```

`op` is `approve`, `reject`, `later`, `rename`, `undo`, `forget` or `advanced` (an `rz_` prefix and any case are accepted).
`item` is an item id (`night_window`, `bed_needs_data`, a place `s0005-1`, `mrg-s0005-1`, `mod-z0001`) or an Advanced entry
id (`adv:undo:bed`, `adv:remove:z0001`, ...); without it the selected item is meant. `rev` is the revision you saw: a
different one is refused (the event `answer_ignored` / `stale`). `sched` (night window only) also switches the schedule ON.
`name` is 1 to 32 characters without control characters (a number is accepted and read as text). `aid` is an answer id
(`[A-Za-z0-9_.-]`, at most 64 characters): an answer with an `aid` that was seen in the last 64 answers is dropped silently
(a repeated delivery), and an answer with an `aid` never reads the 'Spot name' text (it would not know whether the text
was meant for it). The seven buttons send fixed messages without item or revision.

### 10.9 What to check once on the real installation

Nothing in P5 could be run against Home Assistant, a broker or a phone. Check, in this order: copy both blueprints and create
the automation from the review blueprint for one phone, then read the trace of the request and of a tap (A1); restart Home
Assistant with a request pending and confirm one notification within about 30 s, with its map image (the image is sent
again 10 s after the entity list, so an image entity that exists later than that gives a notification without the image,
and nothing else is lost) (A2); add a place while Home Assistant runs and watch the 'Review item' select **and the
'Advanced item' select**, also after two changes within a second and when Home Assistant is busy (if it needs several
seconds to apply a new option list, a select can show unknown until the next change: note how long) (A3, else
`SELECT_MODE = "slots"` for the review select); the notification text, three actions, the map as image and no time or
name on the phone you use, with the phone away from your network too: the phone itself loads the image from the
Home Assistant address it is configured with, so it needs a URL that reaches Home Assistant from outside (A4, A7); Rename with its text
prompt (A5); a tap twice and the same push twice give one zone and one visible notification (A6); 'Show me live' for 5
minutes with someone walking, and `cpu_pct_1h` of the status sensor afterwards (A8, A10); that the recorder excludes of
section 6 match all three images (A11); the time entities and the switch-on box (A12); 'Forget all learned data' on a copy of
the data (A13); a backup with `state.json` in it, and a backup of the app after a capture of one minute, opened to see that no `capture-*.jsonl` is in it (A14,
A15: the `capture/*` entry of `backup_exclude` is the thing under test); after the setup request, copying the blueprint late or naming a phone that is not registered: within a day, between
08:00 and 21:00, a new request and the notification (A16); two phones, one automation each, with a restart of Home Assistant
between the first phone's acknowledgement and the second phone's send: each phone ends with one notification (A17).

## 11. P6: zone occupancy and the spot-light blueprint

P6 turns the zones you confirmed in section 10 into things Home Assistant can use: is somebody at the desk, how many
people, when did they come and go. The learner measures; **it still switches nothing**. A light or a scene is switched by
the optional blueprint of section 11.6, which you install yourself (the app has no Home Assistant API access).

### 11.1 The entities of a zone

Every zone record and every 'Mark spot here' label of the kinds dwell, short_task, bed and manual is one device,
`radar_zone_<id>` (`<id>` is `z0001` for a record or `l0001` for a label; an unnamed label is called `Spot l0001`), a child
of the node's device. At most 24 zones get entities; the others are evaluated and can be tuned only after a slot frees.
Zones of the other kinds (entry, transit, ignore, reflection, pet) are silent model data and have none.

| Entity | What it shows or does |
|---|---|
| `binary_sensor.radar_spot_<id>` ('<zone> occupancy') | `on` while a person is in the zone or the zone waits for its clear delay, `off` otherwise; unavailable while the radars are off (section 11.3). Attributes (all six, always): `kind`, `state_source` (`live`, `failsafe`, `radar_off`, `warmup` or `fault`; the last three are published while the sensor is unavailable and Home Assistant may not show attributes of an unavailable entity, assumption HA[U4], acceptance A8), `variant_now` (the time variant in force, `""` when none), `typical_hours` (`wd 09-12,13-17; we 10-22` or `""`), `confidence` (the suggestion's, rounded to two decimals; `null` for a label or a manual zone) and `poly_cm` (the outline, at most 12 points) |
| `sensor.radar_spot_<id>_count` ('<zone> target count', diagnostic, **disabled by default**) | the number of people in the zone, 0 to 3; at most one change a second (the latest value follows); 0 while the zone is only held |
| `event.radar_spot_<id>_event` ('<zone> enter/leave') | `enter` (with `track`) and `leave` (with `track`, `dwell_s` and `via`: `edge`, `hold_expired`, `door_death` or `early`); `variant` when one is in force. Only real transitions: a radar power-up or a restart of the app fires none for a person who is already tracked within about 3 seconds (section 11.3); the zones turn available about 3.5 seconds after the radars resume, with the real state published first |
| `number.radar_spot_<id>_clear_delay` ('<zone> clear delay', config) | the delay in force, 3 to 900 s; typing a value overrides the learned one |
| `number.radar_spot_<id>_margin` ('<zone> margin', config) | the margin in force, -50 to 100 cm (default 25); a negative value shrinks the zone |
| `button.radar_spot_<id>_forget` ('<zone> forget learned data', config) | discards the learned delay, the radar agreement evidence and your overrides of **this** zone, and also its stored time variants, typical hours, confidence and variant requests until the next nightly run (03:30) refills them; the zone and its entities stay |
| `switch.radar_spot_<id>_vreq_<variant>` ('<zone> - <variant> sensor', config, OFF) and `binary_sensor.radar_spot_<id>_v_<variant>` | only for a zone that has time variants (section 11.4) |

Entity ids follow the unique ids; after a 'Forget all learned data' they carry the installation epoch
(`radar_spot_e1_z0001`) so that a new zone that reuses an old id does not inherit the old entity's name, area or history.
The zone name is part of every entity name and of the device name; if Home Assistant shows the name twice, rename the
entity in Home Assistant, the entity id is fixed.

### 11.2 How a zone is evaluated (and what the numbers mean)

- **Entering**: a confirmed track enters a zone when at least 3 of its last 10 detections lie inside the zone area, the
  stored cells grown by the margin. In a dwell, bed or manual zone a detection of a track that moves at 0.8 m/s or more is
  not counted (somebody walking past a desk does not occupy it; sitting down does). In a short_task zone every inside
  detection counts.
- **Leaving**: one detection outside the zone area is a leave at once. If the last person *vanishes inside* (the radar
  loses a person who sits very still), the zone is **held** until `last detection + clear delay`, then it clears (`via`
  `hold_expired`). A leaving person never clears a hold that someone else caused. A hold is judged on the radar's own
  batches, never on a clock guess (a lost batch must not clear a zone), so in an idle room the zone clears with the first
  batch after the deadline, at most about 5 s late (the node's heartbeat); the event's own time is the computed deadline.
- **Clear delay**: your override (3-900 s), else the learned one, else the default by kind: bed 600 s, dwell and manual
  300 s, short_task 60 s. The learned delay is the 95th percentile of the zone's own hidden spells (a person who vanished
  and came back), from 10 spells, 5 to 600 s, refreshed at the nightly run (03:30), so a zone behaves the same all day.
  A spell longer than twice the delay is not counted, so the learned value can grow by at most about 2 times a night.
- **Door fast-clear**: a person who walks out through a door the learner has seen (a silent `entry_zone` suggestion) and
  vanishes inside the zone area next to it clears the zone after 3 s instead of the delay (`via` `door_death`). Without a
  door suggestion there is no fast clear.
- **Early clear**: only for a zone where the second radar (LD2412) agreed at least 80 % of the time over at least an hour
  of known time. A held zone then clears after 60 s of LD2412 absence with no person seen anywhere (`via` `early`). It only
  ever shortens a hold; the second radar saying 'present' never extends one.
- **Latency.** Entering needs the third inside detection, about 0.3 s after you cross the zone edge on the radar's own
  clock; the node then sends detections in batches of up to 0.5 s and adds one 40 ms pass, so Home Assistant hears it up to
  about 0.84 s after the crossing (the UART read and the network come on top, not measured). A leave is one detection
  outside, about 0.1 s after the crossing on the radar's clock, and up to about 0.64 s on arrival, because the 500 ms batch
  alone uses the 0.5 s: a faster leave needs a firmware change. Step across a zone edge 50 times to see your real figures
  (section 11.8).
- **Not evaluated**: a pet or a reflection in a zone is counted like a person (the radar cannot tell); only the early clear
  uses the second radar. The pause 'Guests present - pause learning' stops learning, not the zones.

### 11.3 Unavailable while the radars are off (and the order at a power-up)

You decided that the zones show `unavailable` while the radars are off, not a held state. Every zone state entity (occupancy,
count, event, variant sensors) has three availability topics that must all say `online`: the learner
(`radar_zones/learner/status`), the node (`<node_prefix>/sensors/status`) and a topic of the app,
**`radar_zones/zone/availability`**, which is `online` only while the radars are on and observed **and** the learner's zones
have resumed. 'Radar data valid' (section 9.2) is not used: right after an add-on restart it is true while the zones have not
resumed yet. The tuning entities, the Forget button and the request switches only need the learner, so they work while the
radars are off, and a tuning command is applied then.

At the morning power-up (and after an add-on restart) the app first publishes the real state of every zone, then `online`.
The history of an occupied zone is therefore `on` (evening), `unavailable` (00:00), `on` (06:00, somebody is there, **no**
`enter` event) or `off`, never `unavailable -> off -> on` and never the evening's old `on`. This relies on Home Assistant
keeping a payload that arrives while an entity is unavailable and showing it when the entity becomes available (acceptance
step A2). A person who sits perfectly still and is first seen more than 3 s after the radars come back would fire an `enter`
at 06:00.

Write state triggers on a zone as `to: 'off'` with `not_from: ['unavailable', 'unknown']` (the example in section 9.2),
otherwise the morning power-up fires them.

### 11.4 Tuning, Forget and time variants

- Type a **clear delay** or a **margin** into its number entity; the number then shows the value in force and the value is
  saved with the state (`zone6.tune`). 'Forget learned data' removes your override and the learned numbers of that zone only
  (the button 'Remove zone' of the Advanced list is something else: it removes the zone).
- A zone that has **time variants** (a mode that held at least 10 stays on at least 4 days, found by the nightly run) shows
  the one in force as `variant_now` (named by its hours, `wd0917` = weekdays 09 to 17) and keeps `typical_hours`. A variant
  gets an occupancy sensor only when you turn its request switch ON; OFF removes the sensor. A variant sensor is `on` while
  the zone is occupied and that variant is in force. Changing the delay per variant is not built.

### 11.5 Privacy and the recorder

What P6 publishes: not retained, per zone, `occupancy`, `count`, `event` and `attributes`; retained, per zone, the two tuning
states (`clear_delay`, `margin`) and, for a zone with variants, the request state of each variant; retained,
`radar_zones/zone/availability`. **Zone names appear only in the entity and device names of the discovery configs** (not
retained, so a restart of Home Assistant leaves no ghost entity), never in a topic or an event payload; an event carries a
track number, the dwell time in seconds and the variant name. The six attributes include the hours a zone is usually used and
its outline: they are shown to every Home Assistant user. With the recorder these form a sleep and occupancy history (section
6 has the excludes and the 10-day purge). The recorder budget of the design (under 500 rows per zone per day) holds up to
about 120 episodes a day for the default set (occupancy, event, attributes, availability) and about 70 with everything
recorded (count and six variant sensors too); a zone with more episodes is a ghost or flapping (the learner's status
attribute `zones_over_budget` counts them), so look at it. An optional valve exists in the code but is off and has no option in 0.6.0 (35 episodes a day is the example of the
design); it would hold back the event, count and attribute publishes of a zone and never delays occupancy or availability.

### 11.6 The spot-light blueprint

`blueprints/radar_zone_spot_light.yaml` is an automation blueprint ('Radar spot occupied -> light/scene'), not an entity and
not part of the app. Copy it to `/config/blueprints/automation/radar_zone_learner/radar_zone_spot_light.yaml`, then Settings >
Automations & scenes > Blueprints > Radar zone learner - spot occupied -> light/scene > Create automation, **one automation per
spot**. Inputs: the spot (an occupancy sensor of a zone or of a variant), a light, a scene for occupied, a scene for clear
(any of them), the delay before switching off (60 s, 0 = at once), an optional brightness, and 'Also switch off when the zone
returns unavailable -> off' (off).

- **On** when the zone becomes `on`, also straight from `unavailable` (somebody at the spot at 06:00 switches the light on).
- **Off** when it has been `off` for the delay, **not** from `unavailable` or `unknown`: the morning power-up does not switch
  anything. While the zone is unavailable (radars off, app down) the light is left as it is.
- **The stuck light**: if the blueprint switched the light on and the person left while the radars were off, the zone returns
  as `unavailable -> off` at 06:00, which is ignored on purpose, so the light stays on until someone switches it. The extra
  input switches it off on that transition too, but it also fires at every morning power-up with nobody there; it is off by
  default.

The blueprint needs Home Assistant 2024.10 or newer and has never been loaded by Home Assistant: check the trace of the first
run of each trigger (acceptance step A1).

### 11.7 New topics

The 4 new inputs (QoS 1, all commands: a retained message on one is ignored and deleted): `radar_zones/zone/+/clear_delay/set`
(3 to 900), `radar_zones/zone/+/margin/set` (-50 to 100), `radar_zones/zone/+/forget` (`PRESS`) and
`radar_zones/zone/+/+/entity/set` (`ON`/`OFF`): 34 subscriptions in all. A value outside its range, a payload that is not a
number, a zone that has no entity or a variant the zone does not have is dropped and counted (`zone_cmds_ignored`); the zone id
must be `z0001` or `l0001` in shape. The outputs are listed in section 11.5; 6 discovery messages for each zone come on top of
the 35 of section 10.7 at every connect and 0-10 s after Home Assistant's birth message, and the not retained states are
sent again 10 s after each burst (and 10 s after a new zone's config), because Home Assistant creates an entity only after its
config and drops earlier states.

### 11.8 What to check once on the real installation

Nothing in P6 could be run against Home Assistant. Check, in this order: install the spot-light blueprint and trace each
trigger (A1); over one night (00:00 and 06:00) with somebody in a zone at 06:00 and again with nobody, read the entity
history of the zone: `unavailable` then the state with no `off` row in between, and no entry in the event history; restart
the app while somebody sits in a zone and see the same (A2); step across a zone edge about 50 times and compare the app's
`t_dev_ms`-based events with Home Assistant's `last_changed` (A3); the margin at the real edges and the pass-by rule, a
0.8 m/s walk past a dwell zone gives no `enter` while sitting down does (A4); a week of hidden-spell data for the delay
defaults (A5); the second radar's OUT against the track in a zone and the OUT-to-presence delay (A6); a walk out through a
real door (A7); one week of recorder rows per zone (A8); add, rename and remove a zone and 'Forget all learned data' while
Home Assistant runs: entities appear within about 10 s, a rename keeps the entity id, a removed zone has no entity after a
restart (A9); tuning entities and a restart (A10); 'Guests present' while a zone is occupied (A11); a 24 h resource check with
24 zones (A12).

## 12. P7: drift, moves, retire, radar moved and tuning

P7 lets the learner notice that the room changed: a piece of furniture was moved, a place has not been used for weeks, the
radar itself was moved or turned. **The learner only recommends.** Every change is a review item that needs your answer (the
same select and buttons as section 10); the only things it does silently are internal numbers (the drift ratios, the old
position of a moved place, the freeze) and they show up as counts in the status attributes (section 12.6). The app switches
these detectors on itself; there is no option for them, and the firmware and the node are not touched (P7 sends no node
command). The P7 data lives in one optional key `zone7` inside the `p5` section of the state (schema 4 stays): a 0.6.0 or
0.5.0 app keeps it and writes it back unchanged, and an installation with no zone writes exactly what 0.6.0 wrote.

### 12.1 The review items and what every answer does

| Item (select label) | Question | Approve | Rename | Reject | Later |
|---|---|---|---|---|---|
| `Moved A` | 'Place A moved?' | **Yes, update it**: the zone takes the new place and keeps its id, its name, its kind, its entities and every automation bound to them; the old spot is remembered as moved-from (section 12.3) | **No, it is a new place**: a new zone is made at the new spot, the old zone stays as it is | ignore the new spot: it is rejected as a place (asked again only after its evidence has doubled and 30 days have passed) and the old zone is untouched | asked again after the next nightly run |
| `Extend A` | 'Extend place A?' | the zone grows by the place (as in P5) | keep it as a new place | ignore it | as above |
| `Retire A` | 'Remove place A?' | **removes the zone and its entities**; automations that use them stop working; the old spot is remembered as retired | not applicable | **Keep**: the question is not asked again for 90 days | as above |
| `Radar moved` | 'The radar seems to have been moved or turned (about 80 cm, 10.0 degrees)' | **Reset the learned maps** (section 12.4); your places are kept and checked again | not applicable | **Ignore**: the detector is quiet for 14 days | as above |

- **Order.** `Radar moved` is shown after `Night window` and `Bed zone`; then come the moved places (a move is the most
  time-sensitive item and is never cut by a long list of new places), the new places by confidence, the extensions and the
  retire items last. At most 12 of those are shown, the rest is counted in `more`.
- **Texts.** With 'Detailed review text' off a P7 text names the place by its tag (`A`) only and carries no zone name, hour or
  date, only a distance or a count; with the switch on it names the zone. Every text puts the three action sentences first and
  is at most 255 characters.
- **`zone_busy`.** Approving a `Moved` or a `Retire` item is refused (review event `answer_ignored`, reason `zone_busy`) while
  somebody is in the zone, so a zone is not reshaped or removed under a person; the item stays open and you answer when the
  place is empty. Reject and Later are never refused. A bed or a frozen zone is not moved or removed by an answer
  (`not_applicable`), and a mark made with 'Mark spot here' never gets an item at all: a mark is your own truth (remove it
  and mark again).
- **A tap on an older question does nothing.** The revision of a `Moved`, `Retire` or `Radar moved` item never repeats, so the
  second move of the same place is a new question, and a tap on the notification of the first one is refused as `stale`.

### 12.2 Drift, 'frozen' and why a bed is never flagged

Every night, for every zone and every mark, the app compares how much a zone was used in the last 3 covered days with the last
14, **per observed second** (a covered day has at least an hour of observation), and only over the hours that were observed
at least half the time in both windows. The two windows are weighted by age (half-lives of 3 and 14 covered days), which is
an interpretation of the design's 3-day and 14-day windows. A ratio under 0.6 is *falling*, under 0.25 for 3 nights is
*flagged* (it only counts, `zones_unused`) and under 0.1 for 21 covered days leads to a retire item.

A zone whose usual hours were **mostly not observed** is *frozen*: it is not flagged, not moved, not retired. That is what
keeps the bed, and any place used while the radars are off (the 00:00 to 06:00 schedule), out of every item, and the same for
a place whose hours you switch the radars off for by hand. While the night schedule is ON a bed is also frozen by the P5 rule,
and so is a place that was used mostly inside the night window when it was confirmed: a bed marked by 'Mark spot here' under
the schedule from the first day has no night hours in any profile, and only this rule protects it. A young zone (under 7
nights) is not judged.
The numbers are computed for marks and beds too (the status counts them), but no item is ever made for them.

### 12.3 Moved places, the merge detector and the old spot

- **A moved place.** A new place 0.5 to 4 m from a zone that is *falling* or has been silent since it was last used, used at
  correlated hours (r at least 0.6) and, farther than 1.5 m, with at most a quarter of its use before the zone's last use,
  becomes one `Moved` item instead of a `Place` item. Approving it keeps the zone's id, so the entity ids, the device and the
  automations stay and only the attribute `poly_cm` changes. (Assumption HA[U10]: Home Assistant leaves the registry
  and the history alone when only that attribute changes; settled by acceptance step A1.) A shift of 0.3 to 0.8 m is
  usually folded into the old place by the learner itself and is offered as a `Moved` item for the same reason; for that
  kind of item (the zone's own suggestion moved) Rename and Reject answer as in P5: Rename makes a new zone from it, Reject
  marks that revision as seen.
- **The merge detector** (`Extend`): a new place that touches a zone within 0.4 m, or overlaps it by 20 %, is used at similar
  hours and whose zone is still in regular use. A moved place wins over an extension for the same candidate.
- **The old spot.** After you approve a move, or remove a zone, the old position is remembered (*moved-from*, up to 50). A
  suggestion there is not offered as a new place until the spot qualifies again **on the stays after your answer alone**.
  'Forget rejections' clears these masks (section 12.5).
- **Retire.** A zone that is not frozen, not a bed and not a mark gets a `Retire` item after **21 covered days with a ratio under 0.1**
  (a day with a ratio of 0.1 or more starts the count again), when the hours it is usually used in were observed at least half
  the time over those days, no move or extension is pending for it and no 'Keep' was answered in the last 90 days.
- **A one-week guest.** After night 14 a new ordinary place is offered only when its evidence spans at least 10 covered
  days, because the learner's own thresholds would offer a seat that a guest used for 7 days on the night after the guest
  leaves. The cost: a genuinely new seat is offered about 11 days later than before (places that were first suggested before
  night 14 are exempt, so the first setup is as before). The withheld ones are counted in `places_probation`.
  'Guests present - pause learning' (section 10.6) stays the real protection: paused time feeds nothing, so those days
  count for nothing in any of these numbers.

### 12.4 'Radar moved': the detector, the push and Reset

- **What it looks at.** The door events (where people appear and vanish) and the *static clutter events* (a reflector that
  gives byte-identical frames while nobody is there, and, with the context blueprint, clutter seen while the home was empty). A
  moved sofa moves neither. Each night the points of the newest 1 to 3 covered days are compared with the other days: the
  app searches shifts up to 1 m and turns up to 10 degrees about the radar. A night counts as *moved* when the best
  transform is at least 0.4 m or 5 degrees, at least 70 % of the recent points then lie on or next to the older points, and
  that beats the unmoved position by at least 20 percentage points. **Two such nights in a row** open the item and send one generic push, 'Radar
  needs attention' (section 10.4). It needs at least 10 recent and 30 older points: with no door in the view and no static
  reflector it stays silent (`radar_moved` in the status says `none`, `watching`, `open` or `ignored`).
- **The effective limits are higher than the design's 0.3 m and 5 degrees.** Measured on the replay's door events the detector
  finds about **0.6 to 0.8 m and 7 to 10 degrees**; a 6-degree turn or a 0.5 m move may be missed. If you move the radar by
  a small amount, use the buttons yourself (section 12.5) or report the miss (acceptance step A2).
- **The item stays until you answer it.** A real move must not disappear because the moved days enter the comparison. While
  the two nights are being counted and while the item is open (for at most 14 days) the learner **holds** the other
  proposals and the drift counters, so no zone is called falling because the frame changed.
- **Reset.** Approve clears everything learned in the old radar frame: the maps, the door, clutter and stay lists, the
  candidates and suggestions (marked stale), the rejection masks, the Later marks of places and the moved-from masks. It
  keeps the coverage record, the night model and the settings, your answers to the night-window and bed items, the zone
  records and marks with their P6 tuning, and the usual-hours profiles. Every zone and mark becomes **needs re-check** for 28
  days: it is held, not retired, and a `Moved` item re-anchors it under relaxed rules (a distance from 0 m, no ratio test),
  and a new place that overlaps it is hidden (`places_recheck_held`). A zone that the turn displaced by at least 0.3 m gets
  its `Moved` item within about two weeks.
- **A limitation you should know.** Until a zone is re-anchored the live evaluation of section 11 still uses its old cells, so
  its occupancy is wrong after a real rotation. The zone entities do **not** go unavailable meanwhile, because a false alarm
  would then drop every automation. Reset sends no command to the node.
- **Reject = Ignore.** Nothing changes; the detector is quiet for 14 days (the comparison is mostly the new position by then).

### 12.5 Forget

'Forget rejections' (section 10.3) now also clears the moved-from and retired masks, every 'Keep' date, the radar-moved Ignore
mark and the snoozes of `Retire` and `Radar moved`, as it clears every rejection and every Later. The per-zone button 'Forget
learned data' (section 11.4) restarts only that zone's *unused* counters (the flag and the retire runs): it must not erase your
90-day Keep or the zone's usual-hours profile. 'Forget all learned data' drops all of it, and the tuning folder.

### 12.6 Tuning data, 'Export tuning data' and the status counts

Every nightly run the app adds **one line of counts and fixed-length histograms** to `/data/tuning/nights.jsonl` (the newest 28
nights): the observed hours, stays and histograms of their duration, LD2412 share, fragments and spread, the hidden spells and
the learned clear delays per zone kind, how many candidates failed each gate and the histograms of the candidates' own
numbers, the drift ratios and the zone-nights per state, the door events, how the 'Mark spot here' marks matched suggestions (a
count, no position) and your cumulative answer counters. It has **no date, no night number, no hour of day, no position, no
name and no id**; a test enforces the list of keys. The file is local, excluded from backups (`tuning/*`) and deleted by
'Forget all learned data'.

| Entity (learner device) | What it does |
|---|---|
| `button.radar_zones_learner_export_tuning` ('Export tuning data', config) | Publishes **one pooled record** once: the sums of the kept nights and the newest value of the cumulative counters, no per-night list, no date. The record is checked first (the same privacy scan, at most 16 KiB); with nothing kept nothing is published and the press is counted. The topic is `radar_zones/learner/tuning/export`, **not retained**, and **no file is written** (the app has no `map:` folder you could reach). |
| `sensor.radar_zones_learner_tuning` ('Tuning data', diagnostic) | `none` or `<n> nights`: how many lines are kept. Nothing else. |

To use it, wait 2 to 4 weeks. Start a listener first, because the message is not retained and a late listener gets nothing:
`mosquitto_sub -t radar_zones/learner/tuning/export -C 1 > tuning.json` (add `-h`, `-u` and `-P` for your broker), or the
'Listen to a topic' tool of the MQTT
integration (assumption HA[U8]: a message of up to 64 KiB is delivered to it and can be copied out; settled by acceptance step
A7). **Read the file before you share it.** Then, on a PC with Python 3.12 or newer, `python -I -B replay/tune.py --aggregate
tuning.json` prints proposals for the constants (the bridge gap, the default clear delays, the confidence and gate
thresholds), or the reason it has none; there is none below 14 covered days. A proposal is applied only by a release: **nothing
is tuned at runtime** except the clear delay of each zone that P6 learns by itself. The ratio thresholds of this section are
never proposed; you read their distributions and decide (acceptance step A8).

The export is your action on purpose. The record is counts only, but it says how many places the household confirmed and
how you answered, and an MQTT message can be read by Home Assistant and every add-on that uses MQTT. Each export logs
`tuning export: <n> nights` and nothing else, and the status attribute `tuning_exports` counts them.

The status attributes (counts only: never a name, an id or a coordinate) gain `zones_frozen`, `zones_blind`, `zones_falling`,
`zones_unused`, `zones_recheck`, `moved_from`, `radar_moved`, `places_probation`, `places_recheck_held`, the counters
`zone7_pruned`, `zone7_reset` and `p7_errors`, and from the shell `tuning_exports`, `tuning_export_refused`,
`tuning_write_errors` and `p7_shell_errors`. A step of the P7 nightly work that fails is counted (`p7_errors`, and one error
line that starts `p7:` with the step's name) and never stops the review, the suggestions or the zones.

### 12.7 HOLD mode is not built

The design deferred a 'HOLD' scan mode (zones held instead of unavailable). Under the owner's decision for this installation
(one switch for both radars, a fixed night schedule, zones `unavailable` while the radars are off, no wake source) it has no
use and is not built, designed or reserved.

### 12.8 New topics and entities

One new input (QoS 1, a command: a retained message on it is ignored and deleted): `radar_zones/learner/export_tuning`
(`PRESS`), which makes 35 subscriptions in all. Outputs: retained `radar_zones/learner/tuning`; not retained
`radar_zones/learner/tuning/export`. Two discovery messages (the button and the sensor) come on top of the 35 of section 10.7
and the 6 for each zone. The push request sensor can now have the kind `attention` (section 10.4). P7 adds no per-zone entity.

### 12.9 What to check once on the real installation

Nothing in P7 could be run against Home Assistant or a real room. Check: move a real piece of furniture by 1 to 1.5 m and use it
as before: within about a week a `Moved` item, Approve, the zone keeps its entity id and an automation bound to it fires at
the new place (A1); turn the radar by about 10 degrees (or move it 0.8 m): within 2 to 3 nights 'Radar needs attention' and
the `Radar moved` item, which stays until answered, and a false-alarm count of 0 over 4 weeks without a move (A2); the real
bed under the 00:00 to 06:00 schedule for 4 weeks is never flagged (`zones_unused` stays 0), and how many nights
`zones_frozen` counts it (A3); a place nobody uses for more than 3 weeks gets a `Retire` item after about 3 weeks, not
earlier, Keep silences it for 90 days, Remove leaves no ghost entity after a restart of Home Assistant (A4); a week with a
guest, with and without the pause switch, and how many days the first genuinely new seat then takes to be offered (A5); copy
the changed push blueprint and trigger the three cases (A6, assumption HA[U9]: the kind `attention` fires the blueprint like the
others); 2 to 4 weeks and the export button (A7); the counts of `Moved`, `Retire` and `Radar moved` items against what really
happened (A8); the share of a day zone's usual hours that is observed, and what a manual 'Radars' switch-off automation does to
a zone (A9); 24 hours on the CM4 with many zones (A10: the nightly work, `rss_mb` under 40, `cpu_pct_1h` under 1).

## Development

Everything except the MQTT client runs with the Python standard library (3.12 or newer). From this
folder:

```
python -I -B -m unittest discover -s tests -t .
python -I -B -m unittest discover -s tests -t . -p "test_service.py"
```

The tests need no broker and no paho-mqtt: `tests/fakes.py` provides a fake client. The P6 shell tests are
`tests/test_p6_*.py` (run them with `-p "test_p6_*.py"`); `tests/fake_ha_zone.py` is a generic fake Home Assistant for zone
entities (an entity registry, availability lists, an event entity log, a recorder model) whose behaviours are the
assumptions of the P6 specification (`HA[U1]`-`HA[U7]`), `tests/p6_harness.py` drives the real layer, and the real `Learner`
behind the real service is used where the core's output matters. The P7 shell tests are `tests/test_p7s_*.py` (run them with
`-p "test_p7s_*.py"`); `tests/p7s_harness.py` is `p6_harness` with the P7 topics and layer and a fake Home Assistant that also
models the 'Listen to a topic' tool (assumption `HA[U8]`), and the `P7Params(enabled=True)` learner behind the real layer is
used for the modify, retire and nightly-line cases. Set `RZL_TEST_TMP` to
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

The P5 checks (P5-1 to P5-10 and the regression checks P5-R1 to P5-R3, spec section 11) run with `--profile p5smoke` (one
9-night run) or `--profile p5`; the checks that need Home Assistant's behaviour (the push flow across restarts, the
blueprint text) are unit tests: `tests/test_p5_push_flow.py` runs the real shell layer against `tests/fake_ha.py`, a Python
port of the blueprint (a fake broker keeps the retained messages, an entity exists only after its discovery config was
seen, Home Assistant can restart), and `tests/test_p5_blueprint.py` checks the blueprint's text. The Python port is what
the tests trust; the blueprint itself has to be traced once in Home Assistant (section 10.9).

The three regression checks compare this tree with the 0.4.0 baseline, which is read-only and lives outside the
repository (`git archive 58804fa ha-addon`, extracted into a scratch folder; both trees contain a package named
`radar_zone_learner`, so each is dumped by its own process). From this folder, with `BASE` the extracted
`ha-addon/radar_zone_learner` of the baseline:

```
python -I -B replay/dump_run.py --tree BASE --jobs bed_w0000_0600,liv_w0000_0600 --seeds 1-3 --nights 14 --out OUT/r1_base
python -I -B replay/dump_run.py --tree BASE --jobs bed_w0000_0600_p4 --seeds 1 --nights 21 --out OUT/r1_base
python -I -B replay/dump_run.py --tree . --jobs bed_w0000_0600,liv_w0000_0600 --seeds 1-3 --nights 14 --out OUT/r1_p5
python -I -B replay/dump_run.py --tree . --jobs bed_w0000_0600_p4 --seeds 1 --nights 21 --out OUT/r1_p5
python -I -B replay/compare_runs.py --a OUT/r1_base --b OUT/r1_p5          P5-R1: learning is unchanged
python -I -B replay/compat_check.py --base BASE --out OUT/compat           P5-R2: state files load in both directions
python -I -B replay/resource_probe.py --p5 --out OUT/probe                 P5-R3: the cost of the P5 additions
```
