# Changelog

## 0.5.0 - P5 setup review and suggestion UI

The review of the learner's suggestions moves from the learner's own attributes to the review device in Home Assistant:
a place is looked at on a map, approved, renamed or rejected, and the answers are remembered. Still **shadow mode**: the
app creates confirmed zone records (name, kind, outline) in its state, but there is no zone entity, no occupancy and no
event yet (P6). Nothing changes in the firmware and the add-on has no new option (`config.yaml` gains only the new
version, the description and `backup_exclude`). The app version is 0.5.0; an installed 0.4.0 offers the update and keeps
its state (schema 4 stays: every P5 datum is an optional section or an optional key, and 0.4.0 can still read a P5 file,
it only drops the P5 additions at its next save). Design: option A, section P5 (OA 6.3, 7.4, 7.6-7.9, 10, 12).

The core (`core/p5state.py`, `zones.py`, `zonework.py`, `rejections.py`, `advanced.py`, `notify.py`, `where.py`,
`anchor.py` and the changes in `review.py`, `learner.py`, `state.py`, `extraction.py`, `behaviours.py`, `observation.py`)
and the shell were written to the P5 specification; the readings of the specification that each side had to make are
listed under 'Interpretations and open points'. The learning itself is unchanged: with the P5 additions left alone, the
learner's suggestions, candidates, labels and counters are identical to 0.4.0 in every replay that was compared (see
'Verification').

### New

Review device (shell: `shell/p5.py`, `select_model.py`, `review_p5.py`, `reviewmap.py`, `topics_p5.py`, `discovery_p5.py`):

- The 'Review item' select is dynamic: `No item`, `Night window`, `Bed zone`, then one option per open place, merge or
  modify item (`Place A`, `Extend B`, `Moved C`; the letters are persistent tags, so an option keeps its meaning while the
  item exists). At most 15 options. A changed option list is announced by publishing the select's discovery config again,
  and the state is sent again 2 s later and in the 10 s second pass (Home Assistant applies a changed list in a task and
  drops a state for an option it does not know yet). `SELECT_MODE = "slots"` in `select_model.py` is the built and tested
  fallback with fixed options (`Place 1` .. `Place 12`) if a real installation does not apply the changed list (acceptance
  A3).
- 'Advanced item' (select, at most 61 options), the buttons 'Apply advanced' and 'Rename', and the Advanced entries: undo
  of a rejection, of a denied night window and of [No bed in view], 'real place' for a reflection, rename and remove of a
  zone or a 'Mark spot here' mark. They are never counted, never pushed and never auto-selected.
- Answers: Approve, Rename (the name text, or a `name` in the message), Reject and Later for the selected item, with the
  item and its revision filled in by the shell. A stale revision, an unknown item, a repeated answer id or a refusal by the
  core is never silent: the review event `answer_ignored` carries `reason`, `op` and `item`. The answer topic takes up to
  512 bytes (a 32-character name with escaped non-ASCII characters can exceed 256) and the keys `name` and `aid`; every
  other command topic keeps 256.
- 'Review map' (image): the selected item outlined, confirmed zones, the other open items, the radar. 'Review push map':
  the head item of the last push request. Both are 280 x 160 px PNG, about 2.5-3 KB, not retained, and follow the option
  `debug_map`. The palette has 32 colours (the 27 of P4 do not move).
- 'Show me live' (switch) and 'You are in' (sensor): a map frame and the text every 2 s for 5 minutes, then it switches
  itself off. The sensor says `Live view off` otherwise; it never carries a coordinate.
- 'Night window start (edit)' and 'Night window end (edit)' (two `time` entities) and 'Approve also switches schedule ON'
  (switch, OFF by default, ticks once).
- 'Review push request' (diagnostic sensor, retained): one state of eight `|`-separated fields,
  `<seq>|<acked>|<kind>|<count>|<rev>|<item>|<acks>|<text>`.
- 'Review pending' attributes gain `tier`, `label` and `more`; the review event gains `answer_ignored`,
  `forget_refused` and `forgotten`.

Learner device (privacy controls):

- 'Forget all learned data' (button) with 'Forget all confirmation' (text): the button does nothing unless the text was
  set to `FORGET` within the last 60 s; every press and the 60 s clear the text. It deletes the state, its snapshots and
  quarantined copies and the raw-frame capture, makes a fresh learner (the node's window, the four settings and the push
  counters are kept), publishes every retained topic again in its empty version and raises the review event `forgotten`.
  Refused while Night learning runs or is still putting the node's night schedule back (the learner's own OFF is not confirmed
  ON yet: the reset would drop that bookkeeping), and while an approved night window waits for the node's echo.
  `rf_stats.json` and `options.json` stay.
