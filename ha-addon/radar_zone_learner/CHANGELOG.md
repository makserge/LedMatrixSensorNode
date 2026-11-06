# Changelog

## 0.7.0 - P7 drift, moves, retire, radar moved, tuning

The learner now notices that the room changed and **asks you** about it: a piece of furniture that was moved (a *Moved* item
that keeps the zone's id, name, entities and automations), a place that nobody has used for 21 covered days (a *Retire* item
with a 90-day *Keep*), and a radar that was moved or turned (a *Radar moved* item that stays until you answer it, a generic
push 'Radar needs attention', and a reset of the learned maps that keeps your places as 'needs re-check'). A zone is **frozen**
(never flagged, moved or retired) when its usual hours were mostly not observed, which is what keeps the bed safe under the
00:00 to 06:00 schedule. The merge detector of the design is live. A one-button, counts-only **tuning export** lets you
propose threshold changes for a release; nothing is tuned at runtime. The learner still only recommends: every change is a
review item that needs an answer, and the app still switches nothing in your home and sends no node command. The P7 data
lives in one optional key `zone7` inside the `p5` section of the state (schema 4 stays; 0.6.0 and 0.5.0 keep the key and write
it back). The firmware is not changed. The app version is 0.7.0; `config.yaml` gains the new version, the description and a
second `backup_exclude` entry (`tuning/*`), no option. Design: option A, section P7 (`CD:phases/P7`).

The core (`core/drift.py`, `moves.py`, `movedfrom.py`, `radarmoved.py`, `tuningagg.py`, `p7state.py` and the changes in
`types.py`, `params.py`, `zones.py`, `review.py`, `notify.py`, `extraction.py`, `where.py`, `learner.py`), the shell and the
replay (`replay/p7_scenarios.py`, `evaluate_p7.py`, `tune.py` and the plumbing) were written to the P7 specification in
parallel by three owners. This block is the shell owner's; the core and replay owners add what only they can report (see
'Verification'). **`P7Params.enabled` is False in the dataclass**: every existing test and every non-P7 replay job builds a
learner that behaves exactly like 0.6.0; the production learner factory in `shell/service.py` passes `enabled=True`.

### New

Review items (shell: `shell/review_p5.py` texts, `select_model.py` labels; core: `review.py`, `moves.py`):

- `mod-<zone>` ('Moved A'): Approve updates the zone in place (**id, name, kind, entities and automations stay**, only the
  attribute `poly_cm` changes), Rename makes a new zone, Reject rejects the new spot as a place. The detector (`CD:pipeline/§7`):
  a new place 0.5 to 4 m from a falling zone, hours correlated at r >= 0.6, and, beyond 1.5 m, at most 25 % of its dwell before
  the zone's last use; a shift under 0.5 m is found through the zone's own suggestion.
- `mrg-<place>` ('Extend A'): the merge detector (touching within 0.4 m or IoU >= 0.2, r >= 0.6, usage not falling) replaces the P5
  scaffolding in production; a moved place wins over an extension.
- `ret-<zone>` ('Retire A', new): 21 covered days with a ratio under 0.1 and the usual hours observed at least half the time;
  Approve removes the zone and its entities, Reject is **Keep** for 90 days. Never the item a push offers.
- `radar_moved` ('Radar moved', new, tier 1 after the night window and the bed): two consecutive moved nights, **latched until
  answered**; Approve resets the maps in place (the zones and marks stay, every one 'needs re-check' for 28 days and is
  re-anchored by a Moved item), Reject is Ignore (quiet for 14 days).
- Answers are refused with the new review-event reason `zone_busy` while the zone is occupied (Approve of Moved and Retire only);
  the order on the P7 path is Radar moved, Moved, places, Extend, Retire. Texts carry **no zone name, hour or date** unless
  'Detailed review text' is on; the P5-path items keep their P5 texts (a merge or modify item without the data key `src`).
- The push request can have the kind `attention` (the core keeps `daily` in its own state, so a 0.6.0 app still loads it; the
  shell writes the word): the blueprint accepts it and says 'Radar needs attention' (no name, time or distance, no actions).

Drift and freezing (core): a coverage-normalised ratio of the 3 over the 14 covered dates (decay-weighted), summed over the
hours observed at least half the time in both windows; a zone whose usual hours are under half observed is frozen
(`ZoneView.frozen` is the P5 static predicate OR this coverage rule). Moved-from and retired masks keep the old spot from coming
back as a new place until it qualifies again on later stays alone. A probation rule keeps a one-week guest's seat from being
offered (a new place after night 14 needs 10 covered dates of evidence).

