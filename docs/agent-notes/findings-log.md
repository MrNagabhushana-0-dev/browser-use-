# Findings log

Real defects found and fixed, honestly assessed for impact — no invented
multipliers. "Impact" below means: how often the bad path is hit in normal
agent use, and how bad the consequence is when it fires.

## Round 2 — security/mcp-client/downloads/llm-schema

Status: in progress (first attempt hit a session usage limit on all 4 workers
simultaneously — resumed after reset, results pending).

Targets: `browser/watchdogs/security_watchdog.py` (domain-restriction
enforcement — a bypass here is the highest-value bug class in this round),
`mcp/client.py`, `browser/watchdogs/downloads_watchdog.py`, `llm/schema.py`
(shared across every LLM provider — one bug here hits all of them).

## Round 1b — WebMCP bridge (self-review, no separate worker)

**Landed** (commit `028de2d`): `webmcp/bridge.py` manifest deadline didn't
actually bound the in-page fetch (only checked a deadline *between* refs), so
an unresponsive manifest server hung `call_webmcp_tool()` on any unknown tool
name indefinitely. Also removed `window.agent` — installed despite the
bridge's own comment saying it deliberately wasn't, a free automation
fingerprint for anti-bot scripts.
**Impact: high** — hit on every page with a slow/dead manifest endpoint;
before the fix there was no bound at all.

Caught during my own verification, not the original worker's: the worker
fixed the JS-side bound (`manifestMs=5000`) but the Python-side wrapper
(`DEFAULT_DISCOVER_TIMEOUT=3.0`) was *shorter* than that, so the outer
timeout fired first and silently defeated the fix for `get_webmcp_tools()`.
Raised to 6.0s. Also found and fixed a test-fixture bug the debugging
surfaced: a shared `HTTPServer()` needs `threaded=True` or a `time.sleep()`
handler serializes across tests sharing the fixture.

## Round 1 — Agent / BrowserSession / Tools / DomService

**Landed** (commit `634cd86`), all four independently re-verified:

- **agent/service.py** — `_log_next_action_summary()` built the debug summary
  string every step and never logged it; docstring promised it would.
  Impact: low (observability only, DEBUG-level), but a real, silent
  contract violation with zero cost to fix.
- **browser/session.py** — `reset()` cleared every per-session cache except
  `_closed_popup_messages`; a dialog auto-dismissed before a reuse of the
  same `BrowserSession` kept being reported to the LLM as freshly closed for
  the rest of the object's life. Impact: medium — pollutes every future
  prompt on session reuse, one of the more common deployment patterns
  (`keep_alive=True`).
- **tools/service.py** — `evaluate()`'s quote-repair heuristic corrupted
  perfectly valid JS that escapes a quote inside a string (ordinary,
  common pattern), turning working agent-authored code into a syntax error
  it had no way to diagnose. Impact: high-frequency for any agent that
  writes JS containing quoted strings with an inner quote.
- **dom/service.py** — JS click-listener detection resolved up to 100
  per-element CDP object handles per call and only released the parent
  array's handle. Impact: unbounded resource leak, on *every single agent
  step* on any page with JS click listeners (extremely common) — the
  worst class of bug landed this round precisely because it compounds
  silently over a long session.

**Self-correction worth recording**: I initially tried to "improve" the
tools/service.py fix's guard regex myself (parity-based backslash counting
instead of the worker's simpler check). Stress-tested it against a
realistic case before shipping and found it was backwards — it would have
made a genuinely over-escaped snippet with an inner escaped quote
*unrepairable*, a regression the original, simpler version didn't have.
Reverted. Lesson: the review gate applies to my own changes too, not just
workers'.

## Round 0 — fleet-driven hardening (synthesis / webmcp / human-input / decide / profile)

