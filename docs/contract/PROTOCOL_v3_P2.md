# PROTOCOL v3: the P2 baseline on the owner's Chrome, and the ground-truth method v2b shares

Declared 2026-10-22, before any v3 measurement (LOOP_MEMORY entry "PROTOCOL v3 DECLARED"). Changing a step to move a
number is B8. A change to this protocol is a new LOOP_MEMORY entry, made before the run it applies to.

## 1. Purpose and comparability

v3 measures the product as the owner will use it: Retinat and its relay on loopback on his laptop, the bridge
extension in his own Chrome, one shared YouTube tab, his CPU and network. Its first run is the P2 baseline.

| protocol | browser and capture | stimulus | runs |
|---|---|---|---|
| v2 (P1) | container Chromium, CDP direct | local file (127.0.0.1:8765, bbb480.webm, first 190 s) | B1, E2 |
| v2b (P2 path in the container) | container Chromium + bridge extension (--load-extension), Xvfb | same local file | B2, E3-E5 |
| v3 (P2 on the owner's Chrome) | his Chrome + bridge extension (Load unpacked) | YouTube, his network | baseline first |

Numbers are not comparable across protocols. The capture path differs (CDP screencast vs the extension's tab), the
machine differs (container CPU vs his laptop, where the percept's models run), the player differs (local file vs
YouTube's adaptive stream, ads, buffering), and the recording differs (size, how frames arrive). Each number carries
its protocol tag. Across protocols, show both numbers side by side; never report a delta between them or base a keep
decision on one.

v2b and v3 share everything from section 5 on: measurement scripts, ground-truth steps, LABELLER and SCORER prompts,
and the result block. v2's verifiers labelled and scored in a single agent, so v2's C1/C2 are reference only.

## 2. Stimulus

- **Candidate:** Blender Foundation's official Big Buck Bunny upload on YouTube, id `aqz-KE-bpKQ`.
  UNVERIFIED-EXTERNAL: the id comes from memory. YouTube refuses this container's network, so the uploader, the edit
  and the length are unchecked. On the day, before recording, the operator writes down what the page shows (channel,
  title, length). The run's entry logs these as MEASURED (seen on the owner's screen).
- **Watched:** film 0:00 to 3:10 (the first 190 s), as in v2/v2b. Captions are off: the player's CC toggle, which the
  owner sets (checklist step 4). Quality stays at the player's default; no other setting is touched.
- **Exact cuts:** `tests/ci/assets/big_buck_bunny/edits.json` was built from bbb_sunflower_1080p_30fps_normal.mp4.
  Per its README, the Commons 4K copy has the same edit. It applies to v3 only if `align` says ALIGNED on the v3
  recording (at least 80% of edit cuts within 0.2 s at one offset). If it does not, label by rec_s only, report
  cuts_reached_model as n/a, and say why in the entry.
- **Replace the candidate** if it is unavailable, region-blocked, needs sign-in, is not this film, or fails to align
  because its edit differs. A replacement must:
  - be visually busy, with clear discrete events (at least 20 hard cuts or entrances/exits in 190 s, not one long pan);
  - have no burned-in captions and no on-screen text narrating the action, with captions off;
  - run at least 190 s;
  - play without sign-in;
  - be an official upload.

  A replacement starts a new series (v3', with its own baseline, labels only).
- **Ads:** if one plays, let it finish. No skipping and no blocking.
  - A pre-roll only shifts the offset, and `align` finds the shift. If the ad ran over 60 s, pass `--max-offset`
    above its length.
  - A mid-roll inside the 190 s breaks the single offset. `align` shows this: the match rate falls under 80%, or the
    residual thirds drift apart. Then label by rec_s, label the ad's start and end (appear/disappear "ad starts/ends"),
    and exclude the ad's span from C1 and C2, reported.

## 3. Setup

- The owner's steps are `docs/contract/P2_MORNING_CHECKLIST.md`: install, load unpacked, self-test, open the video
  paused at the start with captions off, share that tab, start `claude --remote-control`. This protocol adds nothing he
  has to do.
- Only the designated tab is shared, and the run acts only in it. No other tab, window, bookmark, history entry,
  cookie, setting or extension is read or touched.
- Every action is visible and reversible. Chrome's debugging bar and the extension's pill show while the AI acts, and
  the checklist's Undo section stops any of it. In the tab, the run only plays the film and pauses it at the end. It
  makes no navigation and no clicks outside the player.
- The measured commit is the checkout's HEAD on the laptop (`~/retinat`, the branch as the setup cloned or pulled it),
  logged in R0; nobody edits that checkout during the run, so no worktree pin is needed there. Retinat is not
  restarted between the two arms: a restart makes the client reconnect, and a cold start can miss its 30 s connect
  timeout (seen in this container at 23:42 UTC on 2026-10-22). Both arms run on the same Retinat process.

## 4. Run procedure (the RUNBOOK the session on the owner's laptop executes)

The owner pastes one line into the Claude Code session he started in `~/retinat` (checklist step 7): "Run the RUNBOOK
in docs/contract/PROTOCOL_v3_P2.md, then report." That session is the operator; it never asks him for anything.

R0. **Preflight (logged):** `retinat_now` names the shared YouTube tab (if no tab is shared, stop and say so);
    `git rev-parse --short HEAD` (the measured commit; pin it as section 3 says); `curl -s 127.0.0.1:9333/bridge/status`
    (extension version, browser); the tab's channel, title and length as seen; captions off; any pre-roll played out.
R1. **Two arms, in this order, each a fresh watcher sub-agent** (Agent tool, model sonnet, background; the same
    Retinat process for both), each starting the film from 0:00:
    - **v3-B**, the P2 baseline: Milestone-1's watch (until=event, narration written by the watcher). WATCHER PROMPT B.
    - **v3-G**, the M2 product: the last KEPT v2b configuration (LOOP_MEMORY's latest KEEP among E3-E6), with live
      narration. WATCHER PROMPT G (gate) if E5 or E6 is kept, else WATCHER PROMPT G-event.
R2. **Measure** each arm (section 5). The token counter is the watcher's own transcript, the newest
    `~/.claude/projects/*/*/subagents/agent-*.jsonl` after the run; the narration log is the newest
    `~/.config/browseruse/narration/*.jsonl`; the gate log is the newest `~/.config/browseruse/eyes/gate-*.jsonl`.
R3. **Ground truth before scoring** (section 6) from v3-B's recording, once for both arms (same video, same seconds).
R4. **Score** each arm with its own SCORER (section 8). Burn v3-G's narration into its recording:
    `uv run --no-sync python scripts/contract/narrated_recording.py <recording> <narration log>`. Flapping pair:
    `uv run --no-sync python scripts/contract/gate_replay.py log <gate log>`.
R5. **Record:** one result block per arm (section 9) appended to LOOP_MEMORY.md; commit and push to
    claude/laughing-hawking-2cj3a6 (no PR). Leave the tab as it is; the owner unshares it when he chooses.

### WATCHER PROMPT B (v3-B: Milestone-1's watch, on the shared YouTube tab)

```text
You are the watcher in a measured experiment. Use ONLY these Retinat MCP tools; load them first with ToolSearch query
"select:mcp__retinat__retinat_now,mcp__retinat__retinat_look,mcp__retinat__retinat_click,mcp__retinat__retinat_key,mcp__retinat__retinat_watch,mcp__retinat__retinat_record".
Do not use any other tool. The shared tab is a YouTube video; act only inside its player.
1. retinat_look. If captions show, retinat_key "c" once and look again. If an ad plays, let it finish: call
   retinat_watch {"seconds": 10, "until": "time"} until the film itself shows (never skip or block it).
2. retinat_key "0" (to 0:00), then retinat_record {"action": "start", "framerate": 8}, then retinat_key "k" (play).
3. Loop: retinat_watch {"seconds": 12, "until": "event", "hold": false, "detail": "glance"}. After EACH result, BEFORE
   calling the next tool, write in your visible reply (as text, not only in your thinking) one line per visual event
   you see in that result's pictures and timeline, as "[film m:ss] what is seen" (film time from the result). Narrate
   only what the pictures show: never sound, page text, captions or transcripts. Then call the next watch at once.
   Stop looping once a result's "covering ...-Y" has Y of 3:00 or more.
4. retinat_key "k" (pause), retinat_record {"action": "stop"}.
Final answer (your last message), exactly this JSON and nothing else:
{"recording": "<path>", "watch_calls": <n>, "narration": [{"film_s": <seconds>, "text": "<what was seen>"}, ...]}
```

### WATCHER PROMPT G (v3-G: live narration, event gate; E5/E6's watch)

```text
Same as WATCHER PROMPT B, with step 3 and 4 replaced by:
3. Live narration loop. First call: retinat_watch {"seconds": 30, "until": "gate", "hold": false, "detail": "glance",
   "narrate": true}. Every later call: retinat_watch {"seconds": 30, "until": "gate", "hold": false, "detail":
   "glance", "narration": "<lines>"} where <lines> narrates the PREVIOUS result: one line per visual event in that
   result's pictures and timeline, as "[film m:ss] what is seen", lines separated by newlines. Pictures only: never
   sound, page text, captions or transcripts. Write the narration straight into the next call; do not write it
   anywhere else. Stop looping once a result's "covering ...-Y" has Y of 3:00 or more.
4. retinat_key "k" (pause), then retinat_record {"action": "stop", "narration": "<lines narrating that last result>"}.
Final answer (your last message), exactly this JSON and nothing else: {"recording": "<path>", "watch_calls": <n>}
```

### WATCHER PROMPT G-event (v3-G if neither E5 nor E6 is kept: E4's watch)

Same as WATCHER PROMPT G with `"seconds": 12, "until": "event"` in both watch calls.

## 5. Measurement (outputs kept verbatim)

```bash
uv run --no-sync python scripts/contract/measure_watch.py <watcher transcript .jsonl> [--edits tests/ci/assets/big_buck_bunny/edits.json]
uv run --no-sync python scripts/contract/measure_narration.py <narration .jsonl> --transcript <watcher transcript .jsonl> [--edits tests/ci/assets/big_buck_bunny/edits.json]
```

Pass `--edits` only when `align` said ALIGNED and the player's clock matches the edit list's clock. Check this before
trusting cuts_reached_model: three cuts the watch results report should each fall within 0.7 s of an edit cut.
Otherwise leave `--edits` out and report n/a. Save each printed JSON as a file and paste it into the entry, with the
command line and the scripts' commit.

## 6. Ground truth, before any scoring

The order is fixed. The SCORER does not start until the labels hash is in LOOP_MEMORY. The operator runs the
scripts. The LABELLER and the SCORER are two fresh sub-agents, and neither ran the watch.

```bash
REC=<recording.mp4>; GT=<evidence dir>/gt; EDITS=tests/ci/assets/big_buck_bunny/edits.json
G="uv run --no-sync python scripts/contract/ground_truth.py"
$G cuts   $REC --out $GT/cuts.json                                   # objective cuts, recording seconds
$G align  $GT/cuts.json --edits $EDITS --film-to 190 --out $GT/align.json   # --max-offset N after a long pre-roll
$G sheets $REC --out $GT/sheets --cuts $GT/cuts.json                  # 4x3 sheets, a tile every 0.5 s
$G template --out $GT/labels.json --recording $REC --stimulus "<id as seen>, film 0-190 s" [--offset <offset_s if ALIGNED>]
#   LABELLER (section 8) fills labels.json and runs freeze until it passes
$G freeze $GT/labels.json                                            # prints labels_sha256 and a LOOP_MEMORY line
```

Then append the printed line, with the labels' sha256, to LOOP_MEMORY as its own entry, and only then start the
SCORER. `freeze` clears the file's write bits, but root ignores them; the logged hash is what guards the labels.

**What the tool does on known recordings** (MEASURED, `script_version` 01644c5f2e87; detector tuned once, on E2;
B1 held out):

| recording | offset found | edit cuts matched within 0.2 s | residual median / max | edit cuts with a detection within 0.7 s | detections not on an edit cut |
|---|---|---|---|---|---|
| E2 (v2) | 5.442 s (a frame-matching verifier found 5.40 +- 0.05) | 33/34 | 0.042 / 0.158 s | 34/34 | opening fade from black (film ~0); a foreground object leaving the frame (film ~106.4) |
| B1 (v2) | 4.692 s | 30/34 | 0.092 / 0.200 s | 34/34 | the same two |
| B2 (v2b, bridge) | 6.412 s | 34/34 | 0.050 / 0.155 s | 34/34 | the same two |
| E2 against the mirrored edit list (control) | NO ALIGNMENT | 9/34 | | | |

Chance level is about 8 or 9 of 34 at any offset (the runner-up offset). Every edit cut that missed 0.2 s had a
detection 0.2 to 0.7 s away. The causes were B1's frozen recording (4.0 s of identical frames before film 39.77's cut)
and frames arriving about 4 a second in an 8 fps file. Cut timing in these recordings is therefore good to about
0.25 s. The labeller judges frames, not the detector.

## 7. C1 and C2 (as LOOP_MEMORY defines them)

- **Watched span:** the union of the watch windows (measure_watch's film covered). In recording seconds it is film +
  offset_s when ALIGNED; otherwise use the scorer's own offset (SCORER step 3).
- **Narration:** the watcher's text. That is its per-result narration (the narration log, from E3 on) and its final
  answer. A line's time is the film time it states; if it states none, the window of the result it answers.
- **C1** = narrated lines in the span that the recording shows true / narrated lines in the span. A line the scorer
  cannot confirm counts as not true and is listed with the reason.
- **C2 strict** = labelled events in the span that no narration line within +-3 s describes / labelled events in the
  span. This is the share missed.
- **C2 lenient** = labelled events in the span with no narration line at all within +-3 s / labelled events in the
  span.
- Answers may be late; evidence must not be missed. A line's lateness counts in B, not in C2.
- An ad span is excluded from both C1 and C2, and reported.

## 8. Prompts (verbatim; fill only the <...> slots)

### LABELLER

```text
You are the LABELLER for a screen recording of a film played in a browser tab. You label what happens on screen.
You have not seen, and must not look for, anything a watcher wrote about this recording.

You get: the recording <REC>; contact sheets in <GT>/sheets (index.json lists them; each tile is stamped with its
recording second; a red bar marks a cut that a frame-difference detector found since the previous tile); the label
file <GT>/labels.json, made by `ground_truth.py template`. Open nothing else: no transcripts, narration logs, measure
outputs, docs/contract/LOOP_MEMORY.md, chats or other agents' files.

Fill <GT>/labels.json with every event from the first film frame to film second 190 (recording second offset_s + 190
when offset_s is a number, else the end of the film in the recording), one entry per event, in rec_s order:
- cut: a hard cut to a different shot. rec_s = the first frame of the new shot.
- action: a discrete action within a shot: a jump, fall, hit, throw, turn to camera, an object breaking or landing.
  Continuous motion counts once, at its onset; a new action counts again.
- appear / disappear: a character or object entering or leaving the frame; an ad's start or end, if one plays.
- text: on-screen text appearing (a title, an ad overlay).
- sound: only if the recording has an audio track (ffprobe lists one); these recordings normally have none.
Granularity: a distinct change that a viewer of the 0.5 s sheets would call one. If unsure whether it is one event or
two, make it one and say so in `what`. Do not label slow camera moves inside a shot.
Timing: read times off the sheets, then check every cut and every doubtful event on the frames, e.g.
  ffmpeg -v error -ss <t-1> -i <REC> -t 2 -vf fps=8,scale=480:-1 <your scratch dir>/f_%03d.png
(8 fps is the recording's rate; rec_s is to 0.125 s). The detector's marks are hints: check each. Some are not cuts
(a fade from black, a fast action, a foreground object leaving the frame), and a run of identical frames (a frozen
recording) can hide a cut that then shows late. Mention a frozen stretch you notice in the next event's `what`.
film_s = rec_s - offset_s to 3 decimals when offset_s is a number, else null. Set span_rec_s to what you labelled.
`what` = one plain line of what is seen, specific enough to check a narration line against.
Set labeller to your agent id. Do not change stimulus, recording, offset_s, made_before_scoring or recording_sha256.

When done, run: uv run --no-sync python scripts/contract/ground_truth.py freeze <GT>/labels.json
Fix whatever it reports and run it again until it prints labels_sha256. Return its output verbatim and your event
counts by kind. Do not edit labels.json after it has printed a hash.
```

### SCORER

```text
You are the SCORER for one watch run. A separate labeller labelled the recording before anyone scored it, and its
label file is frozen. Compute C1 and C2 exactly as defined below, and try to refute every number you are given.

You get: the frozen labels <GT>/labels.json, whose sha256 was logged in LOOP_MEMORY as <HASH> at <time>; the recording
<REC> and sheets <GT>/sheets; <GT>/cuts.json and <GT>/align.json; the watcher's transcript <T.jsonl> and narration log
<N.jsonl> (if any); the measure outputs <M.json>; scripts/contract/measure_watch.py, measure_narration.py and
ground_truth.py.

1. Run `sha256sum <GT>/labels.json`. If it is not <HASH>, stop and answer only "LABELS CHANGED".
2. Narration lines: every line of the watcher's narration (per-result narration in the log or transcript, and its
   final answer), each at the film time it states, else the window of the result it answers. Watched span: the union
   of the watch windows ("covering a-b" in the watch results).
3. Time base: film_s = rec_s - offset_s from align.json when it says ALIGNED. Otherwise estimate the offset yourself
   from at least 5 unambiguous events and report it with its spread.
4. C1 = narrated lines in the watched span that the recording shows true / narrated lines in the span. Check each
   line on the frames at its time +-1 s (ffmpeg -ss <t> -i <REC> ...). A line you cannot confirm counts as not true;
   list each with why (wrong, motion read into a still, not visible, frozen recording).
5. C2 strict = labelled events in the span with no narration line within +-3 s (film time) that describes that event
   / labelled events in the span. C2 lenient = labelled events in the span with no narration line at all within +-3 s
   / labelled events in the span. Use the frozen labels as they are. List labels you think wrong or missing under
   disputes, and also give C2 with your disputes applied, marked as such. Exclude an ad span (labelled ad start/end)
   from C1 and C2, and report it.
6. Refute: recompute A_unique, B (lower bound median, p90, n), gaps and cuts_reached_model from the transcript with
   your own code and compare with <M.json>; check align.json on the frames at three matched cuts and at every
   unmatched one; look for narration that copies page text, captions or titles instead of reading pixels; check
   whether narration came after each result or in one batch at the end; check the labeller's cut events against
   align.json's pairs (cut recall and precision against the edit list, when ALIGNED).
7. Answer with one JSON object: {labels_sha256_ok, offset_s, offset_source, watched_span_film_s,
   C1: {true, n, value, not_true: [{film_s, line, why}]}, C2_strict: {missed, n, value, missed_events: [...]},
   C2_lenient: {missed, n, value}, by_kind, disputes, C2_with_disputes, recomputed: {...}, refutations: [...],
   verdict: "CONFIRMED" | "REFUTED: <reason>"}, followed by the code you ran.
```

## 9. Evidence rules (publication grade)

- **Raw files kept**, in the run's evidence directory with their paths in the entry:
  - the watcher transcript JSONL and the narration log;
  - the recording and its sha256;
  - cuts.json, align.json, sheets/index.json and labels.json;
  - the labeller's and the scorer's transcripts;
  - every command's output, verbatim.
- **Versions:** the product commit measured (pinned worktree), the scripts' commit, and the `script_version` that
  every ground_truth.py output prints.
- **Provenance on every number:**
  - MEASURED: command and output attached.
  - VERIFIED: a fresh agent reproduced it with its own code.
  - INFERRED: reasoning, saying from what.
  - UNVERIFIED-EXTERNAL: from memory or a third party, not checked here.
- **Append-only:** a logged number is never edited, and corrections are new entries. The labels hash is logged before
  scoring, and a scorer that finds the labels changed stops.
- **Result block** (contract 9.5), one per run:

```text
RESULT <run id> (<date>)
baseline:      <run id compared against | none: first run of this protocol>
change:        <the one declared change | none>
commit:        <product commit measured, pinned worktree>; scripts at <commit>
protocol:      v3 (docs/contract/PROTOCOL_v3_P2.md at <commit>)
stimulus:      <YouTube id; channel, title, length as seen> ; labels <file> sha256 <hash> (logged <time>, before scoring)
A:             A_unique <n>/min [MEASURED measure_watch.py]
B:             lower bound median <s> / p90 <s> (n=<n>); B_narr median <s> / p90 <s> [MEASURED]
C1:            <true>/<n> = <v> [VERIFIED scorer <agent id>]
C2:            strict <missed>/<n> = <v>; lenient <missed>/<n> = <v> [VERIFIED scorer <agent id>]
flapping pair: <4-state vs single-threshold transitions/h, onset clipping> [MEASURED gate_replay.py] | n/a (<why>)
verifier:      <CONFIRMED | REFUTED: reason> (labeller <agent id>, scorer <agent id>)
recording:     <path> sha256 <hash>
```
