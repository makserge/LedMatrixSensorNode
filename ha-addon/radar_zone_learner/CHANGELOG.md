# Changelog

## 0.4.0 - P4 night window, bed item, optional Night learning

New option `night_lux` (default 5 lx, 0.1 to 500): the light level below which the room counts as dark at night.
It is the one source of the threshold (the stored value in the state only records what was in use), so the P0
measurement of the real BH1750 becomes a setting, not a code change. The app version is 0.4.0; an installed
0.3.0 offers the update and keeps its state (schema 3 is migrated to 4 on the first start).

Review fixes: the night-window start is the P90 of one onset per night (radar, else context, else lux), so nights
on which the radar was blind (asleep after the window start) are no longer dropped once the radar has 7 nights;
a later radar quiet start replaces the night's first onset only within 60 min of it (`onset_replace_max_min`),
so a 03:00 bathroom trip seen by the radar (Night learning) no longer becomes the bedtime (Night-learning replay,
seeds 1-3, largest radar onset error: 88, 50 and 65 min with a 90 min bound, 59, 50 and 4 min with 60).

### Fixes after the P4 fidelity and safety review

Night learning (safety; `core/nightlearn.py`, `shell/p4.py`):

- An unconfirmed start is no longer forgotten. After `no_echo` of the schedule OFF (from the shell's 2 min check
  or the core's own 5 min deadline) the run is cancelled, but `set_by_learner` stays False and the echo is watched
  for `nl_unconfirmed_wait_s` (new Param, 15 min): an OFF that shows up late is switched ON again
  (`expect_current` False), an echo that still reads ON ends it with `last_event` `no_echo`. The same watch, now
  15 min instead of 150 s, follows a stop while the OFF is unconfirmed. Before, `no_echo` set the phase idle and
  `set_by_learner` None, so an OFF that was applied after all (broker restart, slow node) stayed for good.
- The review events `device_not_confirmed`, `mark_failed` and `restore_failed` are kept while the broker link is
  down (at most 20) and sent when it is back (on the reconnect and in the tick). Before, the publish was skipped
  and the event was lost, although an outage is the likeliest reason for a missing echo.
- A restore refused because nothing was published (`node_offline`, `no_p2`, `not_connected`) is no longer a try:
  it waits `nl_retry_s` and goes again at once when the node's status turns online or its schedule echo arrives.
  Only a restore that was sent and not confirmed counts towards `nl_restore_tries` (12). Before, 2 hours of an
  offline node used up all tries and the learner gave up silently with the schedule OFF.
- When the learner does give up (`restore_failed`), or the restore is more than `nl_restore_late_s` (new Param,
  24 h) overdue (`restore_overdue`: after the app was down for days the OFF may be the user's own), it sends
  nothing, forgets `set_by_learner`, logs a warning and the shell fires the new review event `restore_failed`
  (`reason` `no_confirmation` or `overdue`; added to the event entity's `event_types`). `NightLearningChanged`
  has the new field `event` for this. Before, a restore was sent after any downtime and could override a later
  OFF of the user.
- After a restart the schedule value in the state is not trusted to say 'back ON': that conclusion waits for the
  node's own echo (RAM flag `fresh`), so a stale stored ON cannot end a restore whose OFF is in the node.
- README 9.5 tells the user what to do when Night learning is interrupted (switch 'Radar night schedule' ON).

Review and node link:

- `proposal_rev` no longer rises by one on every restart: the signature of the stored proposal is computed on
  load. Answers and notifications that carry the revision stay valid across a restart.
- An approved window is written as `night_start`, `night_end`, then the schedule switch (design 7.8: 'Approve
  writes #3/#4 and, if ticked, sets #2 ON'). Before the switch went first and the node applied the old window at
  once (a transient `night_off` and an extra radar power cycle when the approval fell outside the new window).
- The core's bed item text has the comma of design 7.6 ('once, before 00:00 or after 06:00'); the shell already
  rendered it so.
- 'Mark spot here' that is not bound to the bed item keeps the P3 name rule (see the interpretations); a test with
  the night-window item selected and the names Bett, BED, ' Bed ' records it.

Replay and report (`replay/`):

- The p4 profile has no run of the G1-G6 jobs (and no living-room job: the P4 design is about the bedroom). A
  P4-only report no longer reads as an unqualified PASS: the header, the log line and `report.scope` say 'P4-1 to
  P4-7 only; G1-G6 not run', and the report lists the jobs, seeds and night counts it ran (`job_matrix`). The G1-G6
  gate is run with `--profile p3`; the two profiles together are 2,040 simulated days (limit 1,500 per invocation),
  so they are two invocations. The job and night counts of the p4 profile are in `replay/jobs.py` and README.
- P4-1 is gated at the last night (21); the report now says so and shows night 14 (where the first proposal reaches
  the setup review) next to it with 'the same rule reads PASS/FAIL'. P4-R1 adds the share of nights with a wake
  error within 30 min and the learner's wake IQR against the true one.
- P4-2 needs 4 of 5 proposals in both windows (it was 1 for 23:00-07:00).
- The sub-checks of P4-3, P4-4 and P4-7 follow the P3 k-of-n rule scaled to the seeds present (4 of 5; all with
  fewer than 5). Before, one PASS and no FAIL sufficed and a step that was not exercised was ignored. The seeds
  behind each PASS are printed. P4-6 prints its margin ('at the minimum'). P4-7 states how many seeds name the
  bed place 'bed' by themselves and that the named-bed and reject-comes-back paths are covered by unit tests only.
- The Home Assistant note says what is true: the 'cleared for 15 min' trigger is NOT verified (it needs a running
  Home Assistant; the blueprint has no such trigger and its test reads text only); 'the schedule toggle never
  overrides the user' is covered by unit tests.
- P4-2 and P4-R1 are labelled 'lux proxy under an ideal lux model' (the replay's lux is derived from the truth).
- Replay after these fixes (Windows, Python 3.12, 3 workers): `--profile p3` (67 runs, 1,332.8 simulated days)
  G1-G6 all PASS; `--profile p4` (47 runs, 706.9 simulated days) P4-1 to P4-7 all PASS, with G1-G6 reported as not
  run. 638 unit tests pass (613 before; 2 skipped as before).

Not changed, with the reason: the finding that `NightModel.load` does not read the stored `night_lux`. It is
deliberate: `Params.night_lux` is the one source of the threshold (the P0 measurement becomes that constant), the
stored value only records what was in use, and `tests/test_state.py::test_the_night_lux_of_the_options_wins_over_the_file`
asserts it. Reading it would freeze the default 5.0 in every existing state, so a later change of the constant
would never reach an installed learner, and nothing writes a measured value into the state. The same finding
remarked that the blueprint has never been loaded by Home Assistant: kept as the README 9.6 caveat.

Shell side of P4 (design section P4, 6.3, 7.5-7.9). The version number is not raised here: `config.yaml`
and `APP_VERSION` change with the integration of the core side.

- New entities: 'Radar data valid' (`radar_zones/radar/valid`), 'RF on time 24 h', 'RF saving 7 d',
  'Coverage 7 d', the 'Night learning' switch (OFF by default), and on the review device 'Review item'
  (select), 'Review pending', 'Review item text', Approve / Reject / Later and 'Review event'
  (`mark_failed`, `device_not_confirmed`). The P3 entities and their discovery payloads are unchanged.
- The app subscribes to 9 more topics (18 in all): `sensors/state` (lux), `radar/state`, `radar/observing`,
  the contexts `lights_off`, `sleep_hint` and `guests`, and the three new command topics. Retained commands
  are ignored and cleared. `build_topics` and `TopicMapper` (P3) are untouched; the P4 ones are
  `build_topics_p4` and `TopicMapperP4`.
- Node link: the learner writes only `radar/schedule`, `radar/night_start` and `radar/night_end`, never
  retained and never `radar/power`. Nothing is sent before the node's schedule echo has been seen (a node
  without the P2 firmware never gets anything) or while the node is offline; a command with
  `expect_current` is refused when the node's schedule differs (a change by the user stands); the echo is
  checked for 2 minutes.
- RF and coverage numbers from `radar/state` transitions, in `/data/rf_stats.json` (15-minute buckets, 8 days).
- Context blueprint `blueprints/radar_zone_context.yaml` (home empty, guests, optional lights off and sleep hint).
- README section 9; tests `test_p4_*`, `test_nodelink`, `test_rfstats`, `test_blueprint`, `FakeNode` in
  `tests/fakes.py`.
- The P4 layer is optional in `Service` (`p4=None` is the P3 app); `main()` turns it on.

### Interpretations and open points (for the integration)

- The design does not name the hand-off between shell and core. The shell defines it in
  `shell/p4_types.py` (events `LuxSample`, `ReviewSelect`, `ReviewAnswer`, `NightLearningSwitch`,
  `CommandResult`; outputs `NodeCommand`, `ReviewChanged`, `NightLearningChanged`; and the P3 `ContextState`
  for the new context names). Move them to `core/types.py` and re-export them from `p4_types.py`. The core
  may also report Night learning in the status attribute `night_learning` (`{"active": bool, "until": str}`).
- The shell decides nothing about learning. Onsets, wakes, the proposal, the bed item rules (withdrawn and
  shown again), suppressions, the Night learning state machine and `set_by_learner` are core work; a P3 core
  sends no review items, so the review entities stay at `None` and Night learning stays OFF.
- 'Radar data valid' is `online` iff the core status is `learning` (observing, node online, frames within
  15 s) and `radar/observing` is not OFF. The design lists it as an availability topic; it is also
  discovered as a diagnostic binary sensor. After an unclean exit the retained value stays until the next
  start (the learner's own availability topic goes `offline` by its will).
- 'Coverage 7 d' is the share of 168 h in which the learner was observing (not the core's per-hour-of-week
  `o_obs`, which the core does not export yet).
- The review select has fixed options (`None`, `Night window`, `Bed zone`); P5 adds the place items.
  Buttons carry no revision; the shell fills in the selected item and its revision and drops answers for
  a stale one.
- 'Edit times' on the night-window item and the 'switch-on box' as a widget are P5; the box exists only as
  `sched` in a hand-sent answer.
- The context blueprint is checked as text only (no YAML parser or Home Assistant here). It has never been loaded
  by Home Assistant; before relying on it import it, create one automation with all four inputs empty and one with
  each input filled, and check the trace and the retained `radar_zones/context/*` topics once. Whether Home
  Assistant accepts a state trigger with an empty `entity_id` list is not verified.
- Deviations from the design that are deliberate, listed so that they are not mistaken for slips: (1) the entities
  'Review item text' (sensor) and the diagnostic binary sensor for 'Radar data valid' are not in design 6.3;
  (2) a zone made by 'Mark spot here' is a label (kind bed or manual), not an entry of `zones`, so the status
  attribute `review.zones` counts only places that were approved or renamed and stays 0 after a successful
  press (P6 must read labels and zones together); (3) the shell sends only Approve, Reject and Later, so `undo`
  ([No bed in view] back, or a denied night window) and `forget` cannot be sent from MQTT before the P5 'Advanced'
  part (a user who pressed [No bed in view] by mistake types `Bed` as the spot name and presses 'Mark spot here');
  (4) 'zones, room occupancy ... unavailable while not observing' exists in P4 only as 'Radar data valid', the zone
  and room entities arrive with P6; (5) 'Mark spot here' with another item selected keeps the P3 name rule: the
  names bed and bett (whole name, trimmed, any case) still give kind bed, any other name gives kind manual. Design
  7.6 says 'any other press creates a zone of kind manual'; the P3 rule is kept because no review item exists
  before the setup review, the P3 replay marks the bed that way, and a user who types 'Bed' says what the spot is.
- Limits that cannot be removed in the learner: its own OFF cannot be told from the user's OFF by the value alone,
  so a user's OFF inside the 15 min watch after an unconfirmed start is put back, and a user's OFF after an ON
  that the learner never saw (the app was down) is left alone only because of the 24 h rule. Both are bounded and
  reported with the review event.
- The lux used by the replay is an ideal model derived from the truth; how the real BH1750 tracks sleep is the P0
  measurement and stays open. The learner's wake evidence is noisier than the truth (late wakes from a bed exit the
  radar did not see, early ones from a trip counted as the wake), which is why P4-1 holds at night 21 and reads
  differently at night 14 (see the P4-1 night-14 summary of the report).

## 0.3.0 - P3 shadow mode

- First version of the app: learns places from the LED matrix node's LD2454 frames stream
  (`led_matrix_node/radar/ld2454/frames`) and writes them as shadow suggestions only.
- Entities: 'Learner status' (with attributes), 'Learner debug map' (image), 'Mark spot here' (labels
  only) and 'Spot name'.
- No zones, no automations, no notifications and no commands to the node yet.
- State in `/data/state.json` (schema 3), saved every 30 minutes, after each nightly run, after each
  label or name, and on stop; 7 daily snapshots in `/data/snapshots`.
- Health port 8099 for the Supervisor watchdog; the app exits when its worker loop stalls for 120 s.

### Where the code differs from the P3 spec (the spec owner should update the sections named)

Learning rules:

- Co-visibility query (spec 7.7). A candidate's 0.4 m cells come from its own cells, not its dilated
  cells (rounding a dilated set to 0.4 m reaches up to 0.6 m past the candidate and took in the source's
  own fragments for a mirror image 0.9 m from the seat). As in the simulator (SIM:762-769), a fragment in
  a 0.4 m cell that both candidates cover belongs to the candidate: it counts in the denominator and is
  not a source cell. Measured offline on 31 captured replay runs (5 ghost seeds and 26 main jobs, 21
  nights): the spec's query sent 128 of 222 ghost candidate-nights to reflection, the own-cells rules
  222 of 222 (the simulator-literal one and the one first built here give the same); no real place went to
  reflection with either, and the highest co-visibility of a real place is about 0.4. Final form, replay:
  0 ghost places and 0 false places in the P3 profile, and in a held-out run with heavier ghosts
  (gs 0.4, gi 0.3, bedroom 00:00-06:00, seeds 1-3) every mirror and in-room ghost goes to reflection on
  nights 4-9 with 0 false places.
- Co-visibility accumulation (spec 7.7). A second of a fragment is credited at most once per partner
  cell (the simulator takes the union of the source's intervals); the pair matrix is no longer exactly
  symmetric, and `covis()` is clamped to 1. Before, two people in one source cell counted a candidate's
  second twice (values of 1.02-1.08 for a mirror of a two-seat sofa; a third person's chair next to a
  two-person sofa could be sent to reflection at 40% real co-occupancy).
- Unknown LD2412 time (spec 6.6, 7.5, D3). When less than half of a stay's span has a known LD2412 state
  (`pw` bit 1 = 0), the stay is kept with `ld` 0 and is no ghost evidence. Before it was an `ld_off` ghost
  with a GHOST splat at the person's real position, which raised the ghost share of real places until
  gate 3 failed. Ghost evidence stays a stay while the LD2412 is known to be absent. Bridging still needs
  90% known-present time.
- Dropout bridging hold (spec 7.5, undocumented in the spec before). A pending stay is held back from the
  15 min commit while a live track born inside its bridging window may still bridge to it (the simulator
  handles whole tracks in birth order). Only a track whose first sample lies within 1.1 m (bridge distance
  plus fragment radius) of the stay holds it, and a hold never outlives the nightly run: pending stays are
  committed right before it. Before, a static track anywhere in the room held every pending stay for as
  long as it sat.
- 'Mark spot here' does not count a run that already meets the identical-position ghost rule
  (sd < 1 cm for >= 5 min) as a stay in progress.

Input hardening (spec 6.2, 6.3, 6.4; the broker has no ACLs, so any client can publish a frames payload):

- Frames payloads: `t` <= 2^48, `n` <= 5,000 and `a0` <= 120 s, besides the P1 validator's rules.
- A boot id other than the epoch's opens a new epoch only after the epoch has been silent for 15 s (a
  real reboot) or the new id came in 3 batches in a row. A retired boot id is no longer ignored for good;
  it is treated like any foreign id (counters `stale_boot` and `foreign_boot`).
- Seq is compared modulo 2^32 (the uint32 wrap is followed). A batch that is a duplicate or jumps ahead
  by more than 1,000 frames plus 20 per second of receive time is rejected (counters `duplicates`,
  `bad_seq`); ten rejected batches that continue one another are the real stream, and the epoch resyncs
  (`seq_resyncs`, censoring event `seq_resync`).
- `recv - t` is the node's boot time on the host clock. A batch whose boot time is before 2020, or more
  than 10 s earlier than the window minimum (a device ahead of the host), is rejected (`bad_time`); three
  such batches in a row are a real step of the host clock (`clock_steps`, censoring event `clock_step`,
  the offset window restarts). A step forward ends the chain when the old samples leave the window. The
  frontier, the offset and the coverage can no longer be moved by one forged packet.
- The learner's day 0 (`created_date`) is not set before the host clock is past 2020-01-01.
- Payload sizes are limited in the paho thread before anything is copied or queued: 4,096 bytes for
  frames, 256 bytes for every other topic (counter `payload_oversize`).
- State load refuses impossible numbers (times outside 2020-2100, positions beyond 33 m, negative
  durations, unknown kinds); before, a time of 2^63 loaded and then raised in every nightly run.
- A nightly run that raises no longer aborts the event that triggered it or repeats every minute: one
  `NightlyFailed` output (the log names the error type and place, never a value), attribute
  `nightly_errors`, and the next night tries again. The nightly decay hours are capped by the calendar
  time since the last run.

Resources:

- A fragment buffer longer than 512 samples is folded into running sums; a static ghost that lives for
  days no longer grows the memory (it was 144 bytes a second per track). Fragments equal the simulator's
  loop (tests with the threshold lowered).
- 'Mark spot here': a second accepted press within 10 s answers `too_soon`; a press at a spot with a
  label of the same name within 0.4 m adds nothing; at most 100 labels (the oldest manual one goes).
  The same spot name again is no change: no echo and no write.
- Saves for a label or name wait at least 10 s after the previous one (written together later); nightly,
  periodic and final saves are never delayed.
- Stays, door events and ghost events are capped at 20,000, 5,000 and 5,000 entries (the newest stay);
  stale suggestions are still never deleted unless there are more than 200; at most 3 quarantined
  `state.json.corrupt-*` files are kept.

Home Assistant side:

- The debug map is published a second time 10 s after every discovery burst, because Home Assistant
  subscribes to the (not retained) image topic only after it has created the entity.
- The Home Assistant birth message is read from `homeassistant/status`, the default of its own setting,
  not from `<discovery_prefix>/status`.
- After 5 minutes of refused MQTT connections the app exits with code 72 and the Supervisor's watchdog
  restarts it with fresh credentials (the final state is saved).
- `run.sh` no longer passes bashio's text "null" on as a password or user name.
