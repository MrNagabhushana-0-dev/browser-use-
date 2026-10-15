Reflexes, compositor sight for cross-origin video, a self-configuring TLS proxy CA, and a recorder that keeps time. Every item here was found by driving a real browser hands-on through the Retinat MCP tools, then fixed test-first.

## Reflexes: real-time action without the model in the loop
A model thinks in seconds; a game or live control needs an answer within a frame. This follows the dual-system designs ([Figure Helix](https://www.analyticsvidhya.com/blog/2025/02/figures-helix/): a 7-9 Hz VLM over a 200 Hz policy; [The Latent Bridge](https://arxiv.org/pdf/2606.24470), a slow-fast channel for real-time game agents) and event-gated streaming perception ([StreamMind](https://fugumt.com/fugumt/paper_check/2503.06220v3_enmode)).
- **How it works:** the agent writes rules once (`retinat_reflex`). The page evaluates them every animation frame against what it draws: `see.locate(colour)` on the canvas, `see.el`, `see.text`. On a rule's edges, real key or mouse input goes out over CDP.
- **Waiting:** `retinat_wait` sleeps until a `notify` rule fires, and only after that rule's input has gone out.
- **Measured** in a canvas game (`tests/ci/test_reflex.py`):
  - the paddle stayed under a moving ball in 211 of 211 frames, from one install call;
  - 108 key actions;
  - decision to key arriving: **1.6 ms p50, 2.2 ms p95**, on the page clock.
- **Safety:** the page's own script cannot reach the input channel (tested). A mutant that never releases held keys scores 18/211 and is killed.

## The eyes see cross-origin video
Found on Wikimedia Commons: the player streams from another origin without CORS, so the page cannot read the video, and `retinat_watch` said "no video was on screen" while the film played.
- **Fix:** pictures now come from the compositor, clipped to the video at 6 fps, with the same features and keyframes. The sound is reported as unreadable, not absent.
- **Test:** a second origin with no CORS. All 3 cuts are found within 0.4 s, with keyframes.

## Networks that re-sign TLS configure themselves
In the cloud sandbox every HTTPS page failed.
- **How the CA is learned:** on `ERR_CERT_AUTHORITY_INVALID`, the session reads the chain Chromium was shown and learns a CA only if all of these hold:
  - the environment's own CA bundle holds it;
  - it is a CA;
  - it is not a public root;
  - its key verifiably signed that chain.
- **Afterwards:** the CA is saved for this network. The MCP servers restart their browser and retry once, so the first `retinat_open` succeeds.
- **Tests:** learned and then trusted; a same-name CA with another key is never learned (a mutant without the signature check is killed); one-call success through Retinat.

## Recording
- **`retinat_record`** (start/stop) records the session for the person, after Claude in Chrome's `gif_creator`.
- **The recorder** wrote one frame per repaint, so 3 s became 0.2 s and almost black. It now writes the latest picture at the frame rate.
- **Hands-on video and notes** are in `docs/hands-on/2026-10-12/`: Wikipedia and Commons, with seven findings, three fixed here.

## Hourly loop, round 2: the fixes checked live on Wikimedia Commons
- **A player that switches quality stays seen.** Video.js starts 480p, then switches the same `<video>` to 1080p. Taint was kept per element, so the new item was never reported, and the film came back as 0 items. Taint is now re-tested per source. Through the MCP tools: 1 item, 24 s of the real film through the compositor, 4 correct keyframes, about 870 tokens.
- **`retinat_look`** on a page with a paused video now shows the page; it used to show no picture.
- **`retinat_find`** also matches control names (aria-label, title, alt, placeholder). Commons' icon-only play button is now found as `button named "Play video"`.
- **Ticking readouts** (player clock, buffer %) are folded: a short readout (up to 8 values) is kept whole, and a long one keeps its first value, its extremes and its last.
- **Stopping an MCP server closes its browser** (SIGTERM, SIGHUP, SIGINT or the client leaving). The old code left 8 orphan processes in the test; live, 0 orphans.
- **Reflexes v2:** `see.locate` and `see.changed` read video too, and `see.level` and `see.onset` hear the playing media. Events carry the video's own time. On a generated clip, colour, cut and sound start each fire within 0.2 s of the truth.
- **Hands-on:** two more recordings and updated notes in `docs/hands-on/2026-10-12/`.

## Verification
- Affected files: test_reflex 4/4, test_retinat + test_mcp_tool_annotations + test_reflex 29/29, test_eyes + test_retinat 71/71, test_proxy_ca 10/10, test_action_record 6/6. pyright is clean.
- Round 2 files: test_reflex 5/5, test_mcp_lifecycle 2/2, test_eyes + test_retinat + test_mcp_tool_annotations 80/80, then the new eyes tests.
- **Full `tests/ci` on `a12243d`: red, 2 failed** (1,671 passed, 31 skipped, 35m37s). Both failures were eyesbench retina tasks: a one-tick peak and a carousel slide, lost when ticking readouts were folded. Fixed in `6eb147c`: a readout keeps its first 8 values; after that, rising counters are folded and fluctuating figures are noted at new highs and lows. Eyesbench 8/8, eyes + Retinat 77/77.
- **Full `tests/ci` on `6eb147c`: 1,674 passed, 30 skipped, 0 failed** (34m16s, loop watchdog on, 6 stalls, the longest 2.0 s).
- **Rounds 3-4:** each fix's affected files were run (eyes, eyesbench, Retinat, reflex, live view, recording, MCP lifecycle, Tor, desktop control). The eyes + eyesbench + Retinat files passed 87/87 on `ed8387c`.
- **Full `tests/ci` on `9a3d6a1`: died part-way** (925 passed, 0 failed so far). A desktop-control window closed while it was being read, and Xlib's default handler exited the process on the BadWindow. Fixed in `65f798c`.
- **Full `tests/ci` on `65f798c`: red.** It stopped at its first failure, `test_eyes.py::test_browse_watches_each_reel_once...[/feed]`, after 1,027 passed, while a hands-on session loaded the machine. Alone on HEAD that test passed 18 of 19 runs; it is load-sensitive and still open.
- **Full `tests/ci` on `aec0d20`, quiet machine: 1,424 passed, 29 skipped, 1 failed** (`test_screenrec.py::test_a_recording_is_finalized_into_a_playable_file`). The files from `test_screenrec.py` on, rerun on the same commit: **264 passed, 1 skipped**. Every test in `tests/ci` ran on `aec0d20` (1,688 passed); the one failure is root-caused and fixed in `409482f`, whose new test reproduces it every time on the old code. Screen-recorder file 6/6, three runs, on `409482f`.
- **Round 5 (the stale look):** each fix's affected files were run (eyes, Retinat, reflex, recording, live view);
  test_retinat + test_mcp_tool_annotations + test_reflex 38/38 on `4f31b66`. The feed test passed 8 of 8 alone under
  four busy loops; its failure is not reproduced.
- **Full `tests/ci` on `4f31b66`, quiet machine: 1,694 passed, 31 skipped, 0 failed** (36m26s). Every test in
  `tests/ci` ran in one pass, the feed test included.
- **Round 6 (screen reading, capture lock):** affected files on each slice: test_reflex + test_mcp_tool_annotations
  + test_retinat, the new tests repeated (cross-origin video 6/6, eyes-and-rules 3/3; it failed 2/2 before the lock).
- **Full `tests/ci` on `b840ca2`: red, 1 failed, 1,697 passed, 31 skipped** (37m43s). The failure was
  `test_a_look_after_a_recorded_game_shows_how_it_ended`, and it was the test's own stop rule (above). Fixed in the
  test in `b5069f5`, which changes no library code: 5/5 on HEAD.
- **Full `tests/ci` on `b5069f5`, quiet machine: 1,699 passed, 30 skipped, 0 failed** (36m05s).
- **Full `tests/ci` on `e138b57`, quiet machine: 1,699 passed, 31 skipped, 0 failed** (37m19s).
- **Full `tests/ci` on `fb23f7c`: red, 1 failed, 1,698 passed, 31 skipped** (36m02s). The failure was
  `test_play_arena.py::test_a_game_behind_a_title_screen_gets_started_and_played` ("the title screen was never
  dismissed"). It passed in the three full runs before it and 3 of 3 alone, and the arena uses none of the code
  changed in `fb23f7c` (reflex stats). Not reproduced since: 3 of 3 alone, and 2 of 2 runs with the eyes and
  eyesbench files (81/81 each). Open intermittent, cause unknown.
- **Full `tests/ci` on `13e8b2b`, quiet machine: 1,700 passed, 31 skipped, 0 failed** (35m54s).
- **Full `tests/ci` on `3bd1501`, quiet machine: 1,702 passed, 30 skipped, 0 failed** (36m22s).
- **Full `tests/ci` on `1fe0904`, quiet machine: 1,702 passed, 31 skipped, 0 failed** (36m40s).
- **Full `tests/ci` on `391c814`, quiet machine: 1,704 passed, 30 skipped, 0 failed** (37m02s).
- **Full `tests/ci` on `d622168`, quiet machine: 1,706 passed, 30 skipped, 0 failed** (36m43s).
- **Full `tests/ci` on `1d1feea`, quiet machine: 1,709 passed, 30 skipped, 0 failed** (38m07s).
- **Full `tests/ci` on `4f431c4`, quiet machine: 1,711 passed, 30 skipped, 0 failed** (36m55s). The reflex speed test, which failed once in a smaller combined run, passed here.
- **Full `tests/ci` on `859df30`, quiet machine: 1,713 passed, 30 skipped, 0 failed** (37m14s; a first run was cut short by a container restart and run again).
- **Full `tests/ci` on `c1c0e39`, quiet machine: 1,713 passed, 30 skipped, 0 failed** (37m10s).
- **Full `tests/ci` on `9b90786`, quiet machine: 1,713 passed, 30 skipped, 0 failed** (37m23s).
- **Full `tests/ci` on `c09d801`: 1,712 passed, 30 skipped, 2 failed** (36m57s). The stall test, which that commit had added, failed: labels went backwards. Fixed in the next commit. The other failure was the known feed intermittent (1 in 30 runs with every core busy).
- **Full `tests/ci` on `932e83f` (the label fix), quiet machine: 1,715 passed, 30 skipped, 0 failed** (36m41s).
- **Full `tests/ci` on `ca2fcb2` (reflex events at the frame seen), quiet machine: 1,716 passed, 30 skipped, 0 failed** (36m29s).
- **Full `tests/ci` on `3492b95` (scans show the whole page), quiet machine: 1,718 passed, 30 skipped, 0 failed** (36m34s).
- **Full `tests/ci` on `89df927` (slow pages): first run 1 failed (the play-arena title screen, a known intermittent; 10 of 10 alone and under load), 1,720 passed; second run 1,721 passed, 30 skipped, 0 failed** (38m02s).
- **Full `tests/ci` on `58dad8e` (retinat_scroll), quiet machine: 1,722 passed, 30 skipped, 0 failed** (37m04s).
- **Full `tests/ci` on `76ace3b` (find reads pixels), quiet machine: 1,724 passed, 30 skipped, 0 failed** (38m32s).
- **Full `tests/ci` on `fb6530d` (explorer retries and same-site links), quiet machine: 1,726 passed, 30 skipped, 0 failed** (37m22s).
- **Full `tests/ci` on `8197f1a` (changes: failed requests, rewritten rows), quiet machine: 1,728 passed, 30 skipped, 0 failed** (38m22s).
- **Full `tests/ci` on `bfb2530` (web components, retinat_scroll live): first run 1 failed (the asked-late beeps test, the known beep-count intermittent; 5 of 5 alone), 1,730 passed; second run 1,731 passed, 30 skipped, 0 failed** (39m08s).

## Hourly loop, rounds 3-4: names, shutdown, and the first live use of the reflexes and the recorder
All found hands-on, on Wikimedia Commons and on an open-source copy of the T-Rex runner, then fixed test-first.
Notes and recordings are in `docs/hands-on/2026-10-12/` and `docs/hands-on/2026-10-13/`.

- **The page's own text as it loads** is one line, "page content filled in as it loaded: N blocks", not a list of
  "text that appeared". A toast after load still gets its own line. Live on Commons: 6 load-time blocks became one
  line.
- **A SIGTERM'd MCP server exits** (`b669d69`). It used to close its browser and then hang for good on the stdin
  reader, which no cancellation reaches while the client keeps the pipe open. It now also stops its bridge and Tor,
  and a Tor it launches gets `__OwningControllerProcess`. Live: the server exits 0.2 s after SIGTERM, with its 9
  browser processes.
- **A film is named by its page or its player dialog** (`c0817da`, `ed8387c`), not by its badges. Screen-reader-only
  text is not "on screen". A video attended before its metadata is described again, so its size and length are
  known. Live: `now watching "File:Big Buck Bunny 4K.webm"`.
- **A look during a recording is a look** (`2516ff5`): one 640 px JPEG, ~332 tokens. It used to be the recorder's
  1920x1080 PNG, ~2,724 tokens, and on a still page there was no picture at all.
- **Reflexes read one region** (`67f13b1`): `see.locate` and `see.changed` take `{in: [x, y, w, h]}`, for games
  whose player, ground and obstacles share one colour.
- **New reflex rules start clean** (`a3ff316`): no stale event for the next `retinat_wait`, and stats of their own.
- **Recording no longer slows the reflexes** (`63c2d52`). Decision to input while recording was p50 58 ms, p95
  96 ms; it is now p50 1.8 ms, p95 22 ms. Frames are paced to the video's rate, sent as JPEG and decoded off the
  event loop.
- **Dissolves and fades are shot changes; a pan is not** (`fc41722`). This is twin-comparison with a blend test and
  motion compensation (a small pan at 16x16 otherwise passes as a dissolve).
- **`find` shrinks the crop of a large match** to fit 720x300 (`7844340`). It was ~665 tokens for Commons' player.
- **`see.track`** (`7844340`) gives a colour's velocity in px/s, so rules need not guess speed.
- **Desktop control survives a window closing mid-read** (`65f798c`). Xlib's default error handler used to exit
  the process.
- **What a block says is said once** (`aec0d20`): Wikipedia's Appearance panel was reported once per nesting level.
- **A screen recording ended early is not empty** (`409482f`). ffmpeg probes its input for ~2 s before it sets up
  output; a `q` sent sooner wrote nothing. The end of a recording now waits for that.
- **Checked live on the Commons film:** it is named "Big Buck Bunny 4K.webm", 1920x1080, 10:34.6. The title card is
  its own shot, there are 5 correct keyframes, and 30 s costs ~767 tokens.
- **Measured live on the T-Rex runner:** decision to input p50 1.6 ms, p95 2.1 ms. The rules started the game and
  jumped a three-cactus group (in the recording).

## Hourly loop, round 5: the stale look, settled
On the T-Rex runner, with a recording running, a look after the game ended showed an earlier frame. Notes are in
`docs/hands-on/2026-10-13/notes.md`, "Night".

- **The page view starved** (`ecac0a8`). The eyes' view and the recorder share one screencast and one frame slot. A
  recording started after a look took both, and one stopped while shared ended the stream. The view now restarts in
  step. Both orders are tested.
- **The recorder lost the final repaint** (`5a16efe`). Chrome's screencast drops the repaint that ends an animation
  while frames are in flight. A quiet recording is now topped up with one capture. The test failed about half the
  time before.
- **A look captures the page at that moment** (`cfa7e96`) and says so; the stream is the fallback. This was first
  reported as a third cause (a stream read by rules going stale). **That is withdrawn.** The test behind it had a
  stop rule that fired mid-animation, so its looks were right to show the animation mid-way. With the test fixed
  (`b5069f5`), it passes 6 of 6 on `5a16efe` too. The change stays as a design choice, not a fix.
- **Most of my live "stale looks" were my own mistake** (`4f31b66`). I had sent `retinat_look` in the same batch as
  `retinat_wait`, so it ran at the game's start. A seeing tool asked while a wait is pending now says so, and the
  wait's description says to look after it returns. An hour of capture changes chasing this was dropped unmerged.
- **Checked live in strict order:** look, record, rules, wait, then a look shows GAME OVER, and the recording ends
  on it.

## Hourly loop, round 6: rules that read what the page cannot, as the compositor draws it
Notes and recordings are in `docs/hands-on/2026-10-13/notes.md` ("Early morning", "Morning").

- **Cross-origin video and frames, read on screen** (`09d103a`). A rule's reading of a video from another origin
  without CORS (Commons) came back empty, so it never fired, and a frame from another site (an itch.io game) was out
  of reach. Now a source the page may not read is read on screen within its box, and `{from: 'screen'}` reads
  everything. Both new tests fail on the previous code.
- **The screen as the compositor draws it** (`a30f837`).
  - **Where it comes from:** a screencast on a DevTools connection of its own, when the session launched the
    browser. Otherwise forced captures, ~36-50 ms each, 153 ms on the Commons film.
  - **Measured:**
    - pictures are 9-11 ms old when the rules read them;
    - a square drawn in a cross-origin frame was clicked 34-39 ms after it was drawn;
    - a cross-origin cut was caught 45-72 ms after it happened;
    - live on Commons, pictures were 25 ms old, against 153 ms with captures.
  - **Broken frames:** the screencast sometimes sends one about half a second into playback (the video missing, or
    the whole frame black). Frames that replace a region at once are confirmed with a capture: 0 false cuts in 30
    plays, against 3-4 in 10.
  - **Lifetime:** the feed ends with its browser (it used to keep failing every 0.25 s for the rest of the process).
- **One capture at a time** (`b840ca2`). Found live: after rules read the Commons film, a look's keyframe of the film
  was the whole page, shrunk and tiled. Two `Page.captureScreenshot` calls in flight on one tab swap or mix their
  pictures: 20-24 of ~30 of the eyes' pictures, 0 with one lock. All 11 capture sites in the library now share one
  lock per browser connection.

- **A watch begun after a film was found unreadable still says so** (`e138b57`). Live on Commons, a watch started
  after the rules lost "picture: from the compositor ... its sound cannot be read", and called the sound "none
  captured (no audio track)". The percept looked only at events since the watch began. Test first; checked live in
  the same session: with the capture lock, all 7 keyframes of a 20 s watch were the film while a rule read it.

- **A game in another site's frame, live on itch.io** (`fb23f7c`). A rule watched "Pigeon Ascent"'s frame (served from
  `itch.zone`, unreadable to the page) on screen and fired when the game drew its title screen.
  - Pictures were 58-138 ms old there, against ~10 ms in tests: Chrome drew the WebGL game without a GPU at 240% CPU.
  - The reflexes' status now says where the screen comes from (`screen_source`) and how many frames a capture
    replaced (`screen_confirmed`), so a slow page can be told from a slow path.

- **The eyes keep to the page** (`13e8b2b`). On the Guardian's front page a look said "no video playing on
  assets.guim.co.uk/commercial/...", an ad frame. With site isolation off, the script that installs the retina in new
  documents reached other sites' frames, and whichever frame spoke last set the page's URL and attention. The retina
  now runs in the top frame only (test first; checked live).

- **A scan labels each frame by where Chrome drew it** (`3bd1501`). On the Guardian two frames were both "6.4 screens
  down" with different stories: frames from mid-scroll took the end position. Frames now keep the screencast's own
  `scrollOffsetY`. On six solid bands, 18 of 50 frames were mislabelled before, 0 after.

- **A scan says what stays on screen while the page scrolls** (`1fe0904`). On the Guardian a banner covered the
  bottom ~40% of every scan frame without a word. The scan now asks the page for fixed or sticky bands and names them
  with their text; live: "the bottom 37% ("...Who guards the Guardian?...")". A pixel-based first try flagged uniform
  pages too, and was dropped.

- **The eyes' cuts, scored against two whole films.**
  - **The reference.** Big Buck Bunny (the Commons film, same 634.6 s edit) and Tears of Steel (live action), both
    CC BY 3.0 from Blender's mirror. Each edit list came from ffmpeg's scene scores, with every candidate checked by
    eye: 125 cuts and 5 fades, and 135 cuts.
  - **Before.** At compositor sight's 6 samples a second, F1 was 0.85 on both films, with 34 and 29 false cuts a
    film: a bird leaving a still shot, a head passing the lens, a whip pan. A hard cut inside small motion was
    reported as a fade, and clean cuts after motion were missed.
  - **After.** A cut must be a spike (twice the change just before and after it) that reaches 45% of the picture.
    The rising threshold is gone, and a fade may not hold one big step. F1 is 0.97 and 0.93, with 2 and 3 false cuts.
  - **The cost.** More cuts into fast action are missed: Steel at 10/s misses 13 of 135, up from 7.
  - **Tests.** The Bunny's grids (490 KB) and edit list are a CI test asset. The test failed first at precision
    0.788.
  - **Live on Commons:** 3 of 3 cuts, none at the bird.

- **`until=bored` waits 8 s on a film, not 2 s.** A 35 s watch of the Commons film stopped at 6.7 s on a slow pan.
  Simulated on both films (35 s watches every 5 s, sound unreadable), a 2 s quiet window ended 86 of 120 and 33 of
  109 watches while more shots were coming. 8 s ended none early. Items longer than any watch (90 s) now get 8 s;
  reels keep 2 s. Test first; live, the default watch ran its full 35 s.

- **The eyes see text inside web components** (MDN; tests first). MDN's search suggestions sit inside a shadow root,
  and the text watcher observed only the document: none were reported. Open shadow roots are now found on the
  retina's heartbeat and watched. Live, every suggestion is reported as its row.
  - find says a skip link parked above the page "cannot be scrolled to", not "scroll up".
  - changes leaves out requests the browser side blocked (the profile's ad blocker); page-policy blocks are told.

- **Changes says what a search box did** (Wikipedia; tests first).
  - **Failed requests:** a fetch or XHR the page made that failed is now a change, so "no suggestions" comes with
    "a request the page made failed: 429 GET …/search/title?q=Grace+Hopper". Before, changes said "no changes".
  - **Rewritten rows:** a list whose rows are rewritten in place (a typeahead's suggestions) is reported. Before, at
    8 texts a second with a row's title and description counted apart, it was cut off after four rows. Changed text
    is now said as its row, and the budget is per batch.
  - **Live:** the 429s are said. The rows fix was not seen live, since every suggestion request was being refused.

- **The site explorer tells the network's failures from the site's.**
  - **gnu.org:** a flaky egress proxy's 502 was reported as the site's, and the run stopped at the start page. A
    gateway error or no answer is now tried once more (start page, every page, every link check). A page that then
    loads is "failed once, then loaded"; a second failure says it was tried twice and may be the network.
  - **info.cern.ch:** its own http:// links were taken for another site and checked by a fetch the browser blocks as
    mixed content ("returns no response" for a page that loads). The same host on a default port is now the same
    site, http links on https pages are checked at their https address, and they are reported once as low.
  - Tests first; live, 2 pages explored and one real 404 found.

- **find reads text that is only pixels.** On OpenStreetMap, "Westminster" was on screen, drawn into the map's
  tiles, and find said "not found". When the page's text has no match, find now reads the screen with OCR (new `ocr`
  extra: RapidOCR, PP-OCR models on onnxruntime, Apache-2.0) and says so, with confidence and a crop.
  - **Tuned on a live screenshot:** box threshold 0.3 (11 of 12 district names), lines too short for the query
    skipped (up to ~2x faster), letters and digits only in matching, and a match missing up to 30% at one end
    flagged as read in part.
  - **Live:** "Southwark" in 4.8 s, "City of Westminster" in 7.3 s.
  - **Limits:** seconds per read, on screen only.

- **`retinat_scroll`: the browser tools can scroll.** On the Python docs, find said its match was "13.0 screens" down,
  and no Retinat tool scrolled a page (only the desktop tools could). It turns the real wheel by `screens`, or `to` a
  text (the `nth` match), finishing the scroll if smooth scrolling fell short, and answers with where the match is
  now to click. Test first through the MCP handler; live on the Python docs, one call scrolled 13 screens to
  `wait_for` (13 PageDown calls before), `nth` reached the fourth match, `screens: -2` moved 2.0 screens. Live with the slow-page fixes on three.js: a scan in 14.0 s, and a recording that keeps time.

- **Pages drawing a frame a second.** On a three.js example (WebGL drawn in software here), a scan timed out at 60 s,
  even for one screen.
  - **Cause:** Chrome takes in input with the page's frames. The pointer's glide waited a frame per step (82.8 s at
    1 frame a second) and the wheel a frame per notch.
  - **Fix:**
    - the glide keeps to its planned time, skipping ahead along the same path;
    - what is left of a scroll goes in one turn, with a mouse move after it so it has landed;
    - a scan reads the page half as often.
  - **Tests first, on a page drawing 2.5 frames a second:** a click took 10.1 s, now under 5; a 900 px scroll took
    7.5 s, now under 3.
  - **Local stand-in at 1 frame a second:** a scan took over 90 s, now 15.8.

  Recordings of such a page were 20 times too short (ten minutes made 30 s). The recorder's clock wrote one frame per
  turn and each turn waited on the page; it now writes every frame owed. Test first: 16-17 -> 40-65 frames for 5 s.

- **A scan shows the whole page.** On BBC News a scan's six frames were at 0.0, 0.1, 1.5, 1.9, 2.2 and 3.6 screens
  down ("cover 60%"): two near-repeats, and strips of the page on no frame. Frames were chosen as for a film, by look,
  and white pages look alike at 16x16. A scan now chooses frames for the page height they add, and spends frames left
  over on what changed on its own. Test first (look-alike screens: 64% -> at least 95%), plus a guard for the leftover
  rule. Live on BBC News: "showing 98% of the page height".

- **A reflex event says which frame the rule saw.** Live on Commons, a cut rule said the film's cuts came
  0.10-0.12 s late. Measured with `requestVideoFrameCallback`:
  - the video's clock runs 17-29 ms ahead of the frame on screen;
  - the screen picture the rule fired on is older still.

  Each screen picture now carries its draw time, and an event gives the media time of the frame on screen then,
  keyed by presentation time.
  - Test first: a cross-origin clip gave -5 to 42 ms; now each cut is on its own frame.
  - On a 1080p film, 30 cuts of 30 are on their own frame (they were 50-120 ms late).
  - Live on Commons: 16.30/20.22/27.49 s against 16.30/20.17/27.47.

  The firing itself is no faster: 27-53 ms from the cut's frame reaching the screen. Three suspects were measured
  and ruled out: the screencast's frame rate, the picture format and size, and `callFunctionOn`.

- **Compositor sight takes no captures.** It cuts a cross-origin video's box from the newest frame of a screencast
  on a DevTools connection of its own (captures remain the fallback). Each picture is labelled with the film's time at
  the moment Chrome drew it, interpolated from readings of the film's clock taken on every tick. Taking `currentTime`
  less the frame's age went backwards in a stall, and a full run caught it. A 3 s watch went from 18 captures to none. Live on Commons it found the title fade and all three
  cuts, and the recording was the page in 227 of 227 frames.

- **Captures stop showing in the screencast.** A `Page.captureScreenshot` with a `clip` sends its region out as
  screencast frames, at any scale and with any `captureBeyondViewport` (22-40 of 52-97 frames in the test). The
  recorder, the reflexes' screen feed and scans read the screencast, and 15-18% of two Commons recordings were the
  film's box instead of the page. A clip inside the viewport at scale 1 or less is now a viewport capture cropped in
  Python; a clip beyond the viewport or an enlarging zoom still clips. Test first; live, 0 of 108 recording frames.

- **The reflexes no longer confirm screen frames with a capture.** Their "broken frames" (false cuts in 3-4 of 10
  plays) were their own clipped captures showing in the screencast. With that fixed, the screen tests passed 20 of 20
  without the check, so it is gone, and a cut is caught 19-27 ms after it happens instead of 45-72 ms. Live on Commons,
  a cut rule fired at the film's three real cuts and nowhere else.

- **A video of no known length gets the film's 8 s before "bored".** A live stream reports Infinity. Tested on an MSE
  stream with duration Infinity: YouTube's ISS stream answered this network with a CAPTCHA page, out of bounds.

- **Compositor pictures carry the time the film was at.** On Commons, keyframes were listed "at 2.2s 3.9s 3.5s 4.8s"
  and the one at 3.5 s showed a later shot. Labels came from the page's once-a-second report of media time, run
  forward by the clock, which runs ahead while a stream stalls. The video's `currentTime` is now read on both sides
  of each capture. Test first (a film whose clock stops for 1.5 s); live, the keyframes are in order and right.

- **A second watch says what it watched.** "watched 27.4s" for a 10 s watch (it counted the hold between watches),
  "the first 5.1s was not held ... unknown" (an earlier watch had seen it), and "on screen" read when the video was
  found. All three fixed, test first; live: "watched 10.1s", "before 5.0s: in an earlier percept".

- **A video that failed to load is named** (found on video.blender.org, whose H.264 films this Chromium build cannot
  decode). Look and watch said "(no video was on screen)". They now give the media error and the browser's message:
  "MEDIA_ERR_SRC_NOT_SUPPORTED ... the browser says "DEMUXER_ERROR_NO_SUPPORTED_STREAMS..."". Test first; live.

- **Click coordinates.** A look image is ~640 px wide while the viewport is ~1900. The tools said clicks take "CSS px,
  as on the look images", so a click read off the image missed. A look now states the factor ("frame 640x363 of the
  1905x1080 CSS px viewport: to click, multiply its pixels by 2.98"), and the tool descriptions and docs match. Test
  first; live, a click at the image position times the factor landed on the link.

- **No "one moving object" across cuts.** On the film the bouncing-object tracker spliced six shots into one object
  that "reached the bottom 14 times" (~150 tokens of noise). It now speaks only for one shot (test first). Checked
  live after the fix. (`6852df3`'s message wrongly says that check came from the earlier watch, which did show the
  line.)

## Hourly loop: games played by reflex rules (the owner's ask), and four fixes they found

The owner asked for games as the test of real-time sight. The games were Subway Surfers and Temple Run 2 on Poki,
played only through Retinat's tools. The rules read pixels from `{from: 'screen'}` (the games run in a frame from
another origin) and press arrow keys. Clips and the rule are in `docs/hands-on/2026-10-14/`.

- **This machine has no GPU.**
  - Subway Surfers drew 3 frames a second at 1905x1080, with pictures 600 ms old.
  - It drew 8 a second (136 ms) with the profile's viewport at 960x540, and 270-330 ms while recording.
  - Temple Run 2 drew ~16 a second (91 ms).
- **Subway Surfers: 43 with no input, 206 on rules alone, best 323.** The 323 run had a stretch at the end with no
  rules.
  - Lane rules (ground colour near and far per lane, tuned on recorded frames) dodge oncoming trains.
  - A red-in-the-column rule rolls under overhead barriers. It took the run through the tutorial's roll step, where
    it had been stuck at 97.
  - The tutorial's Left/Right step still needs a rule (I did it by hand once, to 318).
- **Temple Run 2: no score yet.** Its tutorial lights the key it wants on an on-screen pad. A rule for that missed
  live, which led to the first fix below. A car racing game was not started.

Fixed, test first:
- **Every look on a page with a scrollbar was ~1% out**, ~15 px at the far edge.
  - The crop that keeps clipped captures out of the screencast took the device scale from a whole-viewport capture,
    which includes the scrollbar. It now uses `visualViewport` over `cssVisualViewport`.
  - A marker at the bottom right of a tall page was 18 px off, now within 4.
- **Reflex screen pictures say the CSS size they span.** A screencast frame includes the scrollbar; a capture does not.
- **No tool offers a wait past a client's 60 s per call.** `retinat_wait(timeout=60)` had come back as "timed out
  after 60s". Waits now go to 50 s, and watch, browse and the eyes tools to 45 s.
- **`retinat_requests` said "30 requests after #0" of 624**, offering since=624 next. It now says how many a limit left
  out ("594 earlier not shown"), and `pattern=` found the game's frame live. The console does the same.

Not fixed: scan, browse and explore have no overall deadline under 60 s yet.

**Verification:** full `tests/ci` on `a5e10a0`: 1,737 passed, 30 skipped, 0 failed (38m50s), first run. The reflex and eyes files passed 105 of 105, and the Retinat file 31 of 31, on the way.

**Next hour, Temple Run 2.**
- Looks line up live after the scrollbar fix: find (329, 400) against look × 1.48 (329, 402).
- **New: `screen_width` for a rule set** (test first). Temple Run 2's tutorial lights its key by drawing it larger,
  and that is ~7 pixels of the rules' 240 px picture.
- Measured live: 240 px pictures 72-117 ms old, 480 px 160 ms, 960 px 510 ms.
- At 480 the pad rule took the runner over the jump and the zipline, and on the third pass past a gap and a broken
  bridge. No score read yet.
- Next: send rules only the regions they read, at full resolution, instead of a whole wider picture.

**Verification:** full `tests/ci` on `aa7ce85`: 1,737 passed, 31 skipped (the default-profile test skips by design while a running Chrome holds that profile), 0 failed, first run.

**Next hour, a car racing game (MR Racer on Poki).**
- Throttle only, it scored 159 (time ran out), then 40 (crashed into a pickup).
- Under the game's red tint no fixed colour tells road from traffic.
- **New: `see.mean({from, in})`**, a region's average colour, so rules can compare regions with each other (test
  first).
- The steering rule is not done.
- **Verification:** full `tests/ci` on `077c38d`: 1,738 passed, 31 skipped, 0 failed, first run.

**Next hour, MR Racer Endless** (notes only, no code):
- The steering rule crashed at 0.22 km, score 71.
- The default chase camera leaves a 24 px band of road between the horizon and the car's roof, and the rule's boxes
  sat above it.
- The game's C camera doubles the band; to be played next.

## Open, from the hands-on notes
- The Bunny's title fades to plain lilac, then the clouds fade in. That fade through a colour is found at some
  sampling phases and missed at others; it was missed live after the cut change. Splitting such runs found it, but
  called more false changes on both films, so it was dropped.
- A capture cropped from the viewport costs 84-100 ms on a busy page against 32 ms clipped. Compositor sight no
  longer captures; looks and zoom-outs still do. The screencast's own CPU cost was not measured. A cut rule's lag live on Commons (0.13-0.18 s of video) is longer than in the test (19-27 ms); not explained yet.
- The retina's grid on a readable video, checked in Chrome: it averages (1-px stripes give a flat grid), so that path
  matches the area-averaged scores, not the point-sampled ones I modelled first.
- A readable film was not checked live against its edit list: PeerTube's films are H.264, which this build cannot
  play.
- Two frames of one recording showed compositor sight's clipped picture instead of the page. Not looked into.
- The feed test `test_browse_watches_each_reel_once...[/feed]` is intermittent under load (1 failure in about 20
  runs alone): the first flick sometimes lands two reels on. Not reproduced under synthetic load (8/8).
- The play-arena title-screen test failed once in a full run (`fb23f7c`) and has passed 5 times since. Cause unknown.
- Two tests failed repeatedly in combined runs in the first hour after the container restarted:
  - `test_a_look_after_a_recorded_game_shows_how_it_ended`, 4 of 5, also on the older commits;
  - the region-runner reflex test, 2 of ~9.

  Neither has failed in 5 runs since. The look test now reports the page's own last frame time if it fails.
- Read on screen, a video's box includes the player's own overlay (play button, controls). On Commons, rules fired as
  it faded; not yet verified against the film's cuts.
- A look on Commons listed a shot "0.9s-0.2s" (an end before its start), in the session with the overlapping
  captures. It did not recur with the lock in place (one 20 s watch); not looked into further.
- "on screen" for a player is often its control text ("0:04 / 10:34 Loaded 2.64%").


🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_013mvkJSRqASuCudGU4bwZQY