**Landed** (commit `df31062`, merged via PR #4): 5 of 9 proposed fixes
survived Lead + advisor review and my own re-verification —
synthesis resolver (`data-test` locator parity, fill-replaces-not-appends),
WebMCP `call_tool` stale-manifest precedence, human-input stuck-key cleanup
on cancellation, decide's never-raises contract + hallucinated-tool-pick
guard, profile's headless UA OS-token mismatch.

**Rejected, with reasons** (held out of the merge, not silently dropped):
- FIX-2 (loose-control search-tool synthesis) — cache-fingerprint gap the
  Lead judged not yet safe.
- FIX-3 (scroll-independent fingerprint) — **blocking**: uncapped name
  lists in the fix could overflow the 60k-char scan budget and silently
  wipe out synthesis entirely on busy pages. Worse than the bug it fixed.
- FIX-6 (cobrowse launch cleanup) — Lead review did not clear it as-is.

Three agents in that round (advisor critique, advisor final sign-off,
FIX-8 review) hit usage limits mid-run — FIX-8 was held un-landed rather
than shipped without review, then verified and landed separately as
Round 1b once credits reset.

## Round 4 - vision: watching a video from its pixels

Asked for: an agent that looks at a video (not its transcript), drives a real browser
like a person for ~zero tokens, shows a live token meter in the browser, is recorded on
a screen recorder, and does not get blocked.

**What was built** (`browser_use/vision/{video,overlay,screenrec}.py`, tests in
`tests/ci/test_{video_watcher,overlay,screenrec}.py`, runnable example
`examples/features/watch_video.py`): seek-driven video sampling with in-page 8x8
signatures and best-first bisection to the biggest cuts; a contact sheet; a token ledger
with a naive-screenshot baseline; an in-page token meter plus a drawn cursor (Chrome does
not move the OS cursor for synthetic CDP input, so a recording of an agent otherwise shows
buttons pressing themselves); an Xvfb + ffmpeg recorder that finalizes the MP4 on a clean
quit. The interaction in the demo is scripted human-like input, so it spends zero model
tokens; only handing the result to a model costs tokens.

**YouTube was NOT usable, and I did not try to defeat that.** From this sandbox's
datacenter IP, the YouTube page loads and then shows "Sign in to confirm you're not a
bot"; the video never starts (`readyState` 0). That is an access control. Getting around it
(fingerprint spoofing, residential proxies, borrowed cookies) is circumvention and was out of
bounds. The legitimate routes are the existing co-browse handover (a person signs in once to
a persistent profile) or a host without a bot wall. The demo uses Big Buck Bunny from
archive.org, an open-licensed film.

**Findings worth keeping**
- The default Playwright Chromium in `/opt/pw-browsers` cannot decode H.264
  (`canPlayType` returns ''); `/usr/local/bin/chromium` can. Nothing failed loudly - the
  video just would not play. Tests use VP8/WebM so they run on either.
- This sandbox's HTTPS proxy re-signs traffic, so Chromium fails with
  `ERR_CERT_AUTHORITY_INVALID` until the proxy CA is pinned. `BrowserProfile.proxy_ca_cert`
  already did exactly that (SPKI pin, not disabled verification).
- **My first design was wrong for the real workload.** It located every cut and then merged
  the excess away to fit the budget. Fine for a 4-shot test, wasteful for a 10-minute film
  with hundreds of cuts. Replaced by best-first refinement that stops once the budget of
  cuts is located. The first real run also showed a 0.1s sliver shot burning one of eight
  keyframes, which produced `min_shot`. Both were caught by looking at the real output, not
  by the tests, which had passed.
- A test suite that passes first time is not evidence. Mutation checks (threshold 95, 0;
  canvas-taint ignored; ffmpeg killed instead of quit; overlay pointer-events on) were each
  killed by exactly the test meant to kill them.
- **The session token counter cannot validate an image-token estimate.** One controlled
  read of the 1300x372 sheet moved it ~163 tokens versus 645 by the published formula. n=1,
  uncontrolled for caching, so it proves the counter is not a usable instrument, not that
  the formula is wrong. Every token figure in the HUD and docs is a labelled estimate.
- Limits that remain: a shot shorter than the coarse step can be missed; a large motion
  (a bird flapping across the frame) can exceed the cut threshold and read as a cut - a
  thumbnail difference cannot tell motion from a cut; with a tight budget a "shot" is really
  a segment between the largest changes; a video in an iframe, or behind DRM, is unreachable.

### Round 4 addendum - what the independent review found (and what it got wrong)

A different model reviewed the new code in an isolated worktree and tried to falsify it.
Every claim was re-verified by writing a failing test first. Outcome:

**Confirmed and fixed** (each with a test that failed on the old code, then mutation-checked):
- BLOCKING: the paint wait used `requestAnimationFrame`, which never fires in a hidden tab,
  with no timeout - `watch()` hung forever despite `seek_timeout`, contradicting the docstring.
  Now bounded at 150ms.
- BLOCKING: an audio-only `<video>` gave "1 shot, static, in-page" with no error; an all-black
  result did the same. A confident wrong answer is worse than an error. Now: no picture is a
  `NoVideoError`; all-black adds a `warnings` entry (a black video and a DRM player withholding
  pixels are indistinguishable from here, so it says so instead of choosing).
- REAL: the largest `<video>` by box area was chosen even if `visibility:hidden` or `opacity:0`,
  and a bigger player in an iframe was invisible to the search. Now: visible elements only, and
  a larger iframe raises a warning that the real player may be inside it.
- REAL: `preload="none"` never fires `loadedmetadata`; it timed out with a raw `RuntimeError`.
  Now nudges `preload='metadata'` (not `load()`, which would restart an MSE player).
- REAL: two concurrent `virtual_display()` calls both picked `:99`; the loser's server died and
  its caller silently used the winner's display. Replaced with Xvfb's own `-displayfd`.
- REAL: ffmpeg dying at start-up left no file and no error. Now `RecordingFailed`.
- REAL: the overlay init script ran in every frame, giving iframes a stale duplicate meter.
- REAL: the "gradual change" branch put a cut at the midpoint of a possibly very wide interval;
  now keeps refining the steeper half.
- Cleanup: a per-shot sample computed and never used (wasted a seek, and a screenshot in
  fallback mode); a test tolerance whose comment said 0.08s but whose value was 0.45s (tightened
  to min_gap, 0.25); a vacuous tiling assertion (replaced by "keyframe lies inside its shot");
  a contact-sheet test that held only because of tile width (now compares against the real
  sizes of the separate keyframes).

**Did not hold up:** the reviewer's CORS concern (a CORS-served video with `crossorigin` stays
on the cheap in-page path - the new test passed without any change), and its tolerance concern
as a *defect* (measured error was already inside 0.25).

