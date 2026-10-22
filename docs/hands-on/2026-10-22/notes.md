# Hands-on, 2026-10-22: a cheaper model plays hard Poki games through Retinat

The owner asked for a cheaper model to play the hardest games for an AI, through this project's tools, on video, with
measured accuracy, and for it to use the eyes rather than screenshots. Sonnet agents played, one game at a time, each
with an expert playbook researched beforehand; I did not touch the games.

## First runs: one still frame per turn (screenshots)

`sonnet-plays-poki-screenshots.mp4` (79 s, 1.3 MB; frames where nothing changed dropped, sped up). Each turn the model
saw one still frame of the game (`retinat_step` with `frame:"game"`), the game paused while it thought.

| Game | Result | Obstacles cleared (its log, checked against the video) |
|---|---|---|
| Moto X3M | Run 1 crashed on the giant stone wheel; run 2 finished level 1, 3 stars, 11.44 s game time | 8 of 9 |
| World's Hardest Game | Level 1 cleared on the second try; died twice on level 2's wall of dots | 1 of 4 |

Neon Challenge Legends had started when these runs were stopped, as not what the owner asked for: the eyes were not in
the loop. The World's Hardest Game player's own main complaint was that a still frame cannot show which way a dot is
moving.

**Found on the way (fixed, test first):** a game opened in the same tab after another was played and resumed drew no
frames, so every look hung until the client gave up (the keep-alive beat now comes with each new page; a capture now
gives up); `retinat_wait` refused a plain wait; the first player thought for up to 16 minutes a turn (later runs use
medium effort and a "keep turns short" rule).

## What changed: the eyes watch each step

Each step now reads the frames the page drew during it (`browser_use/eyes/moving.py`) and says what moved, which way
and how fast in game time, or that the whole view scrolled, and draws each mover's trail and heading on the picture.
On a test canvas: "2 things moved: #1 moving right at 120 px/s; #2 moving down at 61 px/s" (true: 120 and 60).

## Second runs: with the eyes (first build)

`sonnet-plays-poki-eyes.mp4` (7 min 16 s, 5 MB; title cards with the scores, paused frames dropped, play sped up). Same
protocol, a 60-turn cap per game, the eyes on. Accuracy is obstacles cleared over obstacles met, from each model's log,
checked against the footage.

| Game | Result | Obstacles cleared |
|---|---|---|
| World's Hardest Game | Level 1 with 0 deaths; level 2: 7 dot columns and the coin, then died twice | 12 of 15 (80%) |
| Moto X3M | Level 1 finished, 12.97 s game time | 6 of 6, one more unclear |
| Neon Challenge Legends | 97% of level 1, 46 coins, no deaths; the turn cap came ~100 px before the finish | 4 of 4 |
| QWOP | 1.7 m in 59 turns, never fell; no running gait, a kneeling scoot; the 50 m hurdle not reached | 29 of 29 strides survived |
| Level Devil | Stage 1 cleared turn by turn; the door's splash hung the page; stages 2-3 played live in a new tab: stage 2 cleared, stage 3's sliding pit killed it 6 times | 3 of 4 distinct traps, stages 2-3 not on video |

Totals: 54 of 58 (93%), 29 of them QWOP strides; without QWOP 25 of 29 (86%). The 98% asked for was not reached, and
QWOP's survival count flatters a run that covered 1.7 of 100 m.

What the models said held them back, and what was done about it (each fixed test first):

- Moto X3M: a turn with the eyes took 4-10 s, so the model often turned them off. The eyes now read a turn from the
  compositor's video stream (`Page.startScreencast`) instead of a capture per frame: about 1 s a turn.
- World's Hardest Game: the eyes' speeds read 0.76x of what the dots did. They were in the eyes' working pixels, not the
  picture's; they are in the picture's pixels now.
- Neon: the scrolling world came out as a crowd of movers. The eyes now say the whole view scrolled (phase correlation
  frame to frame, refined first to last, so a tiled floor cannot alias it), and group things moving together.
- Level Devil: the door's splash waits for the clock in a loop that never yields; in step mode the clock moves only
  between scripts, so the page hangs. Retinat now spots this, stops the script and says to play that part live. Live
  play had only taps: `retinat_key` now holds keys (`hold_ms`), several at once (`with`, a running jump), letters with
  their key codes. The recorder could not leave the hung tab (stopping its screencast never returned), so the live
  stages are not on video, and stopping the recording ran out of time and left the file unfinished: both bounded now.

Also found: a capture taken between turns showed up in the stream as a frame of another size and was read as the turn's
first frame (only the stream's own frame size is read now); a test of the live view assumed a page that repeats its
colours within the watch never repeats a picture.

## Neural detectors on these games, measured

The owner asked whether a detector (they named "YOLO v28"; the newest Ultralytics family is YOLO26) should give the
eyes their sight. Four were run on the same 50 frames (10 per game, cut from the recordings above), one at a time on
this machine's 4 CPU cores (no GPU), in an environment of their own; one judge per detector looked at every annotated
frame and counted. `detectors-compared.jpg`: the same two frames under each.

| Detector (licence) | Seconds a frame (median) | Player found | Hazards boxed | Usefulness 0-10, per game (WHG, Moto, Neon, QWOP, Level Devil) |
|---|---|---|---|---|
| RF-DETR nano, COCO (Apache-2.0) | 0.23 | 23 of 50 frames | 1 of 122 | 0, 3, 0, 3, 2 |
| YOLO26n, COCO (AGPL-3.0) | 0.07 | 24 of 50 | 2 of 124 | 0, 3, 2, 2, 1 |
| OWLv2 base, open vocabulary (Apache-2.0) | 11.1 | 31 of 50 | 88 of 112 (all WHG dots; no saw, spike or pit) | 6, 3, 1, 0, 3 |
| Grounding DINO tiny, open vocabulary (Apache-2.0) | 14.5 | 46 of 50 | 17 of 123 | 4, 3, 3, 2, 2 |

What this says: detectors trained on photographs (COCO) do not see game sprites (YOLO26n boxed nothing on any World's
Hardest Game frame); the open-vocabulary ones find the player but take 11-15 s a frame here, against ~1 s for a whole
turn. Apart from World's Hardest Game's dots, the only hazards any of them boxed were three saws (labelled "clock" or
"traffic light") and one loose "blade" box over a gear; no spike, and no pit that opens. The motion reader already
follows every World's Hardest Game dot with its speed, and the model playing is itself an open-vocabulary detector
looking at the same picture: what it lacks is time (what moved, which way, how fast), which is what the eyes add. A
detector would have to be trained on these games to earn its place; that is not done.

The five games are being played again on the new eyes; results follow below.
