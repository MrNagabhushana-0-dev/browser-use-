# LOOP_MEMORY (append-only)

Evidence log and distilled rules for RETINAT_CONTRACT v1.0. Every entry: MEASURED (command and output attached) or
INFERRED (labelled). Entries are never edited; corrections are new entries.

## Definitions (fixed 2026-10-22, Milestone-1; changing them to move a number is B8)

- Watcher: one sub-agent per run, given only Retinat tools; its own Claude Code transcript (JSONL) is the token counter.
- METRIC-A (tokens/min): over the watch window (first watch call issued -> last watch result received, plus the one
  turn after it), sum per assistant message (deduplicated by message id) of input + cache_creation + output tokens,
  divided by the window's wall-clock minutes ("A_added"). Also reported: the same plus cache_read ("A_processed"), and
  A_added per minute of video covered.
- METRIC-B (s): for each event in a watch result (each timeline row after the first, at film time v, in a call that
  covered film time v0..v1, issued at t_call, answered at t_result): t_result - (t_call + (v - v0)). Median and p90.
  It assumes the video shows v0 at t_call, which can only overstate the latency. Also reported: call issued -> answered.
- C1 (narration accuracy): of the watcher's narrated events, the share the verifier finds true on the screen recording.
- C2 (event coverage): of the events the verifier labels on the recording over the watched span, the share the
  narration missed. Answers may be late; evidence must not be missed.
- Verifier: a fresh sub-agent that did not run the watch, given only the recording, the transcript and
  `scripts/contract/measure_watch.py`; it recomputes A and B and labels events itself, and tries to refute.
- Protocol (same for every experiment): Wikimedia Commons "File:Big Buck Bunny 4K.webm" (CC BY 3.0), film time 0:00 to
  3:00, played live from a fresh page, recorded with retinat_record; narration from pixels only (no page text, no
  captions, no transcript).

## Log

- 2026-10-22 MEASURED: YouTube is refused to this container's network: retinat_open
  https://www.youtube.com/watch?v=aqz-KE-bpKQ -> "BLOCKED: google-unusual-traffic (google.com/sorry/)". Not bypassed (no
  bot-block evasion). Milestone-1 runs on the same film from Wikimedia Commons as a stand-in until the owner's own
  network is available (P2 / cobrowse).
- 2026-10-22 CORRECTION (METRIC-B definition, before any comparison): the formula t_result - (t_call + (v - v0))
  assumed the video showed v0 at t_call and was written as "can only overstate". MEASURED in E0: each watch covers from
  the previous call's end (coverage 0.6-11.2 s, 11.6-28.2 s, 28.4-45.0 s ...), about 5 s before the call, so it can
  understate. METRIC-B is now v1 - v per event: a lower bound (the model sees nothing of a window until it closes at v1;
  the time to build the result after that is not in it). Also fixed: the parser read only "12.3s" times, not "1:02.3",
  and first reported 43.8 s covered and 6 events for E0; the corrected parse reads 180.8 s and 33.
- 2026-10-22 E0 baseline (product at fb0bb25; watcher: retinat_watch seconds=12 until=time hold=false detail=glance,
  live), MEASURED with `uv run --no-sync python scripts/contract/measure_watch.py subagents/agent-a37e48ab934f5cc04.jsonl`
  (scripts at 71e4ae1): 11 calls, window 185.5 s, film covered 180.8 s; A_added 6,036/min, A_processed 255,938/min;
  B call->answer median 12.64 s (max 12.93); B event lower bound median 7.9 s, p90 14.1 s (n=33). Recording:
  /root/.config/browseruse/recordings/retinat-20261022-150157.mp4. C1/C2 and verification: pending (verifier running).
  Per-turn split (INFERRED from the same transcript): each watch turn adds ~1,240-1,560 new tokens (the result plus the
  turn's own narration) and writes ~300-480; the window's last turn is the watcher's JSON summary (~3,000 out), which
  inflates A in every run of this protocol alike.
- 2026-10-22 E1 declared before measuring: ONE change, configuration only (product unchanged at fb0bb25): the watcher
  calls retinat_watch until=event (stop at the first cut or sound change) instead of until=time 12 s; seconds=12 stays as
  the cap. Target: B (event lower bound median). Keep if B median strictly drops AND C2 does not worsen; A is recorded
  as the trade-off (a later product change targets it).
- 2026-10-22 E1 attempt 1 VOID (MEASURED from its report, recording retinat-20261022-150817.mp4): the film never played,
  both watches said MEDIA_ERR_SRC_NOT_SUPPORTED, so there is nothing to measure. The same page played on a probe right
  after (covering 0.7-2.5 s). Cause not proven; suspect: E0's tab still playing the same stream (its last watch did not
  pause). Protocol addition for every run from here: a fresh browser (Retinat restarted) before the watcher starts.