**Implemented but NOT test-covered:** canvas taint appearing mid-watch now restarts on
screenshot signatures instead of comparing two different measurements. I could not produce the
trigger deterministically, so this path has no test. The reviewer also could not reproduce it.

**Left as a documented limit:** a player that re-`play()`s itself is now re-paused on every seek,
but `rect()` still scrolls the page to centre the video, and the video is left paused. Not undone.

**Lesson, again:** the author's own tests passed 100% and could not have found the two
confident-wrong-answer paths, because the author only wrote inputs the author imagined. The
review is the part that found them.

## Round 5 - scrolling and real-time context (asked for after the video demo missed it)

The owner's complaint was fair: the first demo showed a cursor click and a video watched by
seeking, not scrolling and not real-time perception. Rebuilt around what was actually asked.

**Gap found.** `PerceptionStream`'s pan detector (`perceive._pan`) only tests left/right shifts
along one row, because it was built for side-scrolling games. Vertical page scrolling was
invisible to it, and a fast scroll changes over 55% of cells, which got reported as a scene cut.

**Built.** `perceive.scroll_estimate` (row-profile matching over the central columns, result as a
fraction of the screen), wired into the stream as `scroll=down 0.52h pos=2.3h new=text`, plus
`examples/features/scroll_and_perceive.py` (human wheel input, live HUD, recorded). No model is
involved in the live stream.

