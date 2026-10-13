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
| 5 | Twice with a recording running, a look showed the game's start screen after the rules had seen it start. | **My mistake, not the tools'.** Both looks were sent in the same batch as `retinat_wait`, so they ran at once, at the game's start (see "Correction" below). Two other looks, sent in order, were stale for real; their three causes are fixed (see "Night"). |

**Measured live on this page (no recording):** 11 key taps, decision to input p50 1.6 ms, p95 2.1 ms.
**With the recording running:** p50 3.8 ms on the start screen, 1.8 ms during play.

## My own mistakes with the rules, not the tools'

- The first jump region was not scaled. Once the game starts, its 600x150 canvas is drawn at 1920x480 (3.2x).
- The first Space came before the game was listening; the browser scrolled the page instead.
- The region began 2 px in front of the dino: too late to clear a cactus at 6 px per frame. 40 px ahead works.

## Still open

- **In two recorded runs the game seemed to freeze in mid-air at score 49.** Explained later: the game had
  crashed normally, and the looks were stale (see "Night: the stale look").
- **The rules knew nothing about the game's speed**; they estimated it from the frame count. Since `7844340`,
  `see.track` gives a colour's velocity in px/s. It is not yet used live on this game.

## Later: Wikipedia's main page and the Commons player, with the new `find` crop

**Recording:** `wikipedia-panel-and-commons-find.mp4`, 78 s, made by `retinat_record`.

**Tools:** `retinat_open`, `retinat_look`, `retinat_find` and `retinat_record`.

| # | Found | Status |
|---|---|---|
| 6 | On Wikipedia's main page, "text that appeared" listed the Appearance panel once per nesting level: "Text Small Standard Large" and again "Small Standard Large", and the same for Width and Color. The script builds the panel 2.3 s after load, and the block and the block inside it were each reported. | **Fix written and its test written** (the outermost changed block says it once). Run after the full suite. |
| 7 | `find "play video"` on the Commons player: ~665 tokens before. | **Checked live** after `7844340`: the crop is shrunk to fit, 0.6x, **~231 tokens**. A small match is still magnified: "Welcome to Wikipedia", 1.7x, ~156 tokens. |

The fundraising-style banner ("Photograph a historic site...", Wiki Loves Monuments) is still reported as text that
appeared, 2.7 s after load. It is a real late banner, so that is right.

## Later still: the Commons film with the new shot detection and names

**Recording:** `commons-film-title-card-its-own-shot.mp4`, 48 s. It was made by `retinat_record`, then shrunk to 960
px wide.

**Tools:** `retinat_open`, `retinat_record`, `retinat_click` and `retinat_watch`, for 30 s of the film.

**What the eyes reported:**
- **The item:** one item, `video "Big Buck Bunny 4K.webm" 1920x1080, 10:34.6 long`. It is named by the player
  dialog's title and sized once its metadata was in (`ed8387c`, checked live). Yesterday it was "size unknown" with
  no name.
- **Shots:**
  - the title card is its own shot (0.6-3.7 s, keyframe at 1.0 s), no longer merged with the clouds;
  - then the clouds, the hill, the stream and the bird: 5 keyframes, all correct;
  - ~767 tokens for 30 s.
- **The page's own text** as it loaded was one line (6 blocks). Readouts were folded.

**Not as hoped, minor:**
- The title-to-clouds change is labelled `cut->`, not `fade->`. At the compositor's 6 fps a one-second dissolve is a
  few large steps, so the cut detector takes it first. It is still the right split.
- A 0.1 s first "shot" comes from the compositor's first sample.
- "on screen" for the item is the player's control text ("0:00 / -:- Loaded 3.32%"), since the name now carries the
  title. Controls could be left out of "on screen".

## Night: the stale look (three causes, and a mistake of mine)

**Recordings:**
- `t-rex-recorded-to-game-over.mp4`, 14 s, made by `retinat_record`. The rules start the game and jump a
  three-cactus group, the game crashes at 60, and the video ends on the real GAME OVER screen.
