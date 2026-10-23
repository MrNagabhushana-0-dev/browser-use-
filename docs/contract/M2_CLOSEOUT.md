# Milestone 2 close-out (P2 track, protocol v2b in the container)

Status: PARTIAL. The measurement and verification are done for the container bridge path. The P2 baseline on the
owner's own Chrome (protocol v3) has NOT been run. E6 is blocked, not measured. Every number below is MEASURED
unless marked otherwise; each was re-derived by a fresh verifier (VERIFIED) unless marked otherwise.

## Results (protocol v2b: container Chromium + bridge extension, local copy of Big Buck Bunny, film 0-185 s)

| Run | Product commit | Change | A tok/min | B lower bound median / p90 (n) | C1 | C2 strict missed | C2 lenient missed | Decision |
|---|---|---|---|---|---|---|---|---|
| B2 | c1bef95 | baseline (E2 kept) | 9,370 | 3.9 / 8.5 s (37) | 84/88 = 0.955 | 54/142 = 38.0% (30-47%) | 1/142 = 0.7% | baseline |
| E3 | 13039f1 | live narration per result | 10,996 | 3.75 / 6.46 s (38) | 94/98 = 0.959 | 49/142 = 34.5% | 2/142 = 1.4% | KEEP |
| E4 | 7b9bc1e | INV-2 reservoir component | 10,691 | 3.9 / 6.92 s (39) | 95/101 = 0.941 | 45/143 = 31.5% | 1/143 = 0.7% | KEEP |
| E5 | 0fa303c | event gate wakes the model | 4,253 | 11.95 / 18.15 s (36) | 63/80 = 0.79 | 71/143 = 49.7% | 1/143 = 0.7% | REVERT (wake policy) |
| E6 | 3504321 | gate plus salient-event wake | not measured | not measured | - | - | - | BLOCKED, void |

Other measures, all MEASURED: gaps between watch windows 0 in every run; cuts reached the model 33/33 (B2) and
34/34 (E3, E4, E5); evidence erased 0 s (E4, E5, none reported); film covered 184.9-185.9 s per run.