- 'Forget rejections' and 'Re-suggest night window' (buttons); 'Guests present - pause learning', 'Pets in home' and
  'Detailed review text' (switches, persisted in the state, OFF by default).
- 'Debug capture' (switch, shell only, OFF at every start): the frames topic as one JSON line per message in
  `/data/capture/capture-<UTC YYYYMMDD-HH>.jsonl` (the second input format of `replay/capture_replay.py`), off after 48 h,
  files deleted after 72 h, at most 100 MiB, deleted by Forget all, excluded from backups (`backup_exclude: capture/*`, a pattern relative to the app's data folder:
  an absolute `/data/capture/**` would not match the full path the Supervisor tests, so it would exclude nothing).

Push notifications (`blueprints/radar_zone_review_push.yaml`): one automation per phone turns the retained push request
into one generic notification ('Radar setup review is ready (N items)', then at most one a day) with the map of the head
item as image and the actions Approve, Rename (with a text prompt) and Reject, and sends the taps back with the item, its
revision and the context id. The app owns 'sent once' and 'one a day' in its state; the blueprint acknowledges per phone
(`<seq>:<phone>`), so a restart of Home Assistant or of the app neither loses a request nor shows it twice, and one
phone's acknowledgement never hides the request from another. A request nobody acknowledged is issued again the next day.

Core (the specification, section numbers of the P5 spec): confirmed zone records from approval with stable ids (4);
rejection memory by overlap coefficient with the re-ask rule (5); tiers and tags (2); the night-window edit and the
switch-on box (3.4); the push planner (6.2); the guests pause judged at the time of the evidence (7.3); the pet kind
(7.4); merge and modify answer paths (3.6, 4.7).

Plumbing: 30 subscriptions (the 12 new ones are command topics, so a retained message on one is ignored and deleted), 35
discovery messages per burst (31 from the layer, 4 from the service), `P5Layer(P4Layer)` is the one layer object that
`main` passes as the service's `p4=` (one node link, one RF counter, one event buffer), `StateStore.wipe()`, the service
hooks `bind`, `take_forget_request`, `second_pass` and `forget_all` (looked up with `getattr`, so a plain `P4Layer` and the
P3 app still work).

Tests: `test_p5_topics`, `test_p5_discovery`, `test_p5_select`, `test_p5_review_text`, `test_p5_layer`, `test_p5_live`,
`test_p5_render`, `test_p5_service`, `test_p5_push_flow`, `test_p5_blueprint`, `test_p5_persistence`, `test_p5_capture`,
`test_fake_ha` and the helpers `tests/fake_ha.py` (a Python port of the blueprint: the broker's retained messages, entities
that exist only after their discovery config, a restart) and `tests/p5_harness.py`.

### Changed

- The P4 option `None` of the review select is now `No item`; Home Assistant turns a select state `None` into unknown. The
  command parser still accepts `None`. 'Review item text' says `No item` instead of `None` when nothing is selected.
- `p4_discovery_messages(topics, select_options=None, event_types=None)`: with the defaults the bytes are P4's.
- The review answer is parsed by `parse_answer_p5` (all seven ops, item ids of tier 2 and Advanced entry ids, `name`, `aid`).
- README: new section 10; the P3 and P4 sentences that said 'arrives in P5' are rewritten.
- The log line and the command line description no longer say 'P3 shadow mode'.

### Fixes after the P5 review

- **[Later] on the bed item is not a daily reminder (D41).** The bed item's revision moves twice with a [Later] (at the press
  and at the end of the snooze, which P5-5d needs), so it used to come back as a never-announced `bed_needs_data#N` and the
  next morning's push was made. In `core/notify.py` the bed item now always counts as revision 0 in the announced set, and a
  bed item that was snoozed before the first push counts as announced, the same rule the place, merge and modify items
  already had. Tests: `test_p5_notify` (two), `test_p5_e2e` (the real planner with FakeHa).