- 2026-10-22 E1 attempt 2 (fresh browser; until=event), MEASURED with measure_watch.py on
  subagents/agent-a144a20df7cf8f44d.jsonl: 19 calls, window 195.3 s, film covered 176.6 s; A_added 10,008/min (E0 6,036:
  +66%), A_processed 438,175/min; B call->answer median 4.98 s (E0 12.64); B event lower bound median 3.4 s, p90 8.3 s
  (n=25; E0 7.9 / 14.1, n=33). Recording retinat-20261022-151008.mp4. Keep decision waits for C2 (verifiers running).
  Per call (the product's own estimate in each result, INFERRED): percept ~565 tokens = sheet image ~376 (4 keyframes,
  1292x200 every time) + text ~190; per turn ~1,313 new tokens and ~360 written: the product payload is under half.
- 2026-10-22 E0 VERIFIED by a fresh verifier (it recomputed from the transcript with its own code; it did not run the
  watch): A_added, A_processed, B reproduce exactly. C1 = 50/52 = 0.96 (both false lines are motion read into stills).
  C2 strict (a line within +-3 s that describes the event) = 18/68 missed = 26.5% (cuts 5/31, actions inside shots
  13/37); lenient (any line within +-3 s) = 2/68. Its refutations, all taken:
  (1) A_added counts each turn's output twice (it returns as the next turn's cache_creation) and leaves out the last
  turn, where the narration was written; (2) the p90 index rule read low (14.1; linear 14.66); (3) 10 gaps of
  0.2-0.4 s between windows, 2.3 s of film, hold 2 real cuts (79.97, 131.3) that reached no percept: a product bug;
  (4) the watcher wrote all narration once at the end, not after each result, so B is percept arrival, not narration;
  (5) the Commons player showed its captions ("( bird chirping )") in the pixels, which the protocol forbids;
  (6) narration lines = the keyframes on the sheets, so stretches with no keyframe (e.g. 83-94 s) are missed by
  construction.
- 2026-10-22 CORRECTION 2 (measurement, applied to every run alike): A_unique = input + cache_creation over the turns
  from the first watch call to the watcher's last turn, plus that last turn's output, per minute of that span (every
  token once, all narration included); p90 by linear interpolation; gaps between consecutive windows reported. MEASURED:
  E0 A_unique 5,509/min, B p90 14.66 s, gaps 10 totalling 2.3 s; E1 A_unique 8,947/min, B p90 8.54 s, gaps 17
  totalling 4.4 s.