Tuning (shell: `shell/tuningexport.py`, `p7.py`, `topics_p7.py`, `discovery_p7.py`; core: `tuningagg.py`): one line of counts
and fixed-length histograms a night in `/data/tuning/nights.jsonl` (the newest 28, local, excluded from backups, deleted by
Forget all); `button.radar_zones_learner_export_tuning` publishes **one pooled record** (sums, no per-night list, no date) once
to `radar_zones/learner/tuning/export`, **not retained**, no file written; `sensor.radar_zones_learner_tuning` says `none` or
`<n> nights`. The record is scanned for privacy and size before it is published. `replay/tune.py --aggregate <file>` turns it
into proposals on a PC. HA[U8] (the 'Listen to a topic' tool delivers the message) is an assumption (acceptance A7).

Plumbing: 35 subscriptions (the one new topic is a command, so a retained message on it is ignored and deleted), 2 discovery
messages, `P7Layer(P6Layer)` is the one layer object that `main` passes as the service's `p4=` (no new service hook: Forget
all deletes the tuning folder through the existing `purge_capture` step), `main` builds `build_topics_p7`, new status
attributes (counts only) in core (`zones_frozen`, `zones_blind`, `zones_falling`, `zones_unused`, `zones_recheck`,
`moved_from`, `radar_moved`, `places_probation`, `places_recheck_held`; `zone7_pruned`, `zone7_reset` and `p7_errors` are
`Learner.counters()` only, read by the replay, and not status attributes) and shell
(`tuning_exports`, `tuning_export_refused`, `tuning_write_errors`, `p7_shell_errors`).

Tests (shell, seven modules): `test_p7s_texts`, `test_p7s_discovery`, `test_p7s_topics`, `test_p7s_tuningexport`,
`test_p7s_blueprint`, `test_p7s_entities`, `test_p7s_service` and the helper `tests/p7s_harness.py` (the P7 layer with the
fake core and a fake Home Assistant that also models the 'Listen to a topic' tool); the `FakeLearner` of `tests/fakes.py`
gained the P7 accessors and a clean tuning line.

### Changed

- `main` builds `build_topics_p7` and one `P7Layer`; `P5Layer`, `P6Layer` and their topics are unchanged and still tested on
  their own.
- `config.yaml`: version, description, and `backup_exclude` gains `tuning/*` next to `capture/*` (it matches the folder's files
  and not `state.json`, the snapshots or the options).
- `blueprints/radar_zone_review_push.yaml`: the two kind checks accept `attention` and the generic message for it is 'Radar
  needs attention'. **Copy the blueprint again after the update**; an older copy ignores that kind. It has never been loaded by
  Home Assistant (acceptance A6, assumption HA[U9]).
- `README.md`: a new section 12; the P5 sentences that said merge and modify items are not made in production are in the
  present tense; the 'Reserved for later: the `retire_zone` item' line is gone; the select options row gains `Retire A` and
  `Radar moved`; the privacy summary lists the usual-hours state and the tuning data.
- `THIRD_PARTY_NOTICES.md`: P7 uses no new third-party code or idea (stated there).
- Existing tests, only the mechanical edits of the P7 specification (12.4): the version strings of the shell and manifest tests,
  the manifest test (description line, the two `backup_exclude` entries and what each matches, the two changelog headings), the
  `SHELL_MODULES` list of the lazy-import test, the regex of the blueprint test (the three kinds), and the select test (two
  added labels). Nothing was loosened.

### Privacy changes (read before you update)

`state.json` (in every backup) gains, per zone, a 24-value **usual-hours profile**, the dates a zone was last used and of a Keep
answer, the position of a moved or removed place (the moved-from mask) and counters of your answers: the same class as the
stay list, coarser, and **never published**. The tuning file `/data/tuning/nights.jsonl` is a day-by-day series of counts (a
night with no stays reads as 'nobody home'): it has no date, position, name, id or hour, it is **not in backups**, nothing
publishes it, and Forget all deletes it. The only thing that leaves the add-on is the pooled export, **when you press the
button**, once, to a topic that is not retained; Home Assistant and every add-on with MQTT access can read a message on it
(`CD:privacy` 'Honest correction 3'). Push texts stay generic: a count, 'Radar zone review' or 'Radar needs attention', an item
id with a revision in an action id; no name, time or distance.

### Interpretations and open points (for the integration)

The numbers are the decisions of the P7 specification (section 13.1); the owner questions Q1-Q19 are not answered yet and the
spec's default applies to each. The interpretations (a `PROPOSAL` is the spec's value where `CD` is silent):

