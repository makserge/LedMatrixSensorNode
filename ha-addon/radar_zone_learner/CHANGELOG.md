# Changelog

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
