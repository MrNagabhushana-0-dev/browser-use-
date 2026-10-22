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