- **Forget all is refused while Night learning is still putting the node's schedule back ON**, not only while its switch is
  on: the user ending it early, or the 7th night ending, only starts the restore, which can wait for days for an offline node,
  and a reset in that time dropped the bookkeeping (`set_by_learner`) and left the node's night schedule OFF with nobody
  responsible. The shell now mirrors `set_by_learner is False` (`nl_restoring`) from the status and from the core output
  (`shell/p4.py`, the guard in `shell/p5.py`); the refusal reason stays `night_learning_active`. Tests: `test_p5_layer` (four,
  one with the real core).
- **`backup_exclude` is `capture/*`.** The Supervisor tests each entry with `PurePath.match` against the file's full path, so
  the absolute `/data/capture/**` matched nothing and the raw-frame capture (positions) would have been in every backup. A
  relative pattern matches from the right: it matches the capture files and not `state.json` (`test_manifest`).
- **The push map is geometry only.** It is the review map of the head item without the dwell heat and without the outlines of
  failed candidates: the phone fetches the image from Home Assistant and it may leave the home network, and the heat shows
  where the household spends its time. Tests: `test_p5_layer`.
- **Discovery payloads are UTF-8, not JSON-escaped**: a name with an umlaut or a CJK character costs 2 or 3 bytes in the
  Advanced or review select, not 6; an ASCII payload is byte-identical (`test_p5_discovery`). The 4,096 byte bound is the
  add-on's own and holds for ASCII names; the limit that matters is Home Assistant's 16 KiB for an entity's attributes.
- **Replay, P5-4:** the job `p5setup` scripts one person awake in bed (a scripted extra at the bed, 22:00-23:58 of days 6 to
  9, the mechanism of the two people in bed of `p4mark`): OA 7.6 has the user press 'Mark spot here' awake in bed, which the
  simulator's sleeper (visible 5 s in 600 s) cannot do. That is a deviation from spec 11.6, which has `p5setup` without
  extras; the limit of 8 taps is not moved. Without the extra person the check read 49, 49, 48, 48 and 49 taps. P5-6 reads the
  rev change and the stale entries only up to the night of the undo (afterwards the suggestion is an ordinary one).
- **Documented, not changed:** the push acknowledgement means that Home Assistant accepted the notify call, not that the phone
  received it (a failed remote delivery is only logged by Home Assistant), and the retry rule covers a notify call that raises
  and a blueprint installed late; it has no upper limit (README 10.4). The acceptance steps A2, A3 and A7 are widened (the
  push map image after a restart, the Advanced select, the phone away from home).
- Test support: `FakeHa(image_delay_s=...)` (an image entity that exists only after its platform is up) with two cases (a
  delay below the 10 s second pass gets the image, a longer one gives a notification without it and loses nothing else), and
  `tests/test_p5_e2e.py` (the real `Learner`, the real service and `P5Layer`, two phones).

### Interpretations and open points (for the integration)

The numbers are the decisions of the P5 specification (section 13.1); the shell's own readings come after them.

- D8 `No item` (above). D11 the name text is consumed by an applied approve or rename. D12 names are unique among zone
  records and marks (` 2`, ` 3`). D13 'Mark spot here' still makes a label, not a zone record; the read view
  `Learner.zones()` lists both for P6. D14 the sources of merge and modify items are test scaffolding behind
  `P5Params.zone_change_items` (default off); the answer paths, texts and maps are built and tested, P7 brings the real
  detectors. D27 'You are in' moves only while 'Show me live' runs. D28 a press of 'Mark spot here' with another item
  selected keeps the P4 name rule (a spot named `bed` or `bett` is still a bed). D29 the head of a push is the first place
  item; the night-window and bed items are never answered from a push. D30 the push has its own image, fixed per request.
  D41 a [Later] item that returns is listed again but never announced by a second push. D42 the push acknowledgement is per
  phone. D43 the layer is seeded from the learner before the first publish, so a restart never publishes the empty defaults
  over the retained push state.