- `t-rex-in-strict-order-ends-on-game-over.mp4`, 32 s: the check, in strict order, described at the end.

Twice, a look sent after `retinat_wait` had returned showed an earlier frame of the game:
- at 23:52, a mid-run frame, after the rules had seen the picture stop;
- at 00:21, the dino in mid-air at 49, in two looks 30 s apart with Space pressed between them.

Each cause below was reproduced in a test that failed first:

| Cause | Fix |
|---|---|
| **The page view starved.** The eyes' page view and the recorder share one screencast and one frame slot. A recording started after a look took both; one stopped while the view shared it ended the stream. | `ecac0a8`: the view notices it is out of step and starts again in step. Both orders are tested. |
| **The recorder lost the final repaint.** Chrome's screencast drops the repaint that ends an animation: it is skipped while frames are in flight, and a page that then stands still sends no other. Full-size frames are in flight most of the time; the eyes' 640 px stream was not affected (3/3). | `5a16efe`: a quiet recording is topped up with one capture. The video's last frame after an animation is tested; it failed about half the time before. |
| ~~**A stale stream that was not quiet.**~~ **Withdrawn the next morning.** The test that showed it (failing 5 of 6 on `5a16efe`) had a stop rule that fired mid-animation: a hue drifting 3.3 degrees a frame never crosses `changed()`'s threshold, so "still for 45 frames" came 0.75 s into a 2 s animation, and a look taken then was right to show it mid-way. With a bar sweeping the canvas, the test passes 6 of 6 on `5a16efe`. | `cfa7e96` (a look captures the page at that moment and says so) is kept as a design choice, not a fix. |

The game "freezing in mid-air, with Space doing nothing" was these stale looks: the game had crashed normally, and
Space had in fact restarted it.

### Correction: most of the stale looks that night were mine

Re-reading the call log showed the problem. From 23:53 on, and in every run from 01:54 to 02:38, I sent
`retinat_look` in the same batch as `retinat_wait`. The client ran them together:
- the look came back within a second, at the game's start;
- the wait came back about 10 s later, when the game ended.

Those looks showed the start screen because that was the screen. I took them as the bug still there:
- as a fourth sighting before `cfa7e96`;
- after it, as the fix failing. I then spent an hour on capture variants (a clip at scale 1, no clip, a capture
  Chrome scaled down). They all "still showed the start screen", for the same reason, and that change was dropped.

What stands:
- causes 1 and 2 rest on the two looks sent in order, and on their failing tests;
- cause 3 rested on its test alone, and the next morning that test turned out to be wrong (its stop rule fired
  mid-animation; fixed, it passes on `5a16efe` 6 of 6). Cause 3 is withdrawn: no live look and no sound test shows it.

**Checked live in strict order** on the committed code, at 02:44:
1. a look;
2. record;
3. rules;
4. a wait, which fired at 30.4 s;
5. then a look: GAME OVER at 55.

The recording ends on GAME OVER too.

**The tools now catch this mistake.** A seeing tool asked while a `retinat_wait` is pending says "asked while a
retinat_wait was still waiting". The wait's own description says to look after it returns.

## Early morning: rules that read what the page cannot (`09d103a`)

**Recording:** `commons-film-rules-read-on-screen.mp4`, the first 60 s at 640 px. It was made by `retinat_record`
and shows the Commons player opening and the film playing while the rules read it.

**Why.** Reflex rules read pixels in the page. On Commons the film streams from another origin without CORS, so the
page may not read it, and every `see.locate` or `see.changed` on it came back empty: rules never fired. A game in a
frame from another site (itch.io serves games from `html-classic.itch.zone`) was out of reach of every reading.

**What changed.**
- **The screen as the compositor draws it is fed to the rules.** Python sends a 240 px picture of the viewport about
  20 times a second, and only once a reading asks for it.
