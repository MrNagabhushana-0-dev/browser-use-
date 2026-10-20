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

**Next for the racer:**
- why v5 crashed (its first 30 s off the barrier);
- the box above the roof also reads the barrier and trees, so the rules brake there for nothing (it held 30 km/h);
- the side box "beside us" misses a car level with us;
- try Two Way once XP level 3 unlocks it.