Keep rules, as declared before measuring (LOOP_MEMORY):
- E3 (narration per result): every result narrated before the next call, and C1 and C2 not worse than B2. Met on
  the delivered lines (the final result's narration was lost to a missing tool in E3 and E4; see Caveats). KEEP.
- E4 (reservoir): gaps 0, evidence erased 0, cuts not worse, C2 not worse than E3 (31.5% vs 34.5%). KEEP.
  The C2 comparison is inside the judgement range (23-47%); "not worse" is the weakest part of this keep.
- E5 (gate): A strictly down and in band (yes), gaps 0 (yes), C2 not worse (NO: 31.5% to 49.7%). REVERT. The gate
  module and gate_replay stay as measurement tools; the wake policy is out.
- E6: declared on E5, void with E5's revert, never measured.

## Milestone bars (from contract section 5, M2)

- A into the 2,000-8,600 band: NOT MET. The only run in the band was E5, and it lost on C2. The kept runs sit at
  9,400-11,000 tok/min, above the band.
- C2 not worse than the P2 baseline: met (E3, E4 vs B2) within the judgement range.
- Zero gap-window loss: met (gaps 0 in every run).
- Flapping pair reported: reported for the gate only (see below). The reservoir and narration runs have no gate.
- Narration per result: met for the delivered results; the last result was not narrated in E3 and E4.

## Flapping pair (the gate: 4-state Schmitt vs single threshold; MEASURED, gate_replay.py)

- Live, E5's own signal (1,488 frames, 0.053 h): 4-state 9 flips (170/h), 5 episodes; single threshold 69 flips
  (1,307/h), 35 episodes.
- Film replay, bbb480 0-190 s (1,901 frames): 4-state 21 flips (398/h), 11 episodes, 8 cuts begun while closed,
  onset clipping without reservoir 0.37 s max (2.62 s total); single threshold 71 flips (1,346/h), 36 episodes,
  11 clipped, 0.07 s max. With the reservoir the commit starts at the cursor, before any onset: 0 clipped.
- Borderline synthetic page (120 s, pulse 0-60): 4-state 0-30 flips/h, single threshold 0-7,596/h. At high pulse the
  4-state gate opens once and stays open (no flapping, no close either).
- The contract's target (TPH below 2.0/h) needs at least an hour of signal to measure; the runs here are minutes.

## E6: blocked, not measured

Reason, logged in LOOP_MEMORY (2026-10-22 STOP-RULE EVENT and E6 BLOCKED): this session's Retinat MCP client failed
to reconnect after the container reboot (CONNECT_TIMEOUT 30 s). /mcp reconnect is not available in this web session.
One genuine retry returned no Retinat tools. Respawning Retinat under a separate headless Claude Code client was
refused by the session's permission classifier and not pursued. Later the MCP client did reconnect, but the browser
extension had gone with the rig; the rig was restarted, and the E6 run was not repeated under the owner's steering
(small steps only). E6's keep rule is not applied.

## Recording-quality caveat (MEASURED, ground_truth.py)

- The recordings are 8 fps, and frames arrive about 4 a second: 46-49% of frames are identical. Cut timing is good to
  about 0.25-0.36 s, and a few real cuts appear 0.3 s late (E2 at 60.5; E4 at 20.2, 60.5, 172.4, 174.0).
- The recording-to-film offset is not constant within a run: E3 is 5.31 s overall, but about 4.95-5.03 s for film
  0-45 s and 5.3 s after 52 s (scorer's frame check). Scorers used each run's own alignment.
- B1's recording has a 4 s frozen stretch before film 39.77; E5 has frozen stretches at film 73.8 and the tail.
- One labelled set (143 events, sha256 23db2c52...) from B2's recording serves all four runs. Labels are judgement:
  scorers' C2 ranges span 23-47% depending on how a line "describes" an event. Headline numbers use each scorer's own
  judgement call; they are not from a rule that could be checked mechanically.
- Single run per arm: no run has a repeat. Differences of a few points in C2 are not established.
- The C1 misses are wrong details inside otherwise true lines (a nut called a stick, a pine cone called an acorn, a
  butterfly placed wrongly). Glance-size keyframes limit detail; narration lines follow the sheet keyframes, so
  events between keyframes are missed by construction.
- Token counts come from the watcher's own transcript: A is the watch-turn tokens only. The E5 counter excludes
  the first call's turn (151 tokens; negligible).

## Narration

- Per-result narration held for 25 of 25 results in E3 and 24 of 25 in E4, both with live logs; the last result's
  narration was not delivered in either (retinat_narrate was not in the client's tool list). E5: 10 of 10.
- B2 wrote all narration at the end, in one final answer, despite the prompt. That is why B2 is a baseline, not an
  instance of per-result narration.

## Known defects not fixed (found by the adversarial reviews; not in any kept number)

- The keyframe density rule (since..now, per item) shrinks keyframes instead of adding evidence. It lived in E5 only
  and was reverted with E5.
- FIXED (counter integrity check 1, see LOOP_MEMORY): the erasure counter counted held-video hops (frames only now),
  charged other items' evictions to a shared ring (now per item, at ingest), and re-counted a stuck cursor (the cursor
  now moves to the floor once counted). Four synthetic cases: held hops 0 (was ~20 s); shared-ring flood 0 (was 190 s);
  own eviction 5.5 s exactly (15.5 - 10); stuck cursor counted once (9 s, then 0). An early-return bug, found by the
  own-eviction test, was fixed too. E4's measured 0 s stands: the fixed counter is never above the old one, and E4's
  transcript holds 0 erasure lines. Still open: `last_erased_s` is a side channel, so two concurrent watches can
  read each other's value (not seen in any run).
- The gate closes only on a frame arrival (not when the item changes or ends); E5-only.
- The gate log is unbounded and synchronous (E5 only).
- The counter-integrity review and the hooks and ground-truth reviews did not complete (session limit).

## Commits (branch claude/laughing-hawking-2cj3a6)

- c1bef95 E2 kept (baseline product); 13039f1 E3; 7b9bc1e E4; 0fa303c E5 (policy reverted in b12bcc5); 3504321 E6 (void)
- f2312e0 ground-truth tool and protocol v3 text; 4531d56 protocol v3 runbook; ba4a1a7 v3 amendment
- 7269d38 bridge setup, self-test, checklist; 30a4537 bridge review fixes (relay-address share refused; retry and PASS
  instructions; the eyes extra non-fatal in setup)
- 08d4645, d63610a task_complete.py (4.6 completion gate; under review, not complete)
- 53985bb labels frozen; 3645814 stop-rule event; 9971b66 E6 blocked; 7dfa7cd, aa8c2d5, 8a0d4c1 scoring entries; b12bcc5 E5 revert

## Not done

- Protocol v3: the P2 baseline on the owner's own Chrome, with a real YouTube video. Not run. The checklist sets it up;
  it is the owner's install that gates it.
- Windows and the macOS setup path: not run end to end (setup.ps1 never ran). The macOS path needs Apple Silicon on macOS 14+ for the eyes extra; not run on a Mac.

## Integrity checks after the close-out (owner steering, one at a time)

1. Counter integrity: the reservoir erasure counter is FIXED (commit 59472e8): frames-only loss, per-item eviction,
   the cursor moves once a loss is counted. Synthetic cases (MEASURED): held hops 0 (old ~20 s), shared-ring flood 0
   (old 190 s), own eviction 5.5 s exact, stuck cursor 9 s once. E4's measured 0 s stands (the fixed counter is never
   above the old; E4's transcript has 0 erasure lines). One residual: `last_erased_s` is a side channel.
2. Bridge self-test on the final code (commit f555102, extension 0.6.1), MEASURED with the owner's install path in a
   container on a separate display and port (Xvfb :97, relay 9444, setup's own browser):
   - `python -m browser_use.bridge setup --port 9444`, exit code 0; the log says:
     "Self-test 1 PASS: look 1 55.7 ms, click 136.9 ms, look 2 51.4 ms, button rgb (158, 158, 158) -> (47, 125, 50)";
     "PASS  self-test through relay -> extension -> tab". Earlier runs on 0.6.0 gave the same PASS (look 1 111.8 ms, click
     137.8 ms, look 2 61.1 ms, exit 0).
   - The relay-address refusal, probed as a CDP client against the final relay: Target.createTarget to
     http://127.0.0.1:9444/json/list and to /selftest both return "the bridge's own address is never shared"; no
     Allow window is opened (the refusal comes before the ask). Before f555102 the same probe opened an Allow window.
   - Not run: the FAIL path on the final code (it passed on 30a4537's worker by the builder's hands-on), Windows,
     and the owner's own Chrome.

## Hooks and completion gate (debt item 1, MEASURED)

- Gate (`scripts/contract/task_complete.py`, fixed in e8faf64): PASS case (real verifier file, recording, measurement
  object, "E11 DECISION: KEEP") appends one TASK_COMPLETE line; FAIL cases refuse with exit 1 and append nothing: a
  pending decision ("KEEP, pending the verifier"), an empty verifier file, and a `null` measurement. Run ids match as
  whole tokens (E1 does not match E10 or E1b). Tests ran against a throwaway memory copy; the real LOOP_MEMORY was not
  touched. Fixed: the measurement check accepted any parseable JSON, so `null` or `{}` passed.
- Hook (`.claude/hooks/contract_guard.py`), behaviour checked in a scratch git repo with real staged and modified files:
  docs/ outside docs/contract/ staged: denied; a measure-script change without the words "correction" and "verifier":
  denied, and with them: allowed; a multi-file pytest run: denied; the same command after 3 identical failures:
  denied; an unrelated command: allowed. (An earlier battery of mine used files that were not staged or modified and
  so tested nothing; it is not counted.)
- HOLE 1, not small: the hook is NOT installed. `.claude/settings.json` does not exist in this repository, so none of
  the 4.6 hook rules is enforced in any session here. The install snippet is in the hook's docstring. Installing it
  changes what every session may run, so it is the owner's call; until then, 4.6 compliance rests on discipline.
- HOLE 2, inherent to a file check: the gate proves that a verifier file, a recording and a measurement object exist,
  not that they are right. The verifier file is any non-empty file; the measurement is any non-empty object; the
  KEEP/REVERT line is written by the agent that ran the experiment, so the gate cannot tell a derived decision from an
  asserted one. Only the process (a fresh verifier reading the evidence, as the scorers did for B2 to E5) catches this.
- HOLE 3: nothing forces a "done" claim through the gate. No Stop hook checks for it; a plain-text "done" is not
  blocked. The gate is enforced only when someone runs it.

