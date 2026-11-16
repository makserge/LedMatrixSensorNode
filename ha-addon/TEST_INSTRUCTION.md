# Radar Zone Learner: test instruction for a Home Assistant test instance

Version of this page: 2026-11-16 (after the first hands-on test of 0.7.1, report `Radar_Zone_Learner_0.7.1_Test_Report.md`). It applies to 0.7.1 and
to 0.7.2. The app is installed from GitHub (`https://github.com/makserge/LedMatrixSensorNode#ld2454-zone-learning-p1`, Settings > Apps > App store >
menu > Repositories) or from a zip in the `addons` share. Use the address of your own test Home Assistant (the first tests ran on 192.168.8.233, the
second instance is 192.168.8.100). The log shows `connected to core-mosquitto:1883` and `subscribed (35 topics)` (36 from 0.7.2).
Details of every check are in `TEST_0.6.0.md` (zones, tests T1 to T10) and `TEST_0.7.0.md` (P7, tests U1 to U7); this page is the order to run them in
and the corrections found so far.

Nothing the app does changes your home: it only publishes entities and review items. Lights and scenes switch only if you create the optional
blueprint automation.

Names in newer Home Assistant versions: "Developer tools" is called **Tools**, "Add-ons" are **Apps**; the MQTT listener is under
**Settings > Devices & services > MQTT > Configure**.

## Step 0: is anything coming in? (5 minutes, do this first)

1. MQTT > Configure > "Listen to a topic": `led_matrix_node/radar/ld2454/frames`, Start listening. You should see a message about every 5 s when
   the room is idle and about every 1 s when something moves.
   - Nothing: the node is not streaming frames. The node needs the firmware with the LD2454 frames stream (phase P1). Flash it, then repeat.
2. Settings > Apps > Radar Zone Learner > Log: no `ERROR`, no `Traceback`. After the first frames: `status learning`.
3. The learner status entity attributes: `observing` true, `node_online` true, `radar_moved` `none`, `p7_shell_errors` 0 (the counter is `p7_shell_errors`,
   not `p7_errors`), `core_errors` 0.

## Step 1: the P7 checks (30 minutes, TEST_0.7.0.md U1 to U7)

| # | Do | Expect |
|---|---|---|
| U1 | Read the log and the entity list. | 35 topics (36 from 0.7.2), `button.radar_zones_learner_export_tuning` and `sensor.radar_zones_learner_tuning` exist, no error. |
| U2 | Learner status attributes. | `zones_frozen`, `zones_blind`, `zones_falling`, `zones_unused`, `zones_recheck`, `moved_from`, `radar_moved`, `places_probation`, `p7_shell_errors` = 0. |
| U3 | Press "Export tuning data" while listening to `radar_zones/learner/tuning/export`. | Nothing published before the first nightly run; the press is only counted. |
| U4 | After the first nightly run (03:30 local): look again. | `sensor.radar_zones_learner_tuning` shows `<n> nights` (it is kept across an update, `1 nights` only on a fresh install); a bed counts in `zones_frozen`, never in `zones_unused`/`zones_falling`. |
| U5 | Press "Export tuning data" again. | One JSON record with counts and histograms only (no names, ids, dates, positions). Read it before sharing it. |
| U6 | Press "Forget learned data" on a zone. | Nothing new in the review select. |
| U7 | Restart the app while a zone is occupied. | `unavailable`, then the state, no event. |

## Step 2: zones and occupancy (60 to 90 minutes, TEST_0.6.0.md T1 to T10)

Needs a zone. If you have none: stand or sit at the spot and press "Mark spot here" (learner / review device). A radar mostly sees movement: **do not
hold perfectly still**, shift your weight or move an arm a little for 25 s, within 0.5 m of the spot, then press once. The spot becomes `l0001` with six
entities within about 10 s (`binary_sensor.radar_spot_l0001`, `..._count` (disabled by default, enable it), `event.radar_spot_l0001_event`,
`number.radar_spot_l0001_clear_delay`, `number.radar_spot_l0001_margin`, `button.radar_spot_l0001_forget`).

Run T2 (sit, leave), T3 (the radar loses you: in practice not reproducible on demand, mark it "not run" rather than failed), T4 (set the clear delay
to 20 s), T6 (stop and start the app while you sit in the zone), T7 (walk past), T9 (rename, remove a test zone), T10 (restart Home Assistant). T5 (radars
off) needs the rev 2 board with the radar switch, or a node restart on the current board.

Things that look like bugs but are by design:
- The **learned clear delay** changes after a nightly run (23 s becomes 21 s): it is learned from your own hidden spells (nearest-rank p95, 3 to 900 s).
  A number you set yourself stays until 'Forget'.
- The learner status attribute `last_mark` is only as exact as 5 minutes (`at` is rounded on purpose) and a repeated press with the same result looks
  identical. From 0.7.2 it has a counter `n`.
