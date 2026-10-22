# Hands-on, 2026-10-20

Driven only through the Retinat MCP tools (`retinat_open`, `retinat_click`, `retinat_look`, `retinat_zoom`,
`retinat_reflex`, `retinat_wait`, `retinat_record`), browser config at the 960x540 game viewport.

## 1. A second cold boot (01:42 UTC)

The container had booted 20 s before the first call, and Retinat 12 s before it. The first `retinat_open`
(`https://commons.wikimedia.org/wiki/File:Big_Buck_Bunny_4K.webm`) opened on the first call in **10 s**, and the MCP
server connected in 6 s. On the boot before (23:42) the same first open took 49 s and the connect 88 s: how cold a
boot is depends on the layer under the VM, not on the boot itself.

## 2. MR Racer (Poki), Endless, Farm Land: 109 -> 657

The owner's test of real-time vision is playing games for the highest score. All driving here is by reflex rules,
which read the screen every frame (at 480 px wide, a picture every ~100 ms) and hold arrow keys; I only started each
run, looked in, and changed the rules between runs.

| Run | Rule | Score | What happened |
|---|---|---|---|
| calibration | hold Up, a lane right at 9 s | 122 (0.38 km) | rear-ended a car ahead; v3 was installed a moment before the crash. The box above our roof read 0 -> 5 -> 42 -> 74 over the 3 s before it |
| v3 (`mr-racer-rule-v3.js`) | if the car ahead is close (more than 15 dark pixels above our roof), move into a lane only if its box ahead and its box beside us read clear, else brake | 163 (0.47 km) | its moves were 300 ms steering pulses, which left the car straddling a lane line; a slower white car it moved away from clipped our side (`mr-racer-rule-v3-163-dodges-half-a-lane-into-a-car.mp4`) |
| v4 (`mr-racer-rule-v4.js`) | v3, plus **lane keeping by the white lines below the car**, and a dodge that steers until it has crossed a line | 611 at 4.0 km, still running | long stretches pinned to the right barrier at 30 km/h: the road's edge line under the car read as "a line just left of the middle", so the keeper steered right, into the barrier (`mr-racer-rule-v4-lane-keeping-611-at-4km-4x.mp4`, 4x speed) |
| v5 (`mr-racer-rule-v5.js`), swapped in live at 611 | the keeper reads three boxes; a line under the car with no lane line to its right is the road's edge, so it steers left | **657 (4.26 km), new high score** | it crashed within 30 s of the swap (`mr-racer-rule-v5-off-the-barrier-crash-657.mp4`); not looked into yet |

What was measured (offline, on frames of the recordings):
- Lane lines on a band just below the car (y 268-298 CSS px): centred in a lane, at x ~385 and ~693 (the car's
  middle is ~528); straddling, one line at ~515-528.
- The box above the roof (x 500-560, y 110-138) is a usable early warning: a car closing in our lane rose
  0 -> 5 -> 42 -> 74 dark pixels over ~3 s.
- Clear-road levels of the side boxes: right 0-3, left up to ~100 (road texture), so they take separate thresholds.
- The race's "bonnet" view after a crash is the game's crash camera, not a mid-race switch (our car vanishes from the
  frames only at the crash).
- A notice built on `see.frame` came ~50 s late on the results screen, which draws few frames; built on `see.t` it
  is on time.

Also met: Poki's "You've played for 1 hours, Sign in" popup and the game's "watch a video for cash" dialog over the
results. Both closed with their X; no sign-in.

**Why v5 crashed** (offline, its recording at 4 fps): 4 s after the swap, still against the right barrier. Its keeper
saw no line under the car, so it did not steer; a car came up level with us on the left (the box beside us rose
116 -> 214 -> 536 -> 656 over ~1.5 s); the box above our roof crossed 15 with neither side clear, so it braked; ~1 s
later the car on the left hit us. Not the swap: no rule acts on a car alongside.

Later runs (04:45-05:01 UTC):

| Run | Rule | Score | What happened |
|---|---|---|---|
| v6 | a keeper that steered towards the middle from any line under the car | 86 (0.4 km) | pinned against the left barrier: with the left road edge under the car it steered further left |
| v7, swapped in live over v6 | a symmetric keeper (`mr-racer-rule-v7.js`): a line under the car with a lane line on one side only means the road edge is on the other, so it steers away from that edge | 221 (0.81 km) | crashed soon after the swap, from the bad spot v6 had left it in |
| v7, clean run | the same | **720 at 5.8 km, new high score; stopped by hand, not crashed** | 160 at 0.6 km, 615 at 3.2, 686 at 4.4, 720 at 4.8, then 720 to 5.8 km: at 30-50 km/h behind a slow truck the score stopped rising, so points seem to need speed (`mr-racer-rule-v7-720-at-5.8km-6x.mp4`, 6x speed, 2,469 key actions) |

**What this proves and what it does not.** The car is driven by a JavaScript rule that Retinat runs on every frame
of the page. It reads pixel counts in fixed boxes of the screen and holds arrow keys through CDP `Input` events. It
reacts to the traffic actually on screen; it is not a replayed key sequence. But it is **not** the model seeing
each frame: a model turn takes seconds. I looked at frames between runs and rewrote the rule (v3 to v7). The score
therefore measures that loop of reading pixels and rewriting rules, not frame-by-frame AI vision, and the inputs make
no attempt to look human.

**Boot of 04:42 UTC with `uv run --no-sync`:** the Retinat MCP server connected in **4.6 s** and browser-use in
8.0 s. Python started within 1 s of `uv run`, against ~54 s at 03:36. That boot was only partly cold (the first
`retinat_open` took 16 s), so a fully cold boot is still to be checked.

