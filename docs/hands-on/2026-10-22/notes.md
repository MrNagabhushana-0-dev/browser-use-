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

The games are being played again this way; results and the video follow below.