- **A source the page may not read is read on screen, within its box.** That covers a tainted video or canvas, with
  no change to the rule.
- **`{from: 'screen'}` reads everything**, frames from other sites included.

**Measured in tests** (six runs):
- a cross-origin clip's colour and cut fired at media time 1.99-2.04 s (truth 2.0 s);
- a square drawn in a cross-origin frame was clicked 16-51 ms after it was drawn.

**Live on the Commons film** (`retinat_open`, `retinat_find`, `retinat_click`, `retinat_reflex`, `retinat_wait`):
- A rule `see.changed({from: 'video'}) > 0.35` fired at video times 7.23 s, 9.41 s and 9.93 s, with no errors. Before
  this change it could not fire at all.
- **These were not checked against the film's cuts.** The earlier watch of this film called 3.7-16.1 s one calm shot,
  so these are probably camera motion crossing my threshold, not cuts. The threshold was mine.
- **Live, a screen picture was slower:** 153 ms p50 per picture, 21 pictures in all, against ~50 ms in the tests.
  - It is not the eyes' own compositor loop competing: in a test, the feed took ~78 ms with and without it.
  - Capture time grows with the page's rendering load: ~50 ms on a simple clip, ~80 ms on a busy 720p test pattern,
    ~150 ms on Commons' 1080p film, all with software compositing.

## Morning: the screen as the compositor draws it, and overlapping captures (`a30f837`, `b840ca2`)

**Recording:** `commons-film-rules-on-the-screencast.mp4`, 36 s at 640 px, made by `retinat_record`.

**Tools:** `retinat_open`, `retinat_find`, `retinat_click`, `retinat_reflex`, `retinat_wait`, `retinat_look` and
`retinat_record`, through the Retinat MCP server on Wikimedia Commons.

**What changed before this session.** The rules now get the screen from a screencast on a DevTools connection of their
own, not from forced captures. Each picture is ~10 ms old when they read it, against ~50 ms for a capture. Running the
tests many times found that the screencast sometimes sends a broken frame about half a second into a video's playback
(the video missing, or the whole frame black). A frame that replaces a region almost entirely is now confirmed with a
capture: 0 false cuts in 30 plays, where 3 to 4 in 10 had them.

**Live on the Commons film:**
- **Faster:** the same rule as earlier got its pictures 25 ms old at the median, against 153 ms with captures. That is
  about six times fresher, on the same busy page.
- **What fired:** the rule fired at 0.19 s, 1.31 s and 1.63 s into the film. The earlier watch called the title card
  one still shot (0.6-3.7 s). These firings are probably the player's own overlay (the play button and controls
  fading as the film starts), which is inside the video's box on screen. Not verified.

**Bug found: a look showed the whole page as a keyframe of the film.** After the rules ran, a `retinat_look` (a short
watch, as the film was playing) listed extra "cuts" and a keyframe at 8.1 s that was the Commons page, shrunk and
tiled, inside the film's box. A shot was also listed as "0.9s-0.2s".
- **Reproduced** on a white page with a cross-origin red/blue clip, counting the eyes' compositor pictures that showed
  the page instead of the film:

  | Rules | Pictures showing the page |
  |---|---|
  | off | 0 of 29 |
  | capturing the viewport (the capture-loop feed) | 24 of 30, 20 of 29 |
  | on the screencast (captures only to confirm) | 4 and 7 of 28 |
  | captures one at a time | 0 of 29, 0 of 30 |
- **The cause:** two `Page.captureScreenshot` calls in flight on one tab, with different clips and scales, swap or mix
  their pictures. Every capture in the library now holds one lock per browser connection (`b840ca2`). The test
  failed 2 of 2 before and passes 3 of 3.
- **Open:** the "0.9s-0.2s" shot (an end before its start) was not looked into. It probably came from a corrupted
  first picture, but that is a guess.

## Later morning: the eyes and the rules on the film together (`b840ca2` checked live, a new bug fixed)