Also seen: a `retinat_record` stop and start sent in one message run in parallel, and the start was refused
("already in progress"). Sent one at a time, they work.

**Next for the racer:**
- a rule for a car alongside: speed up or brake to separate rather than hold;
- the box above the roof also reads the barrier and trees, so the rules brake there for nothing (it held 30 km/h);
- the side box "beside us" misses a car level with us;
- try Two Way once XP level 3 unlocks it.

## 3. A third cold boot (02:42 UTC), and the 90 s connect fixed

The container had booted ~100 s before the round. The first `retinat_open`
(`https://en.wikipedia.org/wiki/Temple_Run_2`) opened on the first call in 47 s; before the fix of 2026-10-19 this
case failed at 30 s. But **the Retinat MCP server took 93.5 s to connect** (88 s on the boot of 23:42), against the
client's 120 s, and the client's first `initialize` probe timed out.

Why: before answering anything the server imported all of browser-use, 2,241 modules (3.9 s warm). After a boot each
file is read off a cold disk. Answering the handshake needs only the `mcp` package.

Fixed, test-first: the process starts from a front (`browser_use/retinat/front.py`) that answers the handshake and
the tool list from light modules, 902 modules (1.35 s warm), and imports the server proper in a worker thread; the
first tool call waits for it, and its budget counts from when the front took it. Over real stdio, warm: `initialize`
1.37 s (3.09 s importing the server first), `tools/list` 0.01 s, the first call 4.6 s.

Not verified yet: the connect time on the next cold boot. By module count it should fall to roughly 35-40 s.

**The next cold boot (03:36 UTC), with the front:** the connect took **79.7 s** (88 and 93.5 s before), less than
estimated. By the process start times, `uv run` itself ran ~54 s before Python started (03:36:54 to 03:37:48);
Python then answered the handshake in ~27 s (~44 s on the boot before, without the front). `uv run` checks the
environment against the lockfile before running: every installed package's metadata (243 packages, 1,706 files) read
off the cold disk, under a lock on `.venv` that the two servers starting together share. `.mcp.json` now starts both
with `uv run --no-sync` (0.04 s warm against 0.1-0.2 s); `uv sync` after cloning or changing dependencies, as the
setup already says. To verify on the next cold boot.

## 4. A fresh boot (05:42 UTC) with `uv run --no-sync`

The machine had been up less than a minute when the round began. From the client's own log: **Retinat connected in
6.3 s and browser-use in 9.0 s**, against 79.7 s (03:36) and 93.5 s (02:42) on earlier cold boots. The first
`retinat_open` took 17 s, like the 16 s at 04:42; the coldest boots took 47-49 s. So the layer under the VM was partly
warm: the connect is checked on a fresh boot, not on the coldest disk seen.

## 5. Step mode: the model plays, not its rules

The owner asked whether the racer was really me playing. It was not: reflex rules I wrote drove it (section 2). Real-
time games outrun a model's turn, so game benchmarks pause the game while the model thinks (VideoGameBench Lite, Orak,
NitroGen) or step it a few frames per action (the ALE's frame skip). `retinat_step` does that to a web page.

Measured on a local page before building it (CDP `Emulation.setVirtualTimePolicy`, Chromium headless=new):
- paused: `performance.now()`, `Date.now()` and a 50 ms `setInterval` stood still over a second of real time; a key
  sent while paused was delivered; a screenshot took 0.13-0.3 s.
- `requestAnimationFrame` did not stop: 60 callbacks a second, with timestamps on real time (3446 -> 4446 ms while
  the clock stood at 3410). A game timed by those timestamps, or by counting frames, ran on through the pause.
- advancing 500 ms at once ran the timers due (10 of 50 ms) but only 7 frames; stepping 16-17 ms and waiting for a
  frame each time played 500 ms as 30 even frames in 0.5 s of real time.
- a frame from another origin of the same site (as poki.com hosts its games on `*.gdn.poki.com`) stood still and
  stepped with the page. browser-use launches Chrome here with `--disable-site-isolation-trials`, so a frame from
  another site was not in a process of its own and could not be tried.
- virtual time cannot be left: after a pause, detaching the CDP session that set it left the page frozen.
- once a paused page draws nothing, Chrome makes no more frames, and neither a style change nor a screenshot starts
  them again; a 1 px canvas drawn every frame keeps them coming.

So the page's `requestAnimationFrame` is made to wait for the clock to move and to hand out its time, a 1 px canvas
(1% opaque) keeps frames coming, and `resume` moves the clock with the real one from then on. Tests on local games
timed all three ways (`tests/ci/test_clock.py`); without the rAF change, the rAF-timed game moved 1000 ms in a 1 s
pause.

Subway Surfers on Poki is not Unity: its assets are Vite-built JavaScript (`assets/*-hash.js`, a webp sprite atlas)
served from `5dd312fa-....gdn.poki.com`, the same site as the page.

**Found on the way:** after Retinat was restarted with the new tool, Claude Code reconnected in 1.7 s but kept the tool
list of its first connection, so `retinat_step` could not be called. Retinat did not say its tools could change. It
now serves `subscriptions/listen` (the 2026-07-28 protocol Claude Code speaks) and tells each process's first listener
that the list changed, or older clients on their first call. Both are tested over real stdio with the SDK's clients.
In this session the tool still did not show up within the same turn, so whether Claude Code re-lists on the notice
is not yet seen.