- **Drift (D6, D7, D9, D18, D24, D25).** The 3-day and 14-day series are derived every night from the stay list and the
  coverage table as decay-weighted windows over covered dates (half-lives 3 and 14), not stored bins; per zone only the usual
  profile `u`, two run counters, a few dates and revision counters are persisted. A `collapsed` zone (nothing seen in the whole
  slow window) has ratio 0; the retire run resets at a ratio of 0.1 or more (consecutive, as `CD`; 0.25 is the first draft's
  band) and the flag run at 0.25. `u` is refreshed only while the zone is `ok`. Numbers are computed for marks and beds, items
  only for zone records that are not beds. **Q3, Q2** beds and marks are never moved, merged or retired.
- **Moves (D15, D19-D21, D33, D36, D46, DV9).** The modify detector runs at the review layer (`moves.plan`) and consumes the
  suggestion, so extraction and every suggestion id and cell are as in 0.6.0 until the first answer; the answer swaps the zone's
  `ref` to the new suggestion and keeps the old one in `also`; the modify revision is a per-zone counter that is never reset;
  for a candidate-source proposal Rename and Reject never touch the old zone. A falling zone needs 4 stays in the slow window.
- **Masks and forgetting (D11, D45, DV5, DV13).** Moved-from and retired masks live in `zone7.mf` (the P4 suppression entry is
  pinned) and are judged in gate 7 on the candidate's own post-event stays. 'Forget rejections' also clears them, every Keep
  date, the radar Ignore mark and the P7 snoozes; the per-zone 'Forget learned data' restarts only that zone's unused counters.
- **Guests (D12, DV6, Q1).** The probation rule is a PROPOSAL with a real cost: a genuinely new seat is offered about 11 days
  later after night 14. It is applied to the item list, not to the gates, so learning is unchanged.
- **Radar moved (D8, D26, D34, D35, DV2, DV12, DV15, Q15, Q17).** Door events and the `identical` and `home_empty` clutter events
  (no outline map: the walk grid has no 3-day variant); a hit rate stands in for 'correlation'; the detector delivers about 0.6
  to 0.8 m and 7 to 10 degrees, not `CD`'s 0.3 m and 5 degrees. Latched until answered, held for at most 14 days, quiet for 14
  days after Ignore. Reset keeps the coverage, the night model, the settings, the night-window and bed answers, the zones and
  marks (Q5).
- **Tuning (D14, D28, D38, DV8, Q6, Q18).** An offline loop; the pooled export has no per-night list or date; labels are
  counted without a position (`lb`, `la`).
- **Push (D22, D5).** A retire item and the radar item are never a push head; the attention request is a flag on `PushRequest`
  and shares the one-a-day cap; snoozes of the new items live in `zone7.snz`, so a 0.6.0 app loads the state.
- **Core notes.** (1) `tests/test_p5_notify.py` gained the field `attention` in its pinned `PushRequest` field set (a mechanical
  consequence of Appendix A.2). (2) `core/state.py` needs no edit: the `zone7` sync is one line in `Learner.export_state`, the
  load runs in `Learner.__init__`. (3) A zone entry is created at every nightly for every record and label, `young` included.
  (4) The usual-profile refresh keeps the mass of the hours that were not observed and skips the night on which `u` was
  initialised and a date already counted. (5) The date is marked seen on every night, held or not. (6) The `rr`/`ro` and `mr`/`ms`
  bookkeeping is done in `moves.plan`; a snoozed retire item is filtered like a snoozed modify. (7) `ReviewModel._forget(L)` and
  `_item_exists(self, L, ...)` gained `L`. (8) `Learner.p7_radar_dbg` (the last radar verdict, for the replay report) is an extra RAM
  attribute. (9) `reset_summary()["grids_nonzero"]` counts the non-zero cells over the nine grids.