**What the tests found in my own code** (each re-verified, each mutation-checked):
- A first version returned a confident wrong number on sparse content. The fix was a uniqueness
  check against every rival offset, including standing still.
- That check failed on truly periodic content because its ceiling was a *ratio* of the best
  error, which collapses to zero for a near-perfect match and rules out rivals that fit exactly
  as well. Needed an absolute noise floor. Found by a failing test, not by thought.
- A guard ("twice as good as no movement") survived every mutant, was shown to be subsumed by the
  uniqueness check (offset 0 is one of the rivals), and was deleted rather than kept untested.
- My first "identical page" test passed for the wrong reason (no change at all, not ambiguity).

**The finding that mattered most: silent drift.** I added a ground-truth check to the demo
(page `scrollY` vs the stream's inferred position). Before the uncertainty work, repeated runs on
a real Wikipedia page gave error 0.00-0.03h in most runs and **1.3-1.9 screens of silent drift** in
some: near the bottom (reference lists, category links) rows repeat, the estimator correctly
refused to guess, and the stream said nothing while its position went stale. Fixed by returning
`(fraction, ambiguous)`, emitting `scroll=unknown`, and marking the position `?` from then on.
After the fix, 6 real runs: 5 within 0.01h (about 1% of a screen), 1 hit an unmeasurable step
and reported itself uncertain (0.18h off). n=6 on one site; not a benchmark.

**Limits, stated plainly.**
- The `new=text|media|panel|region` label for the strip that scrolled into view comes from the
  existing coarse region labeller; I did not verify it per strip, and the demo's values
  (`panel` most of the time) say little. Treat as a hint.
- What the page *says* cannot come from this. The contact sheet at this size shows structure
  (section order, image vs text) but not readable body text; reading text from pixels costs real
  image tokens (~1,200 per screen), and DOM extraction is far cheaper and exact for text pages.
  Pixels are for what the DOM cannot give you (video, canvas, games).
- Horizontal scrolling, sticky-header-heavy pages, infinite scroll and zoom are untested.

## Round 5 addendum - the test suite and the machine

**The "full suite" results earlier in this session were not full.** The project's pytest
`addopts` contains `-x`, so every run stopped at its first failure; everything alphabetically
after it never ran, including all my new vision tests in suite context. A genuinely complete run
(`-o addopts` without `-x`): 1436 passed, 2 failed, 1 error, none in the vision code.

**Three of my own earlier tests were fragile in the same two ways:**
1. `browser_use/mcp/server.py` runs `logging.disable(logging.CRITICAL)` at *import time*, and
   pytest imports every test module at collection, so in a full run all logging is off before
   any test starts. Tests that read log output (`test_crash_watchdog_health_check`,
   `test_next_action_summary_logging`) passed alone and failed in the suite. Reproduced
   deterministically (import `browser_use.mcp.server`, then run the test alone), fixed by
   re-enabling logging for the capture only. The import-time side effect itself is left alone
   (out of scope) and is worth an issue: importing a module should not silence a process.
2. An `Agent` owns an `EventBus` whose `_run_loop` task outlives `session.kill()` and
   `agent.close()`; only `Agent.run()` stops it (service.py, end of run). Tests that build an
   Agent or call `step()` directly left it alive and the event loop could not shut down at
   module teardown: a 60-300s teardown error although every test passed. I first blamed the
   shared keep_alive session; that was wrong (disproved by giving the file its own session, then
   by listing the live tasks). Fixed with a fixture that stops the agent's bus.
   Not audited: other tests that build Agents without `run()`; `grep eventbus.stop tests/ci`
   shows only one other file does it.

**The machine ran out of disk, and it was the library.** Crashed/flaky runs late in the round
were `ENOSPC`. `/tmp` held **25,369** `browser-use-*` directories (per-session downloads and
temp profiles) plus 111 UUID profile directories, tens of GB in total, left by repeated suite
runs. Freeing them took the disk from 100% used to 27%. This is a real leak in
`BrowserSession` (temp dirs are not removed on kill) and is not fixed in this PR; it is the
kind of thing that surfaces as "the agent crashed" in a long-lived deployment.

## Round 6 - the browser as eyes and ears (browser_use/eyes)

**Asked for:** the browser as the agent's eyes: no screenshot loop, no HTML dumps, short-video
feeds (Reels/Shorts) "streamed into the model" with their sound, human touch scrolling, low
tokens and latency.

**What is physically possible, stated first.** Claude takes text and images per turn; there
is no continuous video or audio input. So the build is the closest real thing: perception runs
*continuously in the page* at zero token cost, and the model receives compressed *percepts*
when it asks (`eyes_watch`, which can return early on a salient event), plus one line of text
injected into every turn by a Claude Code hook without asking. `hold=True` pauses the video
while the model thinks, so nothing plays unseen. That is event-gated, turn-based perception,
not streaming, and every tool description says what it is.

**Built.**
- `retina.js` in an *isolated world* (page scripts cannot see or tamper with it): picks the
  video a person would look at (largest, playing), taps its decoded frames with
  `requestVideoFrameCallback` (16x16 luma + 4x4 colour per sample, a ring of JPEG keyframes
  drawn from the element, never a screenshot) and its audio with `captureStream()` into an
  AudioWorklet (per 22 ms: loudness, ZCR, centroid, flux, flatness, pitch peak, 24-band
  spectrum; optional 16 kHz PCM). Pushed to Python over a CDP binding ~5x a second.
- `sight.py`: adaptive cuts (floor + MADs), loops vs rewinds, motion, colourfulness (Hasler &
  Susstrunk) and hues; keyframes by greedy facility location (monotone submodular, (1-1/e)),
  whose marginal gain is also the "bored" test.
- `hearing.py`: change points on the band spectrum (Foote novelty), then silence / tone /
  noise / beats / music / speech per stretch, onsets, tempo; `asr.py`: Silero VAD decides
  speech, Whisper tiny.en transcribes (optional extra, local, CPU).
- `percept.py`: one sheet (keyframe row + spectrogram strip per item, one time axis) + text.
- `human/touch.py`: CDP touch flicks on a truncated minimum-jerk profile (lifts off while
  moving), taps, long-press; `Eyes.next()` flicks, then *confirms by sight* that a different
  item settled, falling back to a longer flick, the wheel, the keyboard.
- MCP: `eyes_watch`, `eyes_browse`, `eyes_next`, `eyes_tap`, `eyes_swipe`, `eyes_now`
  (images as `ImageContent`); `eyes/hook.py` for UserPromptSubmit/PostToolUse context.

**Measured.**
- Synthetic ground truth through the real pipeline: cuts at 3.07/6.07/9.07/12.07 s for true
  3/6/9/12 (one 10 fps sample late); tone 441 Hz for 440; beats 120 bpm for 120; noise and
  silence where they are. CI asserts these (`tests/ci/test_eyes.py`, 19 tests).
- Real public-domain clips (Duck and Cover 1951, Apollo 11 launch 1969, Big Buck Bunny,
  LibriVox Sun Tzu), served as a *local* muted vertical feed: all five heard while muted;
  transcripts correct to the ear ("T minus 15 seconds... ignition sequence start", the Bert
  the Turtle song, the Sun Tzu passage); the ignition flash found as a 0.3 s bright shot.
  ~100 s of video watched in ~2.5k tokens of percept (estimate, 28 px patch rule).
- Flick reliability: 16/16 one-item moves on a scroll-snap feed; after the aim fix below,
  12/12 runs of the movement tests (36 executions) against 3/12 failing before.

**Corrections to my own work, each found by a test or a measurement.**
- My first muted-audio probe said a muted video's captured audio is silent. The research
  agent read Chromium's source and said the opposite; a careful re-test (unmuted, muted,
  volume 0) showed -21 dB in all three. The probe was wrong, not the source.
