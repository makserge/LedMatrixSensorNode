# Radar Zone Learner 0.7.0: test instruction for the test Home Assistant

State on 2026-11-06: the app is installed from GitHub (`https://github.com/makserge/LedMatrixSensorNode#ld2454-zone-learning-p1`), version 0.7.0,
running in shadow mode on 192.168.8.233 (Settings > Apps > Radar Zone Learner). The log shows `connected to core-mosquitto:1883` and
`subscribed (35 topics)`. 0.7.0 contains P6 (zone occupancy) and P7 (moves, retire, radar moved, tuning data). Details of every check are in
`TEST_0.6.0.md` (zones, tests T1 to T10) and `TEST_0.7.0.md` (P7, tests U1 to U7); this page is the order to run them in.

Nothing the app does changes your home: it only publishes entities and review items. Lights and scenes switch only if you create the optional
blueprint automation.

## Step 0: is anything coming in? (5 minutes, do this first)

1. Home Assistant > Developer tools > MQTT > "Listen to a topic": `led_matrix_node/radar/ld2454/frames`, Start listening. You should see a
   message about every 5 s or more often.
   - Nothing: the node is not streaming frames. The node needs the firmware with the LD2454 frames stream (phase P1). Flash it, then repeat.
     Everything below needs frames; without them the learner stays idle (that is correct, not a fault).
2. Settings > Apps > Radar Zone Learner > Log: no `ERROR`, no `Traceback`. After the first frames: `status learning`.
3. Settings > Devices & services > MQTT > devices: the learner device(s) exist (names start with `Radar zones learner`). Open the learner status
   entity and look at its attributes: `zones_live`, `zones_frozen`, `radar_moved` (`none`), `p7_errors` (0).

## Step 1: the 0.7.0 checks that work today (30 minutes, TEST_0.7.0.md U1 to U7)

| # | Do | Expect |
|---|---|---|
| U1 | Read the log and the entity list. | 35 topics, `button.radar_zones_learner_export_tuning` and `sensor.radar_zones_learner_tuning` exist, no error. |
| U2 | Learner status attributes. | New keys `zones_frozen`, `zones_blind`, `zones_falling`, `zones_unused`, `zones_recheck`, `moved_from`, `radar_moved`, `places_probation`, `p7_errors` = 0. |
| U3 | Press "Export tuning data" while listening to `radar_zones/learner/tuning/export` (Developer tools > MQTT). | Nothing published before the first nightly run; the press is only counted. |
| U4 | After the first nightly run (03:30 local): look again. | `sensor.radar_zones_learner_tuning` shows `1 nights`; a bed counts in `zones_frozen`, never in `zones_unused`/`zones_falling`; `p7_errors` stays 0. |
| U5 | Press "Export tuning data" again. | One JSON record with counts and histograms only (no names, ids, dates, positions). Read it before sharing it. |
| U6 | Press "Forget learned data" on a zone. | Nothing new in the review select. |
| U7 | Restart the app while a zone is occupied. | `unavailable`, then the state, no event. |

## Step 2: zones and occupancy (60 to 90 minutes, TEST_0.6.0.md T1 to T10)

Needs a zone. If you have none: sit still in the radar room for 25 s and press "Mark spot here" (learner / review device). The spot becomes `l0001`
with six entities within about 10 s (`binary_sensor.radar_spot_l0001`, `..._count` (disabled by default, enable it), `event.radar_spot_l0001_event`,
`number.radar_spot_l0001_clear_delay`, `number.radar_spot_l0001_margin`, `button.radar_spot_l0001_forget`).

Run T2 (sit, leave), T3 (sit very still, held until clear delay), T4 (set the clear delay to 20 s), T6 (stop and start the app while you sit in the
zone), T7 (walk past), T9 (rename, remove a test zone), T10 (restart Home Assistant). T5 (radars off) needs the rev 2 board with the radar switch, or
a node restart on the current board.

Always check three things in the history of the zone: no `off` row between `unavailable` and the real state, no event after a restart, and the
event `leave` carries `via: edge` (stepped out) or `via: hold_expired` (radar lost you).

## Step 3: optional, push and blueprints

- Review push: copy `blueprints/radar_zone_review_push.yaml` from the repo (`ha-addon/radar_zone_learner/blueprints/`) to
  `/config/blueprints/automation/radar_zone_learner/` and create the automation (needs your notify service).
- Spot-to-light: copy `blueprints/radar_zone_spot_light.yaml` the same way, create one automation per spot, open the trace of the first run. It has
  never been loaded by a real Home Assistant.
- Copy the files over the Samba share `\\192.168.8.233\config` (LF line endings).

## Step 4: what takes days to weeks (acceptance steps)

P6 A1 to A12 and P7 A1 to A10 are in the README (sections 11 and 12) and in the last part of the two test files. In short:
- Move a real place by 1 to 1.5 m: a `Moved <tag>` item within about a week; Approve keeps the zone id and entities.
- Turn the radar by about 10 degrees or move it 0.8 m: "Radar needs attention" and a `Radar moved` item within 2 to 3 nights. No false alarms in 4 weeks.
- Bed under the 00:00 to 06:00 schedule: `zones_unused` stays 0 for 4 weeks.
- A place unused for 3 weeks gets a `Retire` item; Keep silences it for 90 days.
- After 2 to 4 weeks: export the tuning data and send it (counts only).

## What to send back

The add-on log (Settings > Apps > Radar Zone Learner > Log), the learner status attributes, and for any step that did not match "Expect": the step
number, the time and a screenshot or a history export of the zone.

## Known limits (so they do not surprise you)

- P7-2 on the PC replay: 23 of 26 runs; a moved sofa is found in 4 to 8 nights, one marginal case has a less exact new area.
- Bed without night learning: needs real radar data to be judged.
- A walker who pauses for half a second next to a dwell spot can count as sitting down.
- The radar-moved detector finds about 0.6 to 0.8 m and 7 to 10 degrees; smaller moves can be missed.
- After a real radar rotation zone occupancy uses the old cells until a `Moved` item is approved (zones do not go `unavailable` meanwhile).
- All Home Assistant behaviour (entity creation, availability, blueprints, recorder, push) is untested on a real Home Assistant until today: that is the
  purpose of this test.

## Update and rollback

- Update: a new version number must be pushed to the repo branch; then App store menu > Check for updates > Update. Same version number = no update offered.
- Roll back: install the older commit's folder as a local app with a higher version, or switch the repository branch (Settings > Apps > App store > menu >
  Repositories).