- 2026-10-22 E1 VERIFIED by a second fresh verifier: every script number reproduced. C1 = 72/73 = 0.986. C2 strict =
  13/83 = 15.7% missed; all 31 edit-list cuts and the title fade covered (E0: 5 of 31 cuts missed); the 13 misses are
  actions inside shots between keyframes. Refutations taken: (1) B left out the 18 events that stopped a watch ("stopped:
  cut at X", the very events until=event speeds up): CORRECTION 3 counts them (v1 - X); (2) ~13.8 s of buffering stalls
  separate wall and film time (A per film-minute is also reported); (3) again no narration text between calls, all
  written once at the end; (4) captions again rendered in the pixels. Its labels differ in number from E0's verifier
  (83 vs 68): C2 across runs is comparable only roughly; cut coverage against the edit list is the steady part.
- 2026-10-22 CORRECTION 3 MEASURED: E0 B lower bound unchanged (n=33, median 7.9 s, p90 14.66 s: a timed watch never
  stops on an event); E1 B lower bound n=43, median 1.0 s, p90 6.98 s.
- 2026-10-22 E1 DECISION: KEEP (declared rule: B median strictly down 7.9 -> 1.0 s; C2 not worse 26.5% -> 15.7%, cuts
  26/31 -> 31/31). Trade-off recorded: A_unique 5,509 -> 8,947/min (+62%), more calls (11 -> 19) each with a fixed
  per-turn cost. Rule (INFERRED, from E0/E1): an event gate trades tokens for latency through the call count.
- 2026-10-22 PROTOCOL v2 (new baseline series; E0/E1 numbers do not compare across it). Why, MEASURED: Wikimedia now
  answers this network 429 (API and upload.wikimedia.org, curl at 15:40; likely also what voided E1 attempt 1), YouTube
  refuses it (google.com/sorry), and the Commons player rendered captions into the pixels. v2 stimulus: a local page
  (http://127.0.0.1:8765/watch.html, a play button over a <video>) serving the first 190 s of
  bbb_sunflower_1080p_30fps_normal.mp4 (CC BY 3.0, Blender Foundation; the exact file tests/ci/assets/big_buck_bunny/
  edits.json was built from, so cut times are known), transcoded VP9 854x480 + Opus (scratchpad stim/bbb480.webm). No
  captions; its sound is readable (same origin), so percepts carry a sound strip. Watcher: E1's configuration (kept),
  one tab, narration written as text after each result. Probe: the film plays (covering 0.0-3.0 s).
- 2026-10-22 B1 declared: protocol v2 on the product before E2 (eyes/service.py at 3bf6cc8's parent of d3a6df1, loaded
  into a Retinat started for it, MEASURED by placing that file before the start). Then E2 (d3a6df1, gap fix) is ONE
  change against B1. E2 keep rule: gaps between windows strictly down AND C2 (cut coverage vs edit list) not worse
  AND B median not worse.
- 2026-10-22 MEASURE ADDED before B1's first result: cuts_reached_model = of the film's own cuts (edits.json, exact for
  protocol v2's file) inside the film span the windows covered, the share some watch result reported (a timeline row or
  "stopped: cut at") within 0.7 s. Objective, no judgement; C2 (verifier's labels, incl. actions) stays the primary.
  On E1 (Commons copy, timing approximate): 31 of 32, missed 39.77 s. (The B1 entry above means: eyes/service.py as in
  d3a6df1's parent.)
- 2026-10-22 B1 MEASURED (measure_watch.py --edits, subagents/agent-a2ddffde6832e7aad.jsonl; recording
  retinat-20261022-154352.mp4): 21 calls, window 181.2 s, film covered 154.1 s; gaps between windows 12 totalling
  27.0 s (max 16.0 s); cuts_reached_model 29/32 (missed 39.77, 104.17, 131.3); A_unique 9,865/min; B lower bound
  median 3.7 s, p90 6.44 s (n=37); call->answer median 3.55 s, max 18.59 s. No narration text between calls (0 text
  blocks) despite the firmer instruction. Cause of the 16 s gap (MEASURED from the call log): a call stopped watching at
  film 34.3 s but answered 18.6 s after it was issued (with the film's sound readable here, the percept's speech model
  ran on CPU), and the next watch started from when that percept was done (film 50.3 s). E2 runs next: fresh Retinat on
  d3a6df1, the same prompt.
- 2026-10-22 E2 MEASURED (product d3a6df1 in a fresh Retinat; subagents/agent-ac05c1c116f61f401.jsonl; recording
  retinat-20261022-154838.mp4, 196.9 s): 24 calls, window 184.9 s, film covered 189.7 s; gaps between windows 0 (B1:
  12, 27.0 s); cuts_reached_model 34/34 (B1 29/32); B lower bound median 2.7 s, p90 6.85 s (B1 3.7 / 6.44); call->answer
  median 2.55 s, max 8.89 s (B1 3.55 / 18.59); A_unique 8,086/min (B1 9,865). The watcher was stopped by an API
  safeguard flag while writing its final JSON: its hand-back narration ends at film 2:03 (123 s); all watch calls and
  the recording are complete. Declared rule (gaps strictly down, cut coverage not worse, B median not worse): met ->
  KEEP, pending the verifier. C1/C2 for B1 and E2 are judged on film 0:00-2:03 only, alike.
- 2026-10-22 STOP-RULE NOTE (MEASURED): the session limit (HTTP 429, "resets 6:40pm UTC") stopped both B1/E2 verifiers
  part-way; they were resumed after the reset with their work kept. The 85% pause in 4.5 could not be applied: usage is
  not visible from inside the session; the limit arrived at 100%. The local film server stopped at its 2 h background
  limit; the verifiers need only files.
- 2026-10-22 B1 VERIFIED (fresh verifier a611b3118fdc6b9d1, own code from the transcript and the recording): reproduced
  calls, window, gaps 12/27.0 s, cuts 29/32 (29/29 of the cuts inside a window; the 3 missed sit in gaps), A_unique
  9,865, B 3.7/6.44 exactly. C1 54/54 = 1.0; C2 strict 7/50 = 14% missed, lenient 1/50. Refuted: (1) B counts 6 stops
  twice (the stop's cut is also the last timeline row of the same result): n=31, median 4.40 s, p90 6.80 s; (2)
  A_unique's span ends where the last turn began, not where it finished (8,878/min to its finish); (3) "0 text blocks"
  is wrong: 1 text block, before the first watch, none between calls. End to end on the recording (event on screen ->
  result in the transcript): median 4.68 s, p90 8.15 s.
- 2026-10-22 E2 VERIFIED (fresh verifier aa84e0ecb75d42a11): reproduced calls 24, window 184.9 s, gaps 0 (23 of 23
  transitions overlap by one 0.2 s sample), cuts 34/34 (all within 0.13 s; 31 from rows, 3 from stops; the same at
  0.3 s), B 2.7/6.85, call->answer 2.55/8.89; the film played without a stall (recording = film + 5.40 s +-0.05). C1
  59/60 = 0.983 (one false: 55.0 "back to camera", a pixel misreading); C2 strict 6/54 = 11.1% missed (8/54 = 14.8% if
  the truncated last line is not narration), lenient 0/54. Refuted: (1) A_unique 8,086 is a script bug: the last line
  is a synthetic zero-usage "API Error" message, taken as the last turn, which drops the last real turn's 5,090 output
  tokens: 9,467/min to the hand-back; (2) B counts 8 stops twice (as B1): n=38, median 3.15 s, p90 7.22 s; (3) film
  covered 189.7 s sums overlapping windows; the union is 185.1 s. Not refuted: pixels-only narration (every line at a
  keyframe time, nothing from sound or speech); d3a6df1 explains the zero gaps (mechanism and data agree, n=1 a side).
- 2026-10-22 CORRECTION 4 (measure_watch.py, applied to every run alike, all four recomputed): a stop whose cut is a row
  of the same result counts once; a synthetic zero-usage message is not a watcher turn; A_unique's span ends where the
  last turn finished (the next line, else the transcript's last); film covered is the union of windows. MEASURED:
  run  A_unique/min  film s  gaps n/total  cuts   B median/p90 (n)
  E0   5,166         180.8   10/2.3 s     33/33  7.9/14.66 (33)
  E1   8,099         176.6   17/4.4 s     31/32  1.3/7.78 (35)
  B1   8,859         154.1   12/27.0 s    29/32  4.4/6.8 (31)
  E2   9,447         185.1   0/0          34/34  3.15/7.22 (38)
  Verifier figures to the hand-back line: B1 8,878, E2 9,467 (within 0.3%). E1 stays KEEP under its own rule (B median
  7.9 -> 1.3).
- 2026-10-22 E2 DECISION: KEEP d3a6df1 (declared rule: gaps 27.0 s -> 0 strictly down; cuts 29/32 -> 34/34 and C2
  strict 14% -> 11.1% not worse; B median 4.4 -> 3.15 s not worse). Regressions it did not target, MEASURED, n=1 a side:
  A_unique +6.6% (8,859 -> 9,447; 154.1 -> 185.1 s film seen in a like window: the per-film-second cost fell), B p90
  6.8 -> 7.22 s. Rule (INFERRED): the next watch must start at the last sample taken, never at when the percept was
  done. Open: narration is the keyframe captions, so a short action between two keyframes is missed by construction
  (6/54 in E2); the watcher writes no text between calls, so B is percept arrival, not narration.
- 2026-10-22 19:30 UTC CONTRACT v1.1 IN FORCE; MILESTONE-1 closed by owner review. M2 GO (P2 track). Owner update
  00:40 IST (relayed): measurement on the owner's Chrome is permitted; the owner installs the extension himself.
  Blocker, MEASURED: this container has no inbound route and the platform cannot reach the owner's computer, so the
  bridge runs on loopback on the owner's laptop (relay + Retinat there), driven by a Claude Code session he starts
  there; tonight's work is container-side only.
- 2026-10-22 PROTOCOL v2b DECLARED (before any v2b measurement): the P2 code path in this container. Container Chromium
  with the bridge extension loaded by --load-extension (stand-in for Load unpacked) on an Xvfb display; relay inside
  Retinat (RETINAT_BRIDGE=9333); stimulus exactly v2's (127.0.0.1:8765/watch.html, bbb480.webm, edits.json cuts); the
  film tab shared through the extension (alwaysShare glob for the stimulus page only); recording by retinat_record
  through the bridge; watcher a fresh Sonnet sub-agent; counter measure_watch.py as at c1bef95. Verifier in two stages:
  a LABELLER sees only the recording (and ground_truth.py's sheets), writes event labels, and their sha256 is logged
  here BEFORE a separate SCORER reads any narration. v2 (P1, CDP direct) numbers are reference only, not comparable.
  Run order and keep rules, declared now:
  B2: v2b baseline, product at c1bef95 (E2 kept), E2's watcher prompt verbatim.
  E3: narration per result (a `narration` argument on retinat_watch, refused while the previous result is unnarrated;
      watcher prompt says so). Keep: every result narrated before the next call (server narration log AND transcript)
      AND C1 not worse AND C2 not worse.
  E4: INV-2 retroactive-commit reservoir as a component (browser_use/eyes/reservoir.py): E2's cursor generalised,
      uncommitted evidence evicted from the ring counted as evidence_erased_s. Keep: gaps 0 AND evidence_erased_s 0
      AND cuts_reached_model not worse AND C2 not worse.
  E5: push gate: a 4-state Schmitt gate (IDLE, CONFIRM_ONSET, ACTIVE, HANGOVER) over the reservoir's per-frame change
      signal decides when the model is woken; no time-only wake-ups. Keep: A_unique strictly down vs E4 AND inside
      2,000-8,600/min AND C2 not worse AND gaps 0. B is reported; a B rise is the declared trade (C2 outranks B).
  Flapping pair (every M2 report): gate transitions/hour of the 4-state gate vs a single-threshold ablation, replayed
  offline over the same logged per-frame signal, on the film and on a borderline page (blinking caret, slow animated
  chart), with onset clipping (seconds of event onset not committed) beside it.
- 2026-10-22 PROTOCOL v3 DECLARED (before any v3 measurement; runs on the owner's Chrome after his install): v2b's
  method on a real YouTube tab he shares and nothing else; captions off; first 190 s watched; recording through the
  bridge. Candidate stimulus: Blender Foundation's own Big Buck Bunny upload (id aqz-KE-bpKQ, UNVERIFIED-EXTERNAL from
  memory), the same film as v2, so edits.json can give exact cuts if the recording's cuts align to it (checked by
  ground_truth.py before scoring; if they do not align, labels only). Full text: docs/contract/PROTOCOL_v3_P2.md.
- 2026-10-22 B2 MEASURED (protocol v2b, product c1bef95 = E2 kept, E2 prompt verbatim; watcher transcript
  subagents/agent-adf046dbda764bac2.jsonl; recording retinat-20261022-192004.mp4; measure_watch.py at c1bef95): 23
  calls, window 184.9 s, film covered 184.9 s, gaps 0, cuts_reached_model 33/33, A_unique 9,370/min, B lower bound
  median 3.9 s p90 8.5 s (n=37), call->answer median 3.19 s max 12.13 s. Narration: 0 text blocks in the whole run;
  all 88 lines in the final answer (the 5th run in a row to batch it, despite the prompt). Verifier (labeller, then
  scorer) pending. Measured-commit pinning from E3 on: Retinat runs from a git worktree of the measured commit
  (RETINAT_CODE_DIR, bin/mcp-serve.sh), so files other agents edit in the main tree cannot leak into a run.
- 2026-10-22 E3 MEASURED (v2b, commit 13039f1 pinned; transcript subagents/agent-a2c886e76af530c78.jsonl; recording
  retinat-20261022-192510.mp4; narration log ~/.config/browseruse/narration/20261022-192444.jsonl): 27 calls (one
  interrupted by an API safety classifier mid-call), gaps 0, cuts 34/34, A_unique 10,996/min (B2 9,370), B lower bound
  3.75/6.46 s (n=38). Narration: 25 of 25 results that had a next call were narrated before it (server log and
  transcript agree); the last was not: retinat_narrate is missing from this client's tool list (cached at session
  start), a harness gap, fixed in E5's commit by taking the narration with retinat_record stop. Narration came a
  median 3.69 s after its result (max 5.43). NEW MEASURE, event -> narration (B_narr, measure_narration.py): median
  8.44 s, p90 12.17 s (n=34). Verifier pending; keep decision waits on C1/C2.
- 2026-10-22 E4 MEASURED (v2b, commit 7b9bc1e pinned; transcript subagents/agent-a3c40ff79ae867c31.jsonl; recording
  retinat-20261022-193123.mp4; log narration/20261022-193107.jsonl): 25 calls, gaps 0, evidence erased 0 (no percept
  said so), cuts 34/34, A_unique 10,691/min, B lower bound 3.9/6.92 s (n=39), narration 24/24 before the next call
  (last again unnarrated, same harness gap), B_narr 9.32/13.74 s. Verifier pending.
- 2026-10-22 E5 DECLARED BEFORE MEASURING (commit 0fa303c): wake policy = retinat_watch until=gate: the call returns when
  the always-on 4-state gate (tau_high 3.0, tau_low 1.5 on the EMA of sight.deltas, attack 0.6, decay 0.15, n_on 3,
  n_off 10 frames) closes an episode, not before 8 s, or once an episode has gone on 15 s into the call; the 30 s cap
  is the transport's (a client stops a call at 60 s), reported as such. The sheet keeps B2's evidence density
  (max(4, round(0.465 x seconds committed)) keyframes): one mechanism, gate-woken commits at constant density, so the
  experiment does not trade evidence for tokens. Comparison base: E4. Keep rule as declared (A strictly down AND in
  2,000-8,600 AND C2 not worse AND gaps 0).
- 2026-10-22 FLAPPING PAIR, OFFLINE (gate_replay.py on commit 0fa303c; same signal through both gates; MEASURED):
  film (bbb480.webm 0-190 s, 1,901 frames, 34 edit cuts): 4-state 21 flips (398/h), 11 episodes, 8 cuts begun while
  closed, clipped 0.37 s max (2.62 s total) without a reservoir; single threshold 71 flips (1,346/h), 36 episodes, 11
  clipped, 0.07 s max (0.49 s total). With the reservoir the commit starts at the cursor, before any onset: 0 clipped.
  Borderline page (120 s; caret 1 Hz, creeping sparkline, ticking clock, an animated card swept in amplitude; median /
  p90 score): pulse 0 (0.03/0.05): 0 flips/h both. pulse 20 (0.74/1.85): 4-state 0, single 360/h. pulse 30
  (1.08/2.71): 4-state 30/h, single 6,606/h. pulse 40 (1.38/3.46): 4-state 30/h, single 7,596/h. pulse 60 (1.79/4.88):
  4-state 30/h, single 2,612/h. Resolution: one flip in 120 s is 30/h, so the 2.0/h target needs >= 1 h of signal;
  at pulse >= 30 the 4-state gate opens once and stays open (no flapping, but no close either).
- 2026-10-22 E5 MEASURED (v2b, commit 0fa303c pinned; transcript subagents/agent-a17fbfeeef8ead1ad.jsonl; recording
  retinat-20261022-193732.mp4; narration log narration/20261022-193716.jsonl): 10 calls (E4 25), A_unique 4,253/min
  (E4 10,691; -60%; inside 2,000-8,600), gaps 0, evidence erased 0, cuts 34/34, film covered 185.9 s, keyframes 84
  (0.45 per film second; B2 0.465), stops: 6 "event going on for 15s", 4 "event over (gate)". B lower bound median
  11.95 s p90 18.15 s (E4 3.9/6.92): the declared trade. Narration 10/10 results, the last with retinat_record stop;
  narration a median 4.8 s after its result; B_narr median 17.39 s p90 24.63 s (E4 9.32/13.74). Windows run ~20 s,
  not 15: the reservoir also commits what played during the model's own turn (~5 s). Disclosure: CPU contention
  throughout B2-E5 (load average up to 9.7 on 4 cores: the side-track builders decoding video and running Chromium
  tests; a narration-burn encode ran ~2 min during E5's first minute and was stopped); B and lag are affected, A is not
  (it counts tokens). Ground truth for C1/C2: B2's recording aligned to the edit list (ground_truth.py: offset
  6.412 s, 34/34 cuts, residual median 0.05 s), 32 contact sheets at 0.5 s; one labeller labels the film once, for
  all v2b runs alike. Keep decision for E3/E4/E5 waits on the scorers.
- 2026-10-22 FLAPPING PAIR, LIVE (E5's own gate log, ~/.config/browseruse/eyes/gate-20261022-193718.jsonl: 1,488
  frames over 190 s, replayed through both gates by gate_replay.py log; MEASURED): 4-state 9 flips (170/h), 5
  episodes; single threshold 69 flips (1,307/h), 35 episodes. Proof video of live narration per result (E5, burned
  in at the second each was written): scratchpad E5-narrated.mp4 (10 narrations, 9 shown before the recording ended;
  the 10th came with the stop).
- 2026-10-22 E6 DECLARED BEFORE MEASURING (commit 3504321, on E5): a gated watch is also woken by a salient event (a cut,
  a new item, a change in sound: the until=event triggers), never before 8 s. Why: E5 tripled B (3.9 -> 11.95 s
  median). Keep rule: B lower bound median strictly down vs E5 AND A_unique inside 2,000-8,600 AND C2 not worse AND
  gaps 0.
- 2026-10-22 STOP-RULE EVENT (MEASURED): the session limit (HTTP 429, "resets 11:40pm UTC") stopped every running agent
  at ~19:55 UTC: the E6 watcher (it had only looked and found the play button: E6 is VOID, not measured), the
  labeller (part-way through the sheets), the side-track reviews and the hooks build, and the core-code review. The
  85% pause could not be applied (usage is not visible here; it arrived at 100%, the second time). The container
  rebooted during the pause: files kept, processes gone. 23:42 UTC: rig restarted (scripts/contract/rig_v2b.py now
  checks for a running Xvfb, not a display socket a reboot leaves behind), the labeller resumed with its work kept,
  reviews and the hooks build re-run on cheaper models where they suffice (B9). Retinat's MCP server failed to
  reconnect after the cold boot (CONNECT_TIMEOUT 30 s), so no watcher can run in this session until it is reconnected
  (/mcp, the owner's side); E6 waits on that.
- 2026-10-23 PROTOCOL v3 AMENDED BEFORE ANY v3 MEASUREMENT: two arms on the owner's Chrome, in order: v3-B, the P2
  baseline (Milestone-1's watch: until=event, narration as the watcher writes it), then v3-G (the last KEPT v2b
  configuration, with live narration); one label set from v3-B's recording serves both. The measured commit on the
  laptop is the checkout's HEAD (logged in R0); Retinat is not restarted between arms (a cold start can miss the
  client's 30 s connect timeout, seen here at 23:42 UTC). Runbook and watcher prompts: PROTOCOL_v3_P2.md section 4
  (commit 4531d56).
- 2026-10-23 E6 BLOCKED, NOT MEASURED. This session's Retinat MCP client failed to reconnect after the reboot
  (CONNECT_TIMEOUT); /mcp reconnect is not available in this web session; one genuine retry (ToolSearch at 23:49 UTC)
  returned no Retinat tools. Respawning Retinat under a separate headless Claude Code client in this container was
  refused by the session's permission classifier, and is not pursued. E6 (commit 3504321, declared above) stays
  unmeasured; its keep rule is not applied. Two probe calls of a headless client were made before that (23:49-23:50,
  no watch): one appended its lines to this session's own transcript file, one wrote its own transcript
  (3be4613e-f5dc-4567-8c4a-a0b15021a3c4.jsonl); neither touched a measured run.
- 2026-10-23 LABELS FROZEN BEFORE ANY SCORING (MEASURED, sha256sum and ground_truth.py freeze agree): labels_v2b.json
  sha256 23db2c52559f45fc87a45491d987535b7f0d77cf2e999661548ec610de0799d4; 143 events (cut 37, action 79, appear 22,
  disappear 5), film 0.21-185.09 s, from B2's recording only (retinat-20261022-192004.mp4, offset 6.412 s); labeller
  sub-agent aa835b693867c20c9 (Sonnet), which opened no narration, transcript, measurement or edit list. It labelled
  one fade the detector does not flag (title -> sky, film 4.09) and listed its uncertain moments. Copies (with the
  alignment and cuts it used): docs/contract/evidence/. One label set serves B2, E3, E4 and E5 alike.
- 2026-10-23 B2 SCORED (MEASURED + VERIFIED, scorer ab30accb5855a3d5d, labels sha256 23db2c52 checked first): A 9,370/min
  and B 3.9/8.5 s (n=37), gaps 0, cuts 33/33: all reproduced by an independent script. C1 84/88 = 0.955 (four lines have a
  wrong detail: a leaf, a pine cone called an acorn, a nut's side, a nut called a ball; the main scene in each is true).
  C2 strict 54/142 missed = 38.0% (judgement range 30-47%), lenient 1/142 = 0.7%. Narration: all 88 lines in one batch
  in the final answer, zero text blocks between results, so B measures percept arrival, not narration. Scorer's files:
  scratchpad/score_B2/. Labels: no count-changing disputes.
- 2026-10-23 E3 SCORED (MEASURED + VERIFIED, scorer a3c32d0870d6d5c62, labels sha256 checked first): A 10,996/min, B
  3.75/6.46 s (n=38), gaps 0 (overlaps 0.1-0.2 s), cuts 34/34: all reproduced by an independent script. Narration
  delivered 94 of 98 lines; C1 94/98 = 0.959 (delivered lines 90/94 = 0.957). Four wrong details: a black frame said to
  show a logo, a red apple called a flower, the rabbit's flank called a rock, a nut called a stick. C2 strict 49/142
  missed = 34.5%, lenient 2/142 = 1.4% (both at 144.6 s, where the interrupted call left a 4.5 s gap in narration).
  Judgement sensitivity: 52-54/142 if the undelivered lines and disputed covers are counted. Narration: after each
  result (median 3.7 s), except the last result, which the retinat_narrate tool did not exist for (tool missing from
  the client's list, a harness gap). Scorer's files: scratchpad/score_E3/.
- 2026-10-23 E4 SCORED (MEASURED + VERIFIED, scorer a69b92184f0f63abe, labels sha256 checked first): A 10,691/min
  (own code 10,711, 0.2% apart), B 3.9/6.92 s (n=39), gaps 0, cuts 34/34: reproduced. Narration: 101 lines, 97 delivered
  (results 1-24, log = transcript); the last result's 4 lines were never delivered (retinat_narrate missing). C1 95/101 =
  0.941 (delivered 91/97 = 0.938). Six wrong or unconfirmed lines (a logo on black, motion read into a slow push-in, a
  hand held out, a posture, a butterfly 'lands', a head direction reversed). C2 strict 45/143 missed = 31.5% (judgement
  range 23-47%), lenient 1/143 = 0.7% (the bite, label 57, has no line at all). Narration came after each result. Scorer
  files: scratchpad/score_E4/.
- 2026-10-23 E5 SCORED (MEASURED + VERIFIED, scorer a49fa8e154d529b0a, labels sha256 checked first): A 4,253/min (own
  code 4,259 with the other span end), B 11.95/18.15 s (n=36), gaps 0, cuts 34/34 (33/34 counting only 'cut->' rows):
  reproduced. Narration 10/10 results, per result. C1 63/80 = 0.79 (lenient 70/80 = 0.875): the gate-woken windows
  carry more stale or wrong detail (a feather called a bird, a red butterfly called a flower, the apple core called a
  crest). C2 strict 71/143 missed = 49.7% (with one duplicate dropped 70/142 = 49.3%), lenient 1/143 = 0.7%. E4 was
  31.5% strict, 0.7% lenient.
  E5 DECISION (declared rule: A strictly down AND 2,000-8,600 AND C2 not worse AND gaps 0): A down 60% and in band: yes;
  gaps 0: yes; C2 strict 31.5% -> 49.7%: NOT NOT-WORSE. REVERT the wake policy (until='gate' and its salient wake,
  E5 and E6's code in eyes/service.py, retinat/server.py, retinat/tools.py restored to E4's state). Kept as measured
  components: eyes/gate.py and scripts/contract/gate_replay.py (the flapping ablation tool; its numbers stand). The live
  gate log (E5) stays a measurement, not a policy. E6 (3504321, declared on E5) is void with the revert.
  The 4.6 completion gate and the RESERVOIR (E4) are KEPT. Scorer's files: scratchpad/score_E5/.
- 2026-10-23 COUNTER INTEGRITY CHECK 1 (owner steering): the reservoir erasure counter's defects, fixed in the reservoir,
  retina (per-item eviction at ingest) and service code. Synthetic cases, MEASURED: held hops 0 (old ~20 s); shared-ring
  flood 0 (old 190 s); own eviction 5.5 s (15.5 - 10); stuck cursor 9 s then 0 (old 9 s each start). The fixed counter
  is never above the old, so E4's measured 0 stands (0 erasure lines in its transcript). Verifier: none run (owner
  steering: one check at a time; the synthetic cases are the test). Commit: see git log (counter fix).
