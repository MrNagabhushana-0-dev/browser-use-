# Hands-on, 13 October 2026: the reflexes and the recorder, first live use, on the T-Rex runner

**Recording:** `t-rex-runner-played-by-reflexes.mp4`, 12 s at 10 fps. It was recorded by `retinat_record` itself (the
tool under test) and shows reflex rules starting the game and jumping a group of three cacti.

**Site:** [wayou.github.io/t-rex-runner](https://wayou.github.io/t-rex-runner/), an open-source (BSD) copy of
Chromium's offline T-Rex game.

**Tools:** `retinat_open`, `retinat_record`, `retinat_reflex`, `retinat_wait`, `retinat_look`, `retinat_key` and
`retinat_console`, all through the Retinat MCP server. There were no scripts and no Playwright. These three tools
were called live for the first time: the MCP tool list in this session now includes them.

## What was tried

1. Record, open the game and look.
2. Write reflex rules:
   - press Space when the game is ready;
   - jump when grey appears just ahead of the dino;
   - notify when the picture stops (a crash) or after a timeout.
3. Wait, look at the result, adjust the rules and repeat.

## What the tools did, and the bugs they showed

| # | Found | Status |
|---|---|---|
| 1 | A look during a recording cost ~2,724 tokens: the recorder's own frame, the whole 1920x1080 viewport as PNG. A look is ~330. Writing the test showed worse: on a still page, a look during a recording returned no picture at all. | **Fixed** (`2516ff5`). Live: a look during a recording is 640x360, ~332 tokens. |
| 2 | A rule could not say "something grey is just ahead". In this game the dino, the ground and the cacti are one grey. `see.locate` gave only where all of a colour sits on average. | **Fixed** (`67f13b1`): `see.locate` and `see.changed` take `{in: [x, y, w, h]}`. A runner test passes 6 obstacles with no hit; with nobody at the keys, 3 or more hits. |
| 3 | `retinat_wait` returned "30 s up" from rules that had already been replaced. The status also reported the old rules' inputs. | **Fixed** (`a3ff316`): new rules start with an empty event queue and fresh stats. |
| 4 | With a recording running, reflex decision to input went from ~2 ms to ~150 ms (in a test: p50 58 ms, p95 96 ms). Every repaint of the 60 fps game was a full-size PNG, decoded on the event loop. | **Fixed** (`63c2d52`): frames are paced to the video's rate, sent as JPEG and decoded in a worker thread. The test now measures p50 1.8 ms, p95 22 ms; live, p50 1.8 ms while recording. |
| 5 | Twice with a recording running, a look showed the game's start screen after the rules had seen it start. | **Not reproduced.** A reload test passes on every variant tried, and I could not show that a freshness check fixes anything, so none was kept. The test stays as a guard (`9a3d6a1`). |

**Measured live on this page (no recording):** 11 key taps, decision to input p50 1.6 ms, p95 2.1 ms.
**With the recording running:** p50 3.8 ms on the start screen, 1.8 ms during play.

## My own mistakes with the rules, not the tools'

- The first jump region was not scaled. Once the game starts, its 600x150 canvas is drawn at 1920x480 (3.2x).
- The first Space came before the game was listening; the browser scrolled the page instead.
- The region began 2 px in front of the dino: too late to clear a cactus at 6 px per frame. 40 px ahead works.

## Still open

- **In two recorded runs the game froze in mid-air at score 49**, just past the three-cactus group:
  - the page's frame loop was alive (a rule still notified);
  - it had focus and was visible;
  - its console was empty;
  - Space neither resumed nor restarted it.

  Without a recording the game crashed normally. This is unexplained; it may be this copy of the game. It is not
  reproduced in a test.
- **The rules know nothing about the game's speed.** They estimate it from the frame count. A reflex that could read
  how fast an object moves (two `locate`s a few frames apart) would be the next step for games.