- The event entity's `via` and `dwell_s` are attributes of the **event entity state** (Tools > States > `event.radar_spot_<id>_event`), read them right
  after the event. If `event.radar_spot_*` is in your recorder exclude (privacy choice), History and Logbook stay empty; remove the exclude for one short
  run if you need the history.

## Step 2b: 'Mark spot here' (0.7.1 and later)

| # | Do | Expect |
|---|---|---|
| V1 | Stay at the spot for 25 s with small movements, press once. | `ok`, a new label/zone at the spot (not a second zone if one is already there). |
| V2 | Leave the room (nobody in the radar's view), wait 90 s, press once. | `no_stay`, no zone is created. 0.7.1 accepted such a press once (report finding V2, cause unknown): if it happens again, switch 'Debug capture' on before the test and tell me the time. |
| V3 | Stay 25 s, leave and press within 30 s. | `ok` (a stay up to 60 s old counts, the 0.7.1 change); after more than 60 s: `no_stay`. |
| V4 | Press twice within 10 s. | The second is refused `too_soon`. |
| V5 | A refused press: read `last_mark.detail`. | `live_confirmed_tracks`, `best_run_s`, `last_target_age_s`, `recent_stays` (counts and seconds only). From 0.7.2 an accepted press has `detail` too (`source` live or recent, `age_s`, `dur_s`). |

Remove a test zone you do not want (review device, Advanced list, "Remove zone") before the bed check (U4): a zone marked while the `Bed zone` review item
is selected becomes a bed.

## Handing a failing run to the developer

- **0.7.2 and later:** switch 'Debug capture' ON (learner device, it switches itself off after 48 h), repeat the failing step, write down the time,
  press **'Export debug data'**. The app copies the capture (frames only) and the state into the Home Assistant share, folder
  `\\<your HA>\share\radar_zone_learner\export-<time>\` (the newest 3 are kept). That folder holds raw positions and the learned map: it is for you and the
  developer; delete it when done. Send the folder.
- **0.7.1:** the capture stays inside the app's private folder and cannot be taken out. Update to 0.7.2.
- With a capture on your PC: `python -I -B replay/analyze_capture.py <folder or file>` shows the radar's gaps, every track's lifetime, walking speed and
  whether a 20 s stay exists (the rule behind 'Mark spot here').

## Step 3: optional, push and blueprints

- Review push: copy `blueprints/radar_zone_review_push.yaml` from the repo (`ha-addon/radar_zone_learner/blueprints/`) to
  `/config/blueprints/automation/radar_zone_learner/` and create the automation (needs your notify service).
- Spot-to-light: copy `blueprints/radar_zone_spot_light.yaml` the same way, create one automation per spot, open the trace of the first run.
- Copy the files over the Samba share `\\<your HA>\config` (LF line endings).

## Step 4: what takes days to weeks (acceptance steps)

P6 A1 to A12 and P7 A1 to A10 are in the README (sections 11 and 12) and in the last part of the two test files. In short:
- Move a real place by 1 to 1.5 m: a `Moved <tag>` item within about a week; Approve keeps the zone id and entities.
- Turn the radar by about 10 degrees or move it 0.8 m: "Radar needs attention" and a `Radar moved` item within 2 to 3 nights. No false alarms in 4 weeks.
- Bed under the 00:00 to 06:00 schedule: `zones_unused` stays 0 for 4 weeks.
- A place unused for 3 weeks gets a `Retire` item; Keep silences it for 90 days.
- After 2 to 4 weeks: export the tuning data and send it (counts only).

## What to send back

The add-on log (Settings > Apps > Radar Zone Learner > Log), the learner status attributes, and for any step that did not match "Expect": the step
number, the time and a screenshot or the Export debug data folder.

## Known limits (so they do not surprise you)

- P7-2 on the PC replay: 23 of 26 runs; a moved sofa is found in 4 to 8 nights, one marginal case has a less exact new area.
- Bed without night learning: needs real radar data to be judged.
- A walker who pauses for half a second next to a dwell spot can count as sitting down. Z7 (walking past a zone turned it on once in the first
  test) is not explained yet: it needs a capture of the walk and the walking speed (the analysis tool prints it).
- The radar-moved detector finds about 0.6 to 0.8 m and 7 to 10 degrees; smaller moves can be missed.
- After a real radar rotation zone occupancy uses the old cells until a `Moved` item is approved (zones do not go `unavailable` meanwhile).

## Update and rollback

- Update: a new version number must be pushed to the repo branch; then Apps > App store menu > Check for updates > Update. Same version number = no
  update offered. An update keeps the learned state (`nights` and the tuning data stay).
- Roll back: install the older commit's folder as a local app with a higher version, or switch the repository branch (Settings > Apps > App store >
  menu > Repositories).