**Recording:** `commons-film-eyes-and-rules-together.mp4`, the first 50 s at 640 px. It was made by `retinat_record`
and shows the click, the rules going in, and the start of a 20 s `retinat_watch` while they run.

**Checked live:** a rule read the film on screen while the eyes watched it for 20 s.
- All 7 of the eyes' keyframes were the film. Before the capture lock (`b840ca2`), this combination gave a keyframe of
  the whole page, tiled.
- The shot list was sane: no shot ending before it started.
- The title-to-clouds change at 3.9 s now reads `fade->`; earlier today it was `cut->`. Not checked against the film's
  own edit list.

**Bug found:** the same watch dropped its line "picture: from the compositor ... its sound cannot be read". It said
instead "sound: none captured (no audio track, or it had not started)". The rules' stats (374 screen pictures) showed
the film was still unreadable to the page.
- **The cause:** the retina says a video is unreadable once, when it finds out, and a percept looked only at events
  since the watch began. I had installed the rules first, so the watch began after that event.
- **Fixed:** a percept takes this from everything the retina said. A test (play, wait 2 s, then watch) failed first
  and passes now. Eyes and Retinat files: 91/91.

## Late morning: a game in a frame from another site (itch.io)

**Recording:** `itch-io-game-frame-read-on-screen.mp4`, 75 s at 640 px, made by `retinat_record`. It covers the
itch.io listing of open-source HTML5 games, the page of "Pigeon Ascent" (Escada Games), Run game, and the game's title
screen.

**Why:** this is the case `{from: 'screen'}` was built for. itch.io runs games in a frame from `itch.zone`, so the page
cannot read the game's pixels at all. It had not been tried live.

**Tools:** `retinat_open`, `retinat_look`, `retinat_find`, `retinat_click`, `retinat_reflex` and `retinat_wait`.
There was no bot wall, and nothing was signed into.