- The heuristic speech/music split fails on real pumping electronic music (it shares the
  4 Hz envelope). Measured HZCRR 0.21 (speech) vs 0.11 (music) per 1.5 s window, with
  overlap; I did not tune thresholds to one clip. Speech is decided by the VAD when the extra
  is installed and every heuristic-only percept says so.
- A feed rewinding a reel when it scrolls into view was reported as a loop; now a jump back
  is a loop only from near the end, a rewind otherwise, and ignored in the first second.
- `next()` declared success at the first change of attention, i.e. mid-scroll; now it waits
  for the new item to stay attended 0.6 s.
- Intermittent "feed did not move": instrumented, the flick's touches all arrived and the
  container scrolled then snapped back; the stroke was aimed at the reel's rect captured
  mid-scroll and got clamped against the screen edge. Now aimed at mid-screen like a thumb.

- Under CPU load the tests failed in new ways, and three fixes came from measuring them:
  (1) audio hops were stamped with media time on *arrival*, so bursts piled onto one instant
  and smeared change points; they are now stamped from the audio clock minus the delivery
  lag. (2) A fast flick could fling a CSS scroll-snap feed past the next item; `next()` now
  reads the videos' document order and flicks back once if it skipped. (3) Touch samples
  are taken at real elapsed time, so an oversleeping loop cannot send two stale points back
  to back. Stamping touch events with planned `timestamp`s was tried first and made it
  **worse** (3-4 failures per run); reverted.