- The time anchor of the design (a place's typical hours) is shown only with 'Detailed review text'; the push stays
  generic either way (owner question 1 of the spec: default off). The auto-names are English (`Place N`, `Bed`).
- Shell readings of the spec: (1) `P5Layer` takes the add-on option `debug_map` as a keyword (the specified signature has no
  other way to know it) and a `clock_ms` for the capture's file names. (2) The review map is published at most once a
  second outside the live view; when a change comes within a second of the last map, the states go out at once and the map
  follows in the next tick, so 'the map before the select state' holds for every change that is not within a second of the
  previous one. (3) The text of the head item in the push state (with the opt-in) is the text of the head as it was when the
  request was made, so it does not change when the item is answered meanwhile (the head is fixed per request). (4) `undo`
  without an item is the review event `answer_ignored` / `no_item`; every other missing or closed item is `no_such_item`.
  (5) The shell stamps the revision of the select's mirror on every answer it forwards, except for the Advanced entries
  (whose revision is the zone record's): they carry what the sender gave. (6) A `name` that is empty after stripping, or has
  a control character, makes the whole answer refused (spec 3.1). (7) The Advanced label of a zone or mark without a name
  is `Marked spot`. (8) `StateStore.wipe()` also deletes a leftover temporary snapshot (`state-*.json.tmp`). (9) Forget all
  gives the cached inputs to the new learner with the time of now, not their old time, so that day 0 of the new learner is
  the day of the reset. (10) After a reset `Service` swaps the learner before it saves, so the file written is the fresh
  state; the specification lists the save before the swap. (11) Two guards the specification does not list, so that a
  persistent fault cannot reach the limit of 100 errors in 10 minutes (exit 71) through the new timers: a render or encode
  error of a map is logged by its class (never its message), counted and not retried until the next change, and a core
  error while a map is made is not retried every second; a live view whose frames get no answer from the core three times
  in a row ends itself with `Live view off`.
- Core readings of the spec: (1) The core emits `PushRequest` only once a request exists (`seq` > 0). The literal text of
  rule 4 would also emit `PushRequest(0, 0, 'none', n, ...)` whenever the number of open items changes before the first
  request; that would break the pinned output order of the P4 tests and the rule that a learner without P5 data emits exactly
  what 0.4.0 emitted. The shell seeds from `Learner.push_request()`, which always returns the current request, so nothing is
  lost. (2) A suggestion also counts as rejected when the centre cell of a stored rejection mask lies inside its cells (the
  wording of check P5-6 (2), a superset of the overlap rule of spec 5.2); gate 7 and the dedupe pool use the overlap rule
  only. (3) The tags of reflection suggestions (needed for the Advanced label 'Real place: Reflection <tag>') are allocated
  when the Advanced entries are built, so a learner that is ready and has a shadow reflection exports a `p5` section with
  tags only. (4) 'Forget rejections' with nothing to clear stays silent as in P4 (no `answer_ignored`); it also clears
  `p5.masks`. A change of the Advanced entries forces a status update so that `review.advanced` stays current. (5) Pets
  (spec 7.4): the day count is the number of distinct days with home-empty ghost events in the region and the share is over
  all ghost events of that region; the kind is evaluated again at every nightly run, so it goes back to `ignore` when the
  switch is turned off or the rule stops holding. (6) `ZoneView.night` of a mark is derived from its creation time; the
  `p5.settings` section drops setting names it does not know at the next save (the four known ones are always written),
  other unknown keys inside `p5` are kept; a malformed `NightTimeEdit` (bad field or minute) is ignored without an event,
  and only a valid edit with no proposed night-window item gives `answer_ignored` / `no_such_item`.
- Open point: if no blueprint ever acknowledges a request, the planner issues a new request every local day between 08:00
  and 21:00 for as long as items stay open (the retry rule of spec 6.2 has no upper bound). It costs one retained state a day;
  a cap is left to the owner.
- The YAML and Jinja of the blueprint were read by a small subset reader and evaluator written for this check (scratch,
  not part of the app) and its decisions match the Python port; neither is Home Assistant.

### Not built

- `retire_zone` and its 'Keep' answer (D15, P7); 'a second reject is permanent' (D16, only in the earlier design).
- Zone entities, occupancy, counts, events and clear delays (P6); coverage-normalised drift, the move check with succession
  and the radar-moved check (P7).
- Debug capture of anything but the frames topic.
- Home Assistant API access or `notify.*` from the app (the manifest has no `homeassistant_api`).

### Verification (development PC, simulated days)

- Unit tests: `python -I -B -m unittest discover -s tests -t .` runs 1,498 tests, all OK, 2 skipped as before (640 in 0.4.0;
  the 0.4.0 tests are unchanged apart from the mechanical edits of spec 12.4: the version strings, the pinned lists).
- `--profile p5` (33 runs, 549.8 simulated days, 17 minutes on 2 workers) on the tree with the review fixes: the gate is
  **PASS**. P5-1 (stale and current revisions, re-delivery, 9 steps in 3 of 3 seeds), P5-2 (learner-side restarts: one setup
  request, `seq` 1, the interrupted request is emitted again with the same `seq`; 15 and 10 requests a run), P5-3 (303
  requests checked), P5-4 (below), P5-5 (5a to 5h), P5-6 (masks of 13 and 14 cells, 11 nights checked, a place back on night
  19), P5-7 and P5-8 (guests pause: 19 and 21 paused stays, none near the guest spot while paused, 9 and 11 stays there in the
  control run) PASS, and so do the three P4 re-runs. The pets run is reported only: no `pet` suggestion appears (the dog sofa
  lies on the human sofa), and the merge and modify sources made no item in either seed.
- P5-4 (first setup in at most 8 taps) PASS in 5 of 5 seeds with 6, 6, 5, 5 and 6 taps (4 places each, one 'Mark spot here'
  press each, which makes a bed label), **after the job `p5setup` was given a person awake in bed** (see 'Fixes after the P5
  review'). Before that it FAILED in all 5 seeds with 49, 49, 48, 48 and 49 taps, of which 40 were failed presses (the
  scripted user gives up after 40): the simulated sleeper is visible for about 5 s in every 600 s, so a stay of 20 s is hardly
  ever in progress, and the world had no session of a person awake in bed, which the design assumes (OA 7.6). It was not the
  review flow: without the failed presses the setup took 8 or 9 taps. The PASS therefore measures the review flow with a user
  who presses while awake in bed; it says nothing about how often a real sleeper's press finds a stay (spec 10.2: with a
  user who is not in bed the bed item costs one [Later] tap and the press follows another day).
- P5-R1: the P3 jobs `bed_w0000_0600` and `liv_w0000_0600` (14 nights, seeds 1-3) and `bed_w0000_0600_p4` (21 nights, seed 1)
  give identical suggestions, candidates, labels, counters and final state on 0.4.0 and on this tree (7 of 7 runs). P5-R2:
  a 0.4.0 state loads in this tree (three idle days and a rename without error), and a P5 state loads in 0.4.0 with
  nothing lost but the P5 additions. P5-R3 on a synthetic full state (376,832 bytes): a 1 s tick 0.159 ms median and 0.322
  ms at most, a cached item list 0.06 ms, a live frame 3.3 ms on average, a map rebuild 17 ms, export and save 37 ms, 0.36 MB
  of memory. P5-R1 to R3 were run again after the review fixes (R1: 7 of 7 identical to 0.4.0).
- The earlier gates, `--profile p3` (G1-G6, 67 runs, 1,332.8 simulated days) and `--profile p4` (P4-1 to P4-7, 47 runs, 706.9
  simulated days), PASS on this tree (run before the review fixes, which touch neither the learning nor the P3 and P4 jobs;
  P5-R1 above was repeated after them), and they were run on the 0.4.0 tree as well and compared: all 67 runs of the P3 profile
  are identical in every field but the timing; the 47 runs of the P4 profile are identical in suggestions, candidates,
  labels, counters, status and in every number of the criteria. What differs is only what P5 changes on purpose: the review
  list of a night now also holds the place items (tier 2) and, in 6 nights, the selection; and in `p4names` a second zone with
  the same name is stored as `BED 2` (D12). The replay costs more CPU per simulated day on the PC (2.50 to 3.00 s in the P3
  profile, 2.89 to 3.58 s in the P4 profile), mainly the push planner and the review check that run for every event; the
  projected load on the CM4 goes from 0.029% to 0.035%.

### Not verified (acceptance on the real installation, spec 11.9)

No Home Assistant, broker, phone, YAML parser or Jinja engine exists on the development PC. To check once: that the
blueprint loads and its traces end without error (A1); that the state trigger fires for the first state after a restart of
Home Assistant, with the push map image (it is sent in the burst and again 10 s later) (A2); that Home Assistant applies a
changed `options` list in place, for the review select and the Advanced select, also after two changes within a second and
when it is busy (A3); the notification on the phone in use, the text prompt of Rename, the image through `/api/image_proxy`
(the phone loads it itself, so the Home Assistant address must be reachable from where the phone is), Local Push (A4, A5, A7); a repeated tap and a repeated push (A6); 'Show me live' for 5 minutes and the CPU cost of a frame on the CM4
(A8, A10); that `image.radar_*` can be excluded from the recorder (A11); the time entities and the switch-on box (A12);
Forget all on a copy of the data (A13); a backup with and without capture files, opened to look inside (A14, A15: that `backup_exclude: capture/*` matches); the retry rule and two phones
(A16, A17).

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