**What happened:**
- **It works.** A rule watched the game's frame on screen, with its box from `see.el('iframe')`:
  `see.changed({from: 'screen', in: [box]}) > 0.15`. It fired when the game drew its title screen ("Choose your
  pigeon name", FIGHT), and the look showed the same.
- **It was slow.** Pictures reached the rules 132-138 ms old (p50) at first, and 58 ms on the second visit. In tests
  they are ~10 ms old.
  - The stats could not say why, so they now do (below). The second visit was on the screencast, with no confirming
    captures.
  - The game itself was the load. Chrome's renderer ran at 240% CPU of 4 cores rendering it without a GPU, while
    Python sat at 10%. That is a limit of this machine, not of the feed.
- **My own mistake:** I ran a timing test while the game still ran in the Retinat browser, and the test failed on
  picture age (26 ms over a 25 ms bound). It passed once the page was closed. Close what you opened before measuring.

**Changed:** the reflexes' `status` now says where the rules' screen comes from (`screen_source`: screencast or
captures) and how many screencast frames a capture replaced (`screen_confirmed`). An agent can then tell a slow page
from a slow path. Test first: the cross-origin video test requires the screencast. test_reflex, test_retinat and
annotations: 42/42.

## Midday: a news front page (the Guardian)

**Recording:** `guardian-front-page-ad-frame-and-scan.mp4`, two parts joined at 640 px: the session that found the
bug, then the re-check and a scan after the fix.

**Tools:** `retinat_open`, `retinat_look`, `retinat_now` and `retinat_scan`. The Wikimedia API, wanted for a reference
shot list of the Commons film, still answered 429, so I tried it once and moved on.

**Bug found:** the first look on theguardian.com/international said "no video playing on
https://assets.guim.co.uk/commercial/tpc-test/v2/index.html", an advertising frame, not the page.
- **The cause:** browser-use runs Chrome with site isolation off, so frames from other sites live in the tab's
  process. The script that installs the retina in new documents reached them too. An ad frame that loaded after the
  eyes opened ran its own retina, and whichever frame spoke last set the page's URL and what the eyes attended to.
  A frame without a video could drop the page's video from attention.
- **Fixed:** the retina runs in the top frame only.
- **Test:** a playing video, and a frame from another origin added a second after load. It failed first (the page's
  URL alternated with the frame's) and passes now. Eyes file: 69/69.
- **Checked live:** after the fix, `retinat_now` gives the Guardian's own URL.

**The scan** covered 12 of 17.6 screens: 6 frames, ~647 tokens. Two smaller things, not fixed yet:
- Two frames were both labelled "6.4 screens down" while showing different stories. The scan labels every frame
  since its last check with the scroll position read after the wheel, so frames from mid-scroll get the end
  position. Chrome's screencast frames carry their own scroll offset, which would label them exactly.
- From the second screen on, the Guardian's "Who pays for the journalism" banner covers the bottom ~40% of every
  frame, and the scan does not say so.

## Noon: the scan names what covers the screen

**Recording:** `guardian-scan-names-the-banner.mp4`, made by `retinat_record`: a scan of the Guardian's front page.

**Why:** in the last session, the Guardian's "Who pays for the journalism" banner covered the bottom ~40% of every
scan frame, and the scan said nothing. What was under the banner went unseen without a word.

**First try: comparing pixels across frames scrolled apart.** Rows that stayed the same would be fixed. It found
the banner. It also said a plain page of solid colour bands had one, because uniform areas look the same however far
the page scrolls; white margins on real pages would too. Dropped.

**What was kept: asking the page.** At each step, the scan asks which elements are fixed or sticky across most of the
screen's width. A band reported at half the steps or more is said to stay put, with its text. Overlays such as
banners, cookie bars and sticky headers are page elements even on canvas pages.
- **Test:** the band page with a fixed banner 40% tall must get the line, and the same page without the banner must
  not. It failed first; eyes and Retinat files 94/94.

**Live:** "stays put while the page scrolls (what is under it was not seen): the bottom 37% ("The Guardian Who guards
the Guardian? Our readers do. Author")". The frame labels were distinct as well (0.0, 2.4, 4.0, 4.8, 5.6, 5.9 screens
down), after `3bd1501`.

## Early afternoon: the eyes' cuts scored against two whole films

**Recordings**, both made by `retinat_record` on Wikimedia Commons:
- `commons-film-watched-for-the-edit-list.mp4`: the two watches whose shot lists were checked (before the change).
- `commons-film-new-cut-rule-live.mp4`: the same film after the change, watched for 40 s.

**Tools:** `retinat_open`, `retinat_find`, `retinat_click`, `retinat_watch`, `retinat_record`.

**The reference.** upload.wikimedia.org still answered 429 (tried once). Blender's own mirror has the same edit: the
Commons file is 634.6 s, and so is `bbb_sunflower_1080p_30fps_normal.mp4`. Big Buck Bunny and Tears of Steel are
both CC BY 3.0. I built each film's edit list from ffmpeg's scene scores, then checked every candidate by eye: frame
pairs for every score of 0.1 or more, every-frame strips where unclear, and brightness traces for fades.
- **Big Buck Bunny:** 125 cuts, 5 fades (the title, two fades through black, an iris-out, the post-credits fade), and
  one whip tilt left as ambiguous.
- **Tears of Steel** (live action, kept as a second check): 135 cuts in 9.5-588.5 s. The animated end credits are not
  scored, and two in-shot lighting and hologram effects are ambiguous.

**What the watches said, checked.** On Commons, `retinat_watch` reported a cut at 24.9 s that is not in the film: a
bird knocked off its branch in a still shot. The title's fade to lilac and the clouds fading in were right. A watch
asked for 35 s also stopped after 6.7 s ("nothing new for 2s") on a slow pan over clouds, 10 s before the next cut.

**Measuring.** The film is sampled as the eyes sample it, 16x16 luma and 4x4 colour grids, 5 sampling phases.
`area` is area-averaged, as compositor sight reduces a cross-origin video. `point` is nearest-neighbour, my model of
the retina's `drawImage` with low smoothing on a readable video; that model is not checked against Chrome yet. Each
cell is F1 (false changes / missed, per film):

| | Bunny, area 6/s | Bunny, area 10/s | Bunny, point 10/s | Steel, area 6/s | Steel, area 10/s | Steel, point 10/s |
|---|---|---|---|---|---|---|
| before | 0.854 (34.4 / 7.4) | 0.898 (26.2 / 2.8) | 0.843 (40.0 / 6.2) | 0.850 (28.6 / 14.2) | 0.895 (22.8 / 7.2) | 0.870 (25.4 / 11.4) |
| after | 0.966 (2.2 / 6.6) | 0.970 (2.2 / 5.4) | 0.969 (1.6 / 6.2) | 0.926 (2.6 / 16.4) | 0.941 (2.0 / 13.2) | 0.921 (1.0 / 18.8) |

**What changed in `sight.py`:**
- **A cut must be a spike:** its change is at least twice the change just before it and just after. Motion changes
  the picture for several samples running: a head passing the lens, a whip pan, shaky hands.
- **A cut must reach most of the picture:** 45% of the cells change by a cut's worth, counting luma or colour cells.
  Something large leaving a still shot within one sample changes only its own part. That is the bird at 6/s, which
  is still a one-sample spike. Real cuts changed 70% or more of the cells nine times in ten.
- **The old threshold is gone.** It rose with the last 20 differences (median plus 6 MADs), and it missed clean cuts
  right after motion: 218.2, 258.1, 326.6 and 328.1 s in the Bunny.
- **Bug fixed: a hard cut inside a run of small motion was reported as a fade.** At 433.7 s a 102-level cut sat in a
  run of 2-3 level changes. The blend test passed it, because the frames before the cut look like one end and the
  frames after it like the other. It was reported as a fade at the run's middle, 0.27 s late. Now no one step may
  carry more than 60% of a fade's change.
- **The test** scores the whole Bunny film from a committed 490 KB file of grids (`tests/ci/assets/big_buck_bunny`,
  with the attribution) against its edit list. Precision was 0.788 (33 false changes) before the change. The test
  also checks the bird, the cut at 433.7 s and the cut after motion at 258.1 s.

**How it was chosen, plainly.** The spike ratio was chosen on the Bunny before Tears of Steel was looked at. Dropping
the old threshold and adding the spread test were judged on both films.

Tried and dropped:
- **Treating a change across two samples as one cut.** Worse on the Bunny, no better on Steel.
- **Splitting a run where it turns, to catch fades through a colour.** It found the Bunny's title fade but called
  more false changes on both films.
- **A histogram test.** Lost recall.
- **Motion-compensated rejection** (the shift test used for fades). It changed nothing.

**What it costs:** cuts into or out of fast action. The shot beside the cut moves about as much as the cut changes,
so the spike test fails. Steel at 10/s misses 13.2 of 135, up from 7.2.

**Checked live after the change:** cuts at 16.1, 20.3 and 27.3 s, against the film's 16.30, 20.17 and 27.47. There
was no cut at the bird, and 16.1 now reads `cut->` (it read `fade->` before). The title fade was not found this time:
1.3-16.1 s came back as one shot.

**Seen, not fixed:**
- **`until=bored` stopped a 35 s watch of a 10.5-minute film at 6.7 s.** The film is cross-origin, so its sound
  cannot be read. Two seconds of a slow pan give no evidence that a long film has nothing new.
- **The watch header said "watched 78.8s" for a 30 s watch.** "On screen" still showed the controls at "0:00".
- **Two frames of the first recording, around 1:12, show a clipped picture instead of the page:** a small thumbnail,
  then the film full-frame. They are probably compositor sight's clipped captures showing up in the screencast. If so,
  they may also be what the reflex screen feed saw as "broken frames". Not verified.
