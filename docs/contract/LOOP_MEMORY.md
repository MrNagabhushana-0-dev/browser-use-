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