- **Shell readings of the spec.** (1) `tuning/last_night` (one number, the learner's night counter) lets a nightly that ran twice
  after a restart be written once; `nights.jsonl` holds no night number, and both files are under `tuning/*`. (2) The export press
  is a shell event answered by the layer; the core never sees it. (3) Forget all deletes the tuning folder in the existing
  `purge_capture` step, so the service has no new hook. (4) The slot-mode select, whose option list is fixed by P5, has no
  `Radar moved` option: an open radar item takes the next slot. (5) The title of every push stays 'Radar zone review'; an
  attention request has no head, so it has no actions. (6) A kept line that fails the core's privacy scan is dropped when the
  file is read, so a damaged or hand-edited file can never reach the pooled record. (7) The retire text names the 90 days from
  `P7Params.keep_days`.

### Changes after the first replay and review

These change rules of the specification. Each new constant is a PROPOSAL in `P7Params` and is listed here and in
`scratchpad/p7/fix_notes.md` of the development session. With `enabled=False` nothing changes (the P6 output is identical).

- **The own-suggestion modify (4.3.3a) needs a real move and two nights.** The replay showed a false `mod-z0001` item
  (source `own`, shift 9 to 17 cm) at nights 30 to 36 and 44 in the control, bed and move jobs, before anything had moved: the
  zone fell for another reason and its own suggestion only changed shape (IoU under 0.6). Now the suggestion's centre must be
  at least `own_min_m` = 0.3 m from the zone's, and the same (suggestion, revision) must have been seen on an earlier nightly
  run (RAM, `Learner.p7_own_seen`; a restart waits one more night). Until then the zone may still take a nearby candidate. The
  candidate source is untouched (the true moves of 1.1 m and 2.9 m are found by it).
- **The guest probation also counts dates (4.3.5).** A one-week guest plus a weekly visitor's stray stay a week before or after
  spans 10 or more covered dates (13 in the replay) although it lies on 8, so it was offered at nights 23 to 26 and 34
  (p7guest seeds 1, 2, 4, 5). Now the evidence must lie on at least `probation_min_dates` = 9 of the retained covered dates as
  well as span `probation_min_span` = 10 (0 switches either off). Cost: a seat used on fewer than 9 of 14 dates is not
  offered by this rule.
- **Succession needs a last use that is still in the retained dates (4.3.3 (4)).** The replay showed a false `mod-z0002`
  (p7ret seed 5, nights 28 to 30): a table nobody used any more was offered as moved to an armchair 3.25 m away that was used
  all along (r 0.86). The stays older than the 14 covered dates are gone, so with the zone's last use older than the oldest
  retained date the share of the candidate's dwell before it was 0 for any candidate. A candidate beyond `succ_far_m` (1.5 m)
  is now not taken in that case (not for an `rc` zone; candidates under 1.5 m do not use succession). Real moves are found
  4 to 8 nights after the move, with the last use inside the dates, so they are unchanged. Cost: a far move noticed more than
  14 covered dates after the last use shows as a plain place item and, later, a retire item.
- **`move_zone`:** the stored verdict of the moved zone is reset (`vs` `young`, `fz` false) so that no second modify is
  offered before the next nightly run; the old `ref` no longer joins `also` (after the mask lifts, the old spot is an ordinary
  place again); a zone that needs re-check records no moved-from mask (its cells are in the previous radar frame).
- **Radar moved:** a night counts only when its newest date brings points of its own (a sparse night used to count the same
  date twice); the matched points must cover at least `rm_min_clusters` = 2 old clusters (or all, when there are fewer); the
  same search on the newest reference date must not already find a large transform (noise); a counting streak is bounded by
  `rm_hold_days` like an open item (the hold ends and `st`, `d0` clear); `hold()` for a streak is bounded the same way. A reset
  keeps an existing `room_outline` suggestion alive (no second one two nights later) and clears the walk-point total.
  'Forget rejections' clears the quiet time too.
- **State:** a huge integer in `zone7` is a value problem (`StateError`, the key resets), not an `OverflowError`; a loaded
  mask list longer than `mf_max` is cut to the newest entries at load (P7 enabled).
- **Masks:** a moved-from entry is identified by zone, reason, day and cells (two answers on one day no longer release each
  other); gate 7 evaluates both the rejection and the moved-from term, so a re-qualifying entry is recorded and released even
  when a rejection also blocks.
- **Drift:** the eligible-hours test has a relative tolerance of 1e-9 (an hour observed exactly half the time is no longer in or
  out by rounding); the slow window keeps only covered dates inside `retention_max_days` (28), the days that still have stays.
- **Bookkeeping:** the retire open flag is lowered for every zone record that leaves the plan; `ZoneForget` touches the review
  (the withdrawn retire item leaves the cached list).
- **Documentation:** 35 topics (34 before P7); the select holds at most 16 options; `zone7_pruned`, `zone7_reset` and `p7_errors`
  are `Learner.counters()` only, not status attributes; no button resets the learned maps; a moved-from mask is made only by a
  `Moved` or `Retire` approval; the key `zone7` is absent only until the first nightly run; only the `Moved` text puts its action
  sentences first; the tuning keys `hs`, `hn` and `dl` come from the P6 spells and include time of 'Guests present - pause
  learning' (the pause stops learning, not the zone evaluator).
- **Tests:** `test_p7_fixes` (43), `test_p7_review_gaps` (32, from the review's verified candidates); five existing tests were
  adjusted with a comment (`key_of` carries the cells, the old `ref` is not in `also`, the own source needs a primed night).

### Deviations from the design (DV1-DV15 of the specification)

DV1 the series are derived and decayed, not stored bins; DV2 the radar-moved features are door and clutter events, no outline
map; DV3 `frozen` is the static predicate OR the coverage rule (a bed marked under night-off has no night hours in any profile);
DV4 empty-off is a replay stress case, not product behaviour; DV5 masks live in `zone7.mf`; DV6 the probation rule for guests
(`CD` states no mechanism); DV7 the three CD buttons map onto Approve, Rename, Reject plus Later, the P7 texts carry no zone
name by default; DV8 the tuning loop is offline; DV9 the modify detector runs at the review layer; DV10 the state key is a
sibling `zone7`, not inside `zone6`; DV11 consecutive retire counting; DV12 the detector finds 0.6 to 0.8 m and 7 to 10 degrees
(measured); DV13 'Forget rejections' clears the masks, Keep dates and snoozes too; DV14 the nightly budget is stated against
the measured P6 time; DV15 the radar item is latched; after the first replay and review: DV16 the own modify needs 0.3 m and two nights, DV17 the probation also counts 9 dates, DV18 the radar-moved verdict has a cluster guard, a noise check and a bounded streak.

### Not built

- HOLD mode (D1: no scan controller, one switch for both radars, zones `unavailable` while the radars are off, no wake source).
- Move, merge or retire of a 'Mark spot here' mark or of a bed (Q2, Q3); zone entities that go `unavailable` while a radar-moved
  item is open or zones are 'needs re-check' (Q4); an automatic runtime change of any threshold (Q6); a per-frame flicker
  counter and the margin proposal; a finer radar-moved detector (Q15); a per-date walk accumulator for an outline map; a room
  occupancy entity (P6 Q1).
- Writing to Home Assistant services from the app, and any firmware change.

### Verification (development PC)

- Shell unit tests (the seven `test_p7s_*` modules above: 40 + 13 + 14 + 34 + 13 + 15 + 24 = 153 tests), all OK, the cases with
  the real `Learner` behind the real layer included (a modify that keeps the entities, a retire that removes them, a nightly
  that writes one clean line, the export after a real night). The existing P3-P6 shell tests are unchanged apart from the
  mechanical edits listed under 'Changed'. The full unit-test count is filled in at integration.
- The replay checks of the P7 specification (P7-1 to P7-10 and the regression checks P7-R1 to P7-R3) belong to the core and
  replay owners and are not reported in this block.
- **Not verified:** everything Home Assistant does (HA[U8] the 'Listen to a topic' tool, HA[U9] the push kind `attention`,
  HA[U10] a change of `poly_cm` alone), the blueprint, the real door and clutter statistics that decide whether the radar-moved
  detector has features, the real distribution of the drift ratios and of rarely used seats, how often a real guest produces a
  candidate, the real effect of the 21-day and 90-day rules, the CM4 cost of the nightly step and the usefulness of the tuning
  proposals. They are the acceptance steps A1-A10 of the specification, section 11 (README section 12.9).

## 0.6.0 - P6 confirmed-zone evaluation

The learner now evaluates the live radar tracks against the zones you confirmed and tells Home Assistant: **zone occupancy,
the number of people, enter and leave events, learned clear delays, door fast-clear, early clear for zones the second radar
agrees with, time variants, tuning entities, and a blueprint that switches a light or a scene.** The learner still decides
nothing about the home: the app has no Home Assistant API access, so a light or a scene is switched only by the blueprint you
install. The zone data lives in one optional key `zone6` inside the `p5` section of the state (schema 4 stays; an installed
0.5.0 offers the update and keeps its state, and 0.5.0 can still read a 0.6.0 file: it keeps `zone6` and writes it back). The
firmware is not changed. The app version is 0.6.0; `config.yaml` gains only the new version and description, no option.
Design: option A, section P6 (`CD:phases/P6`, with the owner's decision of 2026-10-21 for the radar power states).

The core (`core/zoneeval.py`, `zoneinfo.py`, `p6state.py` and the changes in `types.py`, `params.py`, `tracker.py`,
`learner.py`) and the shell were written to the P6 specification in parallel, and the replay was extended by
its own owner. This block is the shell owner's; the core and replay owners add what only they can report (see
'Verification'). With no zone the learner emits exactly what 0.5.0 emitted, and the learning itself is untouched: the zone
evaluator reads the tracker and never writes to it.

### New

Zone entities (shell: `shell/p6.py`, `zonepub.py`, `topics_p6.py`, `discovery_p6.py`, `p6_types.py`). Every confirmed zone of
the kinds dwell, short_task, bed and manual (a record or a 'Mark spot here' label; at most 24 zones get entities, the rest are
evaluated and tuned but have none until a slot frees) is **one device** `radar_zone_<id>` with `via_device` the node:

- `binary_sensor.radar_spot_<id>` ('<zone> occupancy', `device_class: occupancy`): `ON` while a person is in the zone or the
  zone waits for its clear delay, `OFF` otherwise. Attributes, always all six: `kind`, `state_source`, `variant_now`,
  `typical_hours` (`wd 09-12,13-17; we 10-22`), `confidence` (the stored suggestion's, `null` for a label), `poly_cm`.
- A hold ends on the device time of the first radar batch past `last detection + delay` (at most one 5 s heartbeat late in an
  idle room); a tick only reclassifies a person who has not been seen for 2 s (count 0, held) and turns the zones valid 3.5 s
  after the radars resume. A return after a lost batch is no hidden spell, a lost batch never ends a hold, and a pending gap of 15 s
  or more is a power loss (zones unavailable, no event).
- `sensor.radar_spot_<id>_count` ('target count', diagnostic, **disabled by default**): 0 to 3, at most once a second with a
  trailing publish of the latest value.
- `event.radar_spot_<id>_event` ('enter/leave'): `{"event_type":"enter","track":"t1842"}` and `{"event_type":"leave","track":
  "t1842","dwell_s":5230,"via":"edge"}` (`via` is `edge`, `hold_expired`, `door_death` or `early`; `variant` is added when
  there is one). No zone name and no coordinate in a payload.
- Tuning (config entities that work while the radars are off): `number.radar_spot_<id>_clear_delay` (3-900 s),
  `number.radar_spot_<id>_margin` (-50..100 cm, step 5) and `button.radar_spot_<id>_forget` (discards the learned evidence
  and the tuning of that one zone, not the zone). The number states are retained and show the value in force.
- Time variants (`CD:suggestion_types`): the variant in force is the attribute `variant_now`; a zone that has variants also
  gets one request switch each, `switch.radar_spot_<id>_vreq_<variant>` ('<zone> - <variant> sensor', config, OFF), and only
  an ON switch creates `binary_sensor.radar_spot_<id>_v_<variant>`, ON while the zone is occupied and that variant is in force.
  The request is stored in the state (`zone6.ve`).

Zones are **unavailable while the radars are off, warming up or faulted, and while the node or the learner is dead**, as the
owner decided for option A (no PIR, so the design's held states do not exist). Every zone state entity has three availability
topics with `availability_mode: all`: `radar_zones/learner/status`, the node's `sensors/status` and a **new retained topic
`radar_zones/zone/availability`**, which is `online` only while the core's `ZoneStates.valid` and the shell's own radar
validity are both true. `radar_zones/radar/valid` (P4) is not used for zones: it ignores the core's resume state, so after an
add-on restart it would show `unavailable -> off -> on`. At a resume the real state of every zone is published **before**
`online`, so the history is `unavailable -> on` or `unavailable -> off`, never `unavailable -> off -> on` and never the
evening's stale `on`; no event comes from a power transition (the core's rule, checked end to end with the real core).

Blueprint `blueprints/radar_zone_spot_light.yaml` ('Radar spot occupied -> light/scene'): one automation per spot; a light, a
scene for occupied and a scene for clear, an off delay (60 s), an optional brightness. On at `to: on` (also straight from
unavailable, so a person at the spot at 06:00 switches the light on); off at `to: off` with `not_from: [unavailable,
unknown]` after the delay; an unavailable zone changes nothing. The header says what that means for the light: if the
blueprint switched it on and the person left while the radars were off, the morning `unavailable -> off` is ignored and the
light stays on until someone switches it (the optional input 'Also switch off when the zone returns unavailable -> off' changes
that and also fires at every morning power-up with nobody there; off by default).

Core (the specification, section numbers of the P6 spec): the zone area is the stored cell mask grown by the margin (25 cm);
a confirmed track enters after at least 3 of its last 10 detections inside, a detection at 0.8 m/s or more is not counted in a
dwell, bed or manual zone (a pass); one detection outside the area is a leave at once; a track that vanishes inside holds the
zone until `last detection + clear delay` (default by kind: bed 600 s, dwell and manual 300 s, short_task 60 s; learned
nightly as the nearest-rank p95 of the zone's own hidden spells, 5-600 s, from 10 spells; a tuning value wins); a moving death
inside a learned door clears after 3 s; a zone the second radar agrees with (LD2412-on share >= 0.8 over at least an hour of
known time) may clear early after 60 s of LD2412 absence and no track anywhere; POWER interruptions (power, silence of 15 s or
more, a reboot) suspend every zone with no event, a GAP keeps the zones and suspends the hold deadline for the unobserved
span (spec 5.1-5.8, 6).

Plumbing: 34 subscriptions (the 4 new ones are command patterns, so a retained message on one is ignored and deleted), 35
discovery messages per burst from the P5 app and 6 for each zone, `P6Layer(P5Layer)` is the one layer object that `main`
passes as the service's `p4=` (no new service hook), `Service` rate-limits the new save reason `zone` like `label`, and
`ZonePublisher` publishes only changes.

Tests: `test_p6_topics`, `test_p6_discovery`, `test_p6_layer`, `test_p6_lifecycle`, `test_p6_tuning`, `test_p6_recorder`,
`test_p6_blueprint`, `test_p6_service` and the helpers `tests/fake_ha_zone.py` (a generic fake Home Assistant for zone entities
whose behaviours are the assumptions HA[U1]-HA[U7], with a recorder model) and `tests/p6_harness.py` (the layer with the fake
core, and the real `Learner` behind the real service, frames over MQTT).

### Changed

- `main` builds `build_topics_p6` and one `P6Layer`; `P5Layer` and `TopicsP5` are unchanged and still tested on their own.
- The description line of `config.yaml`; the README (new section 11; the P3-P5 sentences that said 'no zone entity until P6'
  are in the present tense; the privacy summary says what the zone entities reveal; the retained-topic list gains the tuning
  and availability topics).
- `THIRD_PARTY_NOTICES.md`: the k-of-n entry (P43YM/ld2450_zones) and the zone margin (TillFleisch/ESPHome-HLK-LD2450) are no
  longer 'planned': they are idea references with no code used; the pass-speed idea of zomco/mmwave-engine is added; the
  hlk_radar_fusion line is rewritten as well: since P6 the second radar (LD2412) may only shorten a held zone (idea reference, no code used).
- Existing tests: the version strings, the `SHELL_MODULES` list of the lazy-import test, the description line of the manifest
  test and one assertion of `test_p5_service.MainWiringTest` (the subscriptions `main` builds are 34, not 30: `main` builds the
  P6 topics).

### Privacy changes (read before you update)

P5 published no per-zone topic. P6 does, on purpose: per-zone occupancy, count, events and attributes. **Zone names appear
only in the Home Assistant entity and device names of the discovery configs** (not retained), never in a topic or an event
payload. The six attributes include the hours a zone is usually used and the zone's outline (`CD:privacy`: details appear only
inside Home Assistant); occupancy, events and hours are a location and routine log once the recorder keeps them. The README
recommends the recorder excludes for the event entities and the 10-day purge. The retained topics added are the two tuning
states and the request-switch states of each zone (a small integer or ON/OFF) and `radar_zones/zone/availability`.

### Interpretations and open points (for the integration)

The numbers are the decisions of the P6 specification (section 13.1); the owner questions Q1-Q20 are not answered yet and the
spec's default applies to each.

- **Q1** no room occupancy entity (not in the P6 scope; the 0.5.0 comments that said P6 are corrected). **Q2** the latency
  gate wording: enter 0.8 s on frame time and 0.84 s on arrival time, leave 0.5 s on frame time with the arrival time
  reported (0.64 s: the firmware's 500 ms batch gate alone uses the budget). **Q3** 24 zones with entities. **Q4** the tuning
  entities are built now, automatic re-tuning stays P7. **Q5** `forget` is per-zone evidence and tuning, not 'Remove zone'.
  **Q6** doors are read from the silent `entry_zone` suggestions; there is no door confirmation flow. **Q7** `early_absent_s`
  60 and `ldr_min_known_s` 3,600 are proposals; the evidence is not decayed (P7). **Q8** a variant's name is its id
  (`wd0917`), the request is one switch per variant. **Q9** the blueprint inputs are proposals. **Q13** the optional recorder
  valve is built and OFF (`P6Params.valve_after_episodes`, 35 if turned on; it holds back events, count, variant sensors and
  attributes of a zone after that many episodes in a local day, never occupancy or availability). **Q14** the six attributes are
  always published. **Q15** zones are unavailable, not held, while the radars are off. **Q16** `zone6` keyed by zone id.
  **Q17** `via` early, `dwell_s` from the zone becoming occupied, P5 entity ids. **Q18** after a Forget all the `unique_id` and
  the device identifier carry the installation epoch (`radar_spot_e1_z0001`, `radar_zone_e1_z0001`), so a reused zone id does
  not inherit a removed zone's registry entry. **Q19** only dwell, short_task, bed and manual zones have entities. **Q20** the
  '< 500 recorder rows per zone per day' line is a proxy that holds up to 121 episodes a day for the default set and 74 with
  everything recorded; above that it fails and `zones_over_budget` (core) says so.
- Shell readings of the spec: (1) The attributes are published also while the zone is unavailable (the state source `radar_off`
  or `warmup` must be visible then); occupancy and count are not. (2) `typical_hours` and `confidence` are fields of the core's
  `ZoneState` (CORE added them after the interface of Appendix A, which has no other path to the shell). The shell reads the
  installation epoch from `Learner.zone6.export()["set"]["epoch"]`. (3) The entity names are `<zone> occupancy`, `<zone> target
  count`, `<zone> enter/leave`, `<zone> clear delay`, `<zone> margin`, `<zone> forget learned data`, `<zone> - <variant>
  sensor` and `<zone> - <variant> occupancy`; an unnamed label is `Spot <id>`. If Home Assistant puts the device name in front
  of an entity name, the names read doubled: the entity ids are fixed by `default_entity_id` either way. (4) A zone that is new
  to Home Assistant gets its not retained states again 10 s after its config (an entity is created after its config and drops
  earlier states); a burst has the service's second pass. (5) A zone event that the broker link cannot take is dropped and
  counted (`zone_events_dropped`); the other zone topics are sent again at the next tick. (6) Zone commands are checked twice:
  in the MQTT thread (the mapper knows the zones that have entities) and in the layer; both count into `zone_cmds_ignored`.
  (7) A tuning command is applied while the radars are off (D38); the number echoes the effective value from the core's
  `ZoneStates`, read back at the next tick when the core does not emit it. (8) The `state_source` values beyond `live` are a
  proposal: `failsafe`, `radar_off`, `warmup`, `fault`. (9) The availability list has the shape of spec 8.1 for the state
  entities; the tuning entities, the button and the request switches use the single learner availability topic.
- Not verifiable here (nothing ran against Home Assistant): HA[U1]-HA[U7] (spec 14.2) and the blueprint, acceptance steps A1,
  A2, A8, A9; the real recorder rows, the real latency over MQTT, the CM4 cost and the LD2412 numbers (A3, A5, A6, A12).

### Changes after the P6 replay (second fix round)

- **Replay (scripted walkers, P6-15):** a scripted walker used to be born and to end inside the room, mostly beside a zone: a
  new track has speed 0 for its first detections and the last waypoint of a track is held for 10 frames (speed 0), so the pass
  looked like a person who stops, and a pass of 1.0 m/s that began or ended beside a dwell zone gave an enter and a leave
  (P6-15 failed in every seed at every noise level). `replay/p6_people.walk_through` now extends each end of a pass, in whole
  one-second steps of the same speed, until it is 1.6 m (`WALK_CLEAR_M`) from every spot (at most 7 extra seconds in the matrix).
  The zone rule (`core/zoneeval.py`) is not touched. P6-1, P6-2 and P6-15 pass in all 15 edge runs (176 passes a run, none failed).
- **Replay (P6-10):** the `p6setup` runs were 3 seeds of 10 nights, and only seed 1 produced a zone with variants (the others
  were INCOMPLETE). They are now 5 seeds of 21 nights (two weekends, 14 weekdays: variants in 4 of 5 seeds, all of them pass);
  the P6 profile is about 870 simulated days.
- **Core (cost, P6-R3):** `core/zoneeval.py` does the same work with fewer steps: a detection of a track that is a member of
  every zone it is in no longer builds a set and sorts the member ids; the per-frame sweep skips the loop (and the sorting of
  the active zones) when every active zone has members that are all recent and no hold; the typical hours, confidence and variant ids
  of a zone are formatted once per `zone6.info` entry (the entries are replaced as a whole, never changed in place) and not for
  every state row; the filtered speed is read only for a zone the track is not in yet. Outputs are identical (the probe's
  walking and sitting runs give the same outputs byte for byte, four replay runs give identical results). Evaluator cost on
  the 200-zone probe: 15 to 17 microseconds per detection before, about 10 to 12 after (sitting 9 before, 7 to 7.5 after).

### Not built

- A room occupancy entity (Q1), an entity for a transit zone, a confirmed-door flow, a per-variant delay.
- The automatic re-tuning of the constants, drift, moves, retire and the night-window freeze rule (P7).
- Writing to Home Assistant services from the app, and any firmware change.

### Verification (development PC)

- Shell unit tests (the eight `test_p6_*` modules above: 24 + 30 + 54 + 36 + 17 + 16 + 22 + 24 = 223 tests), all OK, the
  end-to-end cases with the real `Learner` included (a person walks in, sits and leaves; a night with the radars off gives
  `on -> unavailable -> on` with no second enter; a variant request; Forget all moves the epoch). The existing P3-P5 tests
  are unchanged apart from the mechanical edits listed under 'Changed'. The full unit-test count is filled in at
  integration.
- The replay checks of the P6 specification (P6-1 to P6-4, P6-7 to P6-11, P6-14, P6-15 and the regression checks P6-R1 to
  P6-R3) belong to the core and replay owners and are not reported in this block.
- **Not verified:** everything Home Assistant does (above), a real zone edge, a real power cycle and the real clear-delay
  distribution. They are the acceptance steps A1-A12 of the specification, section 11.9.

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
