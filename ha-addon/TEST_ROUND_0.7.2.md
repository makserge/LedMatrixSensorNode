# Radar Zone Learner 0.7.2: short test round (about 35 minutes)

Prepared 2026-11-16 from the first hands-on report (0.7.1: 10 pass, 2 partial, 2 fail, 1 not run). One session, everything combined, with a capture
running, so a failure can be analysed afterwards. The general order and the "looks like a bug but is by design" list are in `TEST_INSTRUCTION.md`.
Write the time (to the minute) next to every result.

Instances: 192.168.8.233 (0.7.2 installed, MQTT added, **no frames arrived yet**) and 192.168.8.100 (update it to 0.7.2 first).

## 1. Check (5 minutes)

| # | Do | Expect |
|---|---|---|
| 1a | Settings > Apps > Radar Zone Learner: version 0.7.2, log shows `subscribed (36 topics)`. MQTT > Configure > listen to `led_matrix_node/radar/ld2454/frames`. | A frames message every 5 s (idle) or 1 s (movement). **Nothing: stop**, the node does not publish to this broker. |
| 1b | Learner status attributes: `observing` true, `p7_shell_errors` 0, `core_errors` 0. Remove the test bed zone `l0002` (review device > Advanced list > Remove zone). Keep `l0001`. | `zones_total` 1. |

## 2. One capture session (25 minutes)

Switch **Debug capture ON** (learner device > Configuration) and keep it on until step 2e.

| # | Do | Expect | Write down |
|---|---|---|---|
| 2a (V2) | Leave the room (out of the radar's view), wait **90 s**, press 'Mark spot here' once from your phone. | `last_mark.result` `failed`, `reason` `no_stay`, `zones_total` unchanged. | Time; `last_mark` (all fields); `zones_total` before and after. |
| 2b (Z7) | Walk a straight 3 m path at about 1 m/s, never stopping: **3 times through** the spot, **3 times 0.3 m beside** its edge. | The zone never turns `on`. | For each pass: time, distance, `on` yes or no. |
| 2c | Sit in the zone 30 s, then leave the room. Right after leaving read Tools > States > `event.radar_spot_l0001_event`. | Zone `on` while you sit, `off` after you leave; the event shows `leave` with `via` (`edge` or `hold_expired`) and `dwell_s`. | Times, `via`, `dwell_s`. |
| 2d | Sit in the zone again and **restart the app** (Settings > Apps > Radar Zone Learner > Restart). | `unavailable`, then `on`; no `off` blip, no event. | Times. |
| 2e | Press **Export debug data** (learner device > Diagnostic). Switch Debug capture OFF. | A folder `\\<HA>\share\radar_zone_learner\export-<UTC time>\` with `capture-*.jsonl`, `state.json`, `README.txt`. | Folder name. |

If 2a or 2b fails (a mark accepted, a zone turned on), **send the export folder** and your times. On the PC it is read with
`python -I -B replay/analyze_capture.py <folder>`.

## 3. Next day (2 minutes)

Read `rss_mb` and `cpu_pct_1h` in the learner status attributes (rss about 25 MB, cpu well under 1 %) and `sensor.radar_zones_learner_tuning`
(one more night, `p7_shell_errors` still 0). Delete the `export-*` folder from the share when the developer has it (it holds raw positions).

## Later, not today

- 'Mark spot here' tests (normal press, a press within 30 s after leaving, two people, too soon): skipped until the stay rule is simplified.
- Clear delay 20 s hold (T4), node restart (T5), the spot-to-light blueprint (T8), rename/remove zone (T9), Home Assistant restart (T10).
- The long checks: P6 A1 to A12 and P7 A1 to A10 (README sections 11 and 12): moved furniture, moved radar, bed over 4 weeks, retire, guest rule,
  tuning export after 2 to 4 weeks.

## Result table

| Step | pass / partial / fail / not run | Time | Note |
|---|---|---|---|
| 1a, 1b | | | |
| 2a | | | |
| 2b | | | |
| 2c | | | |
| 2d | | | |
| 2e | | | |
| 3 | | | |