- A click train looked like a tone once silent hops were ignored for steadiness; a tone now
  also has to be continuous. Revisiting a reel created a new item; identity is now element
  plus source.

**Limits, plainly.**
- Instagram and YouTube were **not** tested: Instagram needs your login; YouTube served a
  bot check to this sandbox earlier; and this Chromium build cannot decode H.264, which
  Instagram serves (VP9/WebM plays). The demo feed is local, with real clips.
- DRM (EME) video gives no pixels; cross-origin video without CORS taints the canvas
  (detected and said, no picture); videos in iframes are not attended.
- Wheel and ArrowDown do **not** move a CSS scroll-snap feed here (measured), so the fallbacks
  only help feeds with their own wheel/key handlers.
- 16x16 grids see composition, not detail; on-screen text is only readable on the sheet.
- The VAD called 0.7 s of Big Buck Bunny (no dialogue) speech. Whisper tiny.en is
  English-only; set `BROWSER_USE_EYES_ASR_MODEL=base` for other languages.
- Screen recordings from the Xvfb recorder have no audio track.
- Final stability on the pushed code: 6/6 runs of tests/ci/test_eyes.py (21 tests each), 3
  idle and 3 with three of four cores saturated. Before the load fixes, half of such runs failed.

Demo assets (`demos/eyes-feed-*`): a local muted feed of Duck and Cover (1951, public
domain), Apollo 11 launch (1969, NASA, public domain), Big Buck Bunny (c) Blender Foundation,
CC BY 3.0, and LibriVox's reading of The Art of War (public domain). The recording has no
audio track (the Xvfb recorder captures video only); the eyes heard every reel.

**Not novel.** Standard web APIs (rVFC, captureStream, AudioWorklet), facility-location
keyframe selection (video summarization literature), Scheirer-Slaney / Lu et al. audio
features, Foote novelty, minimum-jerk motion, contact sheets (vercel-labs/agent-browser also
has contact sheets and touch input). What is specific is the packaging for a turn-based
model: muted listening, boredom as marginal coverage, gestures confirmed by perception, and
percepts sized in tokens.

