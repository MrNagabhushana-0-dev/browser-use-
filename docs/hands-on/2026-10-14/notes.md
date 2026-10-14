# Hands-on, 2026-10-14

## Just after midnight: a three.js example that a scan never came back from

**Where:** `https://threejs.org/examples/webgl_animation_keyframes.html`, a full-window WebGL scene. This Chromium
draws WebGL in software, at 1-3 frames a second on this machine.

**What happened** (`threejs-scan-hung-before.mp4`):
- `retinat_look` gave the scene right, at ~338 tokens.
- `retinat_scan` timed out after 60 s, twice, even asked for one screen and two frames.

**Why:**
- **The glide.** A scan first glides the pointer onto the page. Chrome takes in mouse moves with the page's frames, so
  each step of the path waited for a frame. Measured on a local stand-in (a full-window WebGL shader): 17.7 s at
  3 frames a second, 82.8 s at 1. A click on such a page was as slow.
- **The wheel.** It turns in notches, and each notch waited for a frame too (~8 s a screen at 1 frame a second).
- **The reads.** Every read of the page waits ~2 s there, and a one-screen scan made eight.

**Fixed, test first** (a page whose every frame takes 400 ms, 2.5 frames a second):
- **The glide keeps to its planned time.** Each step is where the glide is by the clock when it is sent. On a slow
  page it skips ahead along the same path and still ends on the target. A click took 10.1 s; now under 5 s, still
  trusted, still with moves before it.
- **What is left of a scroll goes in one turn** once a notch takes over 0.15 s. Chrome answers a wheel event before
  the page has taken it in, so a mouse move follows the last turn; it is answered after, and input is taken in order.
  A 900 px scroll took 7.5 s; now under 3 s, as far.
- **A scan reads the page four times instead of eight:** position, end, height and fixed bands in one read.
- **On the local stand-in:** a scan at 3 frames a second went from 23.7 s to 5.0 s, and at 1 frame a second from over
  90 s to 15.8 s.

**Live after** (`threejs-scan-finishes-after.mp4`): the scan came back in 24.7 s with the scene, 1 screen, 100% shown.
That was before the reads were combined, which saved ~11 s on the stand-in. Not measured live since.
It did not say the scene moves on its own: 6 frames in 25 s, and the animation is small against the whole frame.

**Found while trimming those videos: a recording of a slow page was 20 times too short.** Ten minutes made 30 s. The
recorder wrote one frame per turn of its clock, and each turn first waited on the page: the screencast's
acknowledgement, or a capture, ~2 s here. It now writes every frame its clock owes, from the latest picture, and
stopping writes what is still owed.
- Test first: 5 s of a page drawing 2.5 frames a second made 16-17 frames at 10 fps; now 40-65.
- The two videos above are from before this fix. They are 30 s and 33 s long for ~10 minutes each, so they run fast.

## 02:10: three.js again with the fixes, then the Python docs

**three.js, live with the fixes** (`threejs-scan-and-click-recorded-in-time.mp4`):
- **The scan** came back in 14.0 s with the scene (it timed out at 60 s yesterday, then took 24.7 s).
- **A click on the scene** took 12.2 s end to end: about five round trips with the page at ~2 s each. Those are the
  check of what is under the pointer, the last glide steps, the press and the release. Input that Chrome takes with
  the page's frames cannot go faster than the page draws.
- **The recording:** 59.2 s for 68 s of session (it was 30 s for ten minutes). The rest is most likely the wait for
  the slow page's first picture, which the video cannot show.

**The Python docs** (`python-docs-find-far-below.mp4`). `retinat_find "wait_for"` gave 4 matches, each "below the
visible area: scroll down about 13.0 screens" (and 13.5, 13.7, 14.6). Nothing in Retinat scrolled a page: only the
desktop tools could. It took 13 PageDown keys or a run of swipes, each a call.

**Added, test first: `retinat_scroll`.** It turns the real wheel over the middle of the page: by `screens` (negative
is up), or `to` a text, the `nth` match as find counts them. With `to`, it looks again and finishes the scroll if
smooth scrolling fell short, then answers as find does, with where the match is now to click.
- The test is a long page with the heading 39 sections down. The heading ends on screen, its stated centre is within
  6 px, and `screens: -2` moved 2 screens up.
- **Not checked live:** this session's MCP client lists tools only when it starts, so the new tool cannot be called
  from it. The test calls it through the server's MCP handler, on a real browser.

## 03:00: OpenStreetMap, and text that is only pixels

**Before** (`osm-find-map-label-not-found-before.mp4`). The map of London looked right, but at 640 px its labels
could not be read. `retinat_find "Westminster"` said "not found": find searched the page's text and the names of
controls, and map labels are drawn into image tiles.

**Added: find reads the screen when the page's text has no match** (the new `ocr` extra: RapidOCR, PP-OCR models
on onnxruntime, Apache-2.0).
- **Measured on OpenStreetMap screens:**
  - A full 1920x1080 read is 4.5-6 s on 4 cores, most of it recognising ~200 lines.
  - Lines too short to hold the query are skipped: "Westminster" 6.2 -> 4.1 s, "City of Westminster" 5.0 -> 2.7 s,
    no gain for a short word.
  - A lower box threshold (0.3, default 0.5) recovered "Lambeth", which had come back as "eth": 11 of 12 district
    names, against 10.
- **Live, three things showed up only on the real screen:**
  - Labels came back as "Cityof-Westminster", so matching keeps only letters and digits.
  - "Southwark" was read as "uthwark" at every setting tried. A line holding all but up to 30% of the query at one
    end counts, said to be "read only in part", and its box is extended over what was missed.
  - Matching was right on a frame of a recording and wrong live. The live screenshot read differently from the h264
    frame I had measured on, so the measures above were redone on a saved live screenshot.
- **Tests:** a canvas with "Kensington Gardens" drawn in it is found within its drawn box, and an absent name is not.
  Pure tests cover the matching rules.

**After** (`osm-find-map-label-read-from-pixels.mp4`):
- "Southwark": 4.8 s, at (1123, 586), read in part, with the crop centred on it.
- "City of Westminster": 7.3 s, at (906, 664).

**Limits:**
- Seconds, not milliseconds, so it runs only when the page's text has no match. Every miss costs a read, and the
  answer says how long it took.
- Only what is on screen now.
- Small labels crossed by lines can still be missed.

## 04:00: the site explorer on gnu.org and info.cern.ch

**gnu.org, Hello's pages** (`explore-gnu-proxy-502-taken-for-the-site.mp4`):
- `retinat_explore` reported "[high] Link to /software/hello/ returns 502" and explored one page.
- The page it had loaded said "upstream request failed": this environment's egress proxy, not gnu.org. curl at the
  same time: 2 of 3 requests failed, the third got 200 in 0.6 s.
- The run took its links from that one error page, found none, and stopped.

**Fixed, test first** (a site whose home page answers 502 once, then 200):
- A load that meets a 502/503/504, or no answer, is tried once more 2 s later. That covers the first load, which
  discovery takes its links from, and every page.
- A page that then loads is "[medium] ... failed once (502), then loaded on a retry", and the run goes on.
- One that fails again stays high, and now says "on both of two tries 2 s apart (a gateway error: it may be the
  network, not the site)".
- Link checks get the same one retry.
- Live afterwards gnu.org was unreachable (3 of 3), so it was reported broken, as it was.

**info.cern.ch** (`explore-info-cern-own-http-links.mp4`). The site links its own pages as http://info.cern.ch/...
while the run started at https.
- **Before:** one page explored, plus "[high] Link to /hypertext/WWW/TheProject.html returns no response". Discovery
  took http for another site, and the link check's fetch of an http address from an https page is blocked by the
  browser as mixed content. That page loads fine.
- **Now:**
  - The same host on a default port is the same site, whatever the scheme.
  - An http link on an https page is checked at its https address, and reported once as a low finding.
  - Rule tests are pure: Chrome trusts loopback, so the block cannot be made against a local server.
- **Live:** 2 pages, TheProject.html 200, the false alarm gone. One new high finding, a 404 for
  /hypertext/WWW/Administration/Mailing/Overview.html, which curl confirms on both schemes. It is a real dead link.

## 04:45: Wikipedia's search box

**Before** (`wikipedia-search-suggestions-missed-before.mp4`). find, click and type worked: the box took "Alan
Turing", then "Ada Lovelace". Two things went unsaid:
- **"Alan Turing":** the suggestions stayed those for "Al", and `retinat_changes` said "No changes since last asked".
  True, but no help: retinat_requests showed the suggestion request for "Alan Turing" refused (429). Wikimedia
  rate-limits this network.
- **"Ada Lovelace":** the request got 200 and the rows on screen became "Ada Lovelace / English mathematician
  (1815-1852)", "Ada Lovelace Award" and so on. Neither changes nor a look said so: only the first suggestions, for
  "A", had ever been reported.

**Fixed, test first:**
- **A fetch or XHR that fails is a change:** "a request the page made failed: 429 GET /w/rest.php/v1/search/title?q=…".
  - Once each. Images, scripts and requests the page cancelled itself are left out.
  - `retinat_changes` journals them as it is asked. On a still page no batch from the retina comes along to do it,
    which my first version relied on: its test passed only because the first changes call had already taken the line.
- **Rows rewritten in place are reported:**
  - The watcher's budget was 8 texts a second, and a suggestion's title and description were two texts. A list of
    seven was cut off after its fourth row.
  - Changed text is now said as its row (list item, option, table row, link) when that is 240 characters or less.
  - The budget is per batch of changes, 8 a second with up to 20 texts each: a ticker makes many batches, a list
    update one.
  - Test: seven rows rewritten through their text nodes are all reported.

**After** (`wikipedia-search-refused-requests-said.mp4`): typing "Grace Hopper", changes said the suggestion requests
for "Grace Hop" and "Grace Hopper" were refused (429). Every suggestion request was being refused by then, so the rows
fix could not be seen live. I did not retry against Wikimedia's limit.

## 05:50: MDN's search box, a web component

**Before** (`mdn-search-web-component-unseen-before.mp4`). MDN's own search filters its index in the page, so it
was a fair check of last hour's rewritten-rows fix.
- Typing "flexbox" showed "Flexbox", "Basic concepts of flexbox", "Typical use cases of flexbox"… on screen.
  Neither `retinat_changes` nor a look reported any of it.
- The search box is a web component: its rows are inside a shadow root, and the text watcher observed the document,
  which changes inside a shadow root never reach.
- find said "Skip to search" was "above the visible area: scroll up about 0.3 screens" on a page at its top. It is a
  skip link parked above the page until it has keyboard focus; no scrolling reaches it.

**Fixed, test first:**
- **Open shadow roots are watched too.** The retina cannot see the page's own attachShadow calls from its isolated
  world, so new roots are looked for on its heartbeat, about once a second. What a component already shows when its
  root is first found (after the page settled) is said then.
  - Test: a custom element attaching its root after load, then filling it, is reported.
- **find says a match above the top of the page cannot be scrolled to**, as a skip link is placed until it has
  keyboard focus.
- **Requests the browser side blocked are left out of changes.** On MDN the profile's own ad blocker stopped its ad
  call and telemetry, and changes said "a request the page made failed: blocked POST /pong/get". Blocks by the
  page's own policy (CSP, mixed content, CORP/COEP) are still told, and retinat_requests still lists all of them.
  This one's test was written with the code, not before it.

**After** (`mdn-search-web-component-seen.mp4`):
- Every suggestion is reported as its row: "CSS / Guides / Flexible box layout Basic concepts of flexbox", "CSS /
  Reference / Properties flex-wrap CSS property", …, refreshed as the typing went on. That also shows last hour's
  rows fix live.
- find: "out of sight above the page: cannot be scrolled to".
- changes no longer lists the blocked ad call.

**A run of the eyes and Retinat files had one failure I did not keep the log of.** Two reruns with the same code:
115 of 115, then 115 of 115 with the log kept. It is an intermittent I cannot name.

## 06:36: `retinat_scroll` live at last

A container restart reconnected the MCP client, which then listed `retinat_scroll`
(`python-docs-scroll-to-wait-for-live.mp4`). On the Python asyncio docs:
- `to: "wait_for"` scrolled 13.0 screens in one call and put the match at y 540, mid-screen, with the crop on it.
  It had taken 13 PageDown calls.
- `to: "wait_for", nth: 4` brought the fourth match to y 540.
- `screens: -2` moved 2.0 screens up: "12.6 screens from the top of 24.9".

## 08:00: Puter's spreadsheet (from the owner's links)

`spreadsheet.puter.com`, an Excel-like grid. Typed "Revenue", Tab, "1200", Enter into A1:B1
(`puter-spreadsheet-changes-flooded-by-labels.mp4`).
- `retinat_changes` listed the grid's column letters, "A", "B", "C" … "T", one line each, under "(41 earlier
  change(s) not shown)". Whatever it said of the cells was cut off.
- Asking again with a larger limit gave nothing: the hidden changes had been marked read.

**Fixed, test first** (a journal with the same burst):
- Five or more short labels (3 characters or fewer) within one second are one line: "26 short labels ("A", "B",
  "C", … "Z")".
- When changes must still be cut, pages, items, sounds and failed requests are kept before texts, and what was left
  out is said by kind: "(7 earlier text(s) not shown)".

## 08:27: Poki games, played by reflex rules (the owner's ask: Subway Surfers, Temple Run, a car race)

The owner asked for games instead of websites, as a test of real-time sight. All of it ran through Retinat's own
tools: `retinat_open`, `retinat_click`, `retinat_key`, `retinat_reflex` (rules over `{from: 'screen'}`, since the
games run in a frame from another origin), `retinat_wait` and `retinat_record`. No page script was injected and no
game state was read: the rules see pixels and press arrow keys.

**What this machine can do.** There is no GPU, so Chrome draws WebGL on the CPU (SwiftShader). At the 1905x1080
viewport, Subway Surfers drew **3 frames a second**, the GPU process used 280% CPU and pictures reached the rules
**600 ms old**. I set the browser-use profile's viewport to 960x540 (config only, no code), which gave **8 fps and
136 ms**. With a recording running alongside, it was 6-8 fps and **270-330 ms** (p50, `reflex status`). Temple Run 2
drew ~16 fps with pictures **91 ms** old. A person playing at 3-8 fps would struggle too.

### Subway Surfers (SYBO, on Poki)

| Version | What the rule looked at | Score | What went wrong |
|---|---|---|---|
| none | no input | 42-43 | ran into the first barrier |
| v1 | ground colour in boxes per lane | 12 | geometry taken from the crash camera: the "near" boxes sat on the runner's own body, so it dodged all the time (`subway-surfers-rules-v1-aimed-at-the-runner.mp4`) |
| v2 | boxes recalibrated on a running frame; ratios to box size | 43 | dodged the oncoming trains (1 → 40), then the barrier: its near count fell 3 frames before impact, too late at 330 ms (`subway-surfers-rules-v2-dodge-trains-miss-barrier.mp4`) |
| v3 | far box counts as danger too; jump when no side lane is clear | 43 | the score froze at 43 because the game's **tutorial** rewinds a step done wrong; a 900 ms jump cooldown blocked the press inside the prompt |
| v4 | 400 ms cooldowns | **206** | passed the jump step, then looped on "Press Arrow Key Down" (an overhead barrier) |
| v5 | + red in the runner's column above y=190 → roll | 158+ | **passed the roll step** (stuck at 97 for a minute before; `subway-surfers-rules-v5-roll-passes-the-tutorial.mp4`), then looped on the Left/Right step |

- Best run: **323** ("New High Score!"). To be exact: the rules played it to 206, and the last stretch ran after I had
  cleared them, with no input.
- I did the tutorial's Left/Right step by hand with `retinat_key`: the game waits at a prompt. It went to 318.
- v5's rule is `subway-surfers-rule-v5.js`.
- How the rules were tuned: frames from the recording replayed offline through the same boxes (numpy, scratch
  scripts), against what the live rule had done.
  - The counts were clean. The right lane's far count fell 64 → 5 as a train came.
  - The overhead sign scored ~100 red pixels in the column, low barriers 20-40, and three other frames in 420 scored
    over 60.

**Not done:**
- A rule for the Left/Right tutorial step. The prompt's arrow shows the direction; I did not get to reading it.
- Coins.
- A run past the tutorial with rules only.

The tutorial comes back on every run: the game does not seem to keep its progress in this profile.

### Temple Run 2 (Imangi, on Poki)

- No input: it ran the opening straight for over a minute.
- The tutorial shows an on-screen arrow pad and **lights the key it wants**: a solid grey face (211,207,210), where the
  others let the path show through. A rule pressing whichever key is lit never fired.
- Replayed offline, the lit ▲ gave 23-25 grey pixels (threshold 20). Live, fewer than 10 even at tol 60.
- That led to a Retinat bug (below).
- I pressed ArrowUp by hand at the first prompt and the jump cleared it. The tutorial then looped at an arch with
  hanging ropes, a slide (`temple-run-2-tutorial-pad-and-arch.mp4`).
- No score yet.

### A car racing game

Not started this round.

### Bugs the games found, fixed test first

1. **Every look on a page with a scrollbar was ~1% out.**
   - On Poki, `retinat_find` put "Temple Run 2" at (329, 400); the look's pixels times its factor gave (322, 394).
   - The cause is in `browser/capture.py`. A clipped capture is taken as a whole-viewport capture and cropped (to keep
     clips out of the screencast). The crop took the device scale as image width / client width. The whole capture
     includes the scrollbar (1920 against 1905), so the crop took in the scrollbar and squeezed it into the picture.
   - Now it uses `visualViewport` (device px) over `cssVisualViewport`.
   - Test: a tall page with a fixed marker at the bottom right, found in the look image and scaled back. It was 18 px
     off, now within 4.
2. **Reflex screen pictures were mapped onto the window's width whatever they spanned.**
   - Screencast frames take in the scrollbar; captures, after fix 1, do not.
   - Each picture now carries its CSS size: the screencast's `deviceWidth`/`deviceHeight`, or the clip.
   - Test: a still page and an animated one, each with a scrollbar and a red square near the right edge; the rule must
     find it within 5 px.
3. **`retinat_wait(timeout=60)` came back as an error, "timed out after 60s".** MCP clients give a call 60 s.
   - `retinat_scan` had failed that way twice in this session.
   - Waits now go to 50 s and watches to 45 s, `eyes_watch`/`eyes_browse` included; the wait says to call again.
   - A test checks every tool's duration maxima.
   - Not fixed: scan, browse and explore have no overall deadline. A long one can still run past 60 s.
4. **`retinat_requests` hid what its limit left out.**
   - On Poki, 594 of 624 requests were analytics beacons; the list said "30 requests after #0" and offered since=624
     next, so the game's own frame could never be listed.
   - It now says "The newest 30 of 624 … (594 earlier not shown)", and `pattern=` (a regular expression on the URL)
     found the game frame at `*.gdn.poki.com` live. The console's log gets the same header.

**Unexplained:** at 08:34 Retinat's Chrome died while Subway Surfers was rendering at 1905x1080, as a test run
started. The test profiles are separate, nothing in the log says why, and Retinat came back on about:blank by itself.

## 09:55: Temple Run 2 again, with looks that line up and a sharper screen for the rules

Retinat was restarted with the scrollbar fixes.
- The look is now 640x366 with factor 1.48 and no scrollbar in it.
- "Temple Run 2" at look (222, 272) times 1.48 is (329, 402). `retinat_find` says (329, 400).

**The tutorial pad, read properly.**
- The key the game wants is drawn **larger**. Its grey face was not the tell: over the fog every key looks grey.
- Counting the arrow's yellow per key separates them: the lit ▼ had 1554 pixels in a 4x zoom, the others 52-260.
- At the rules' 240 px screen, that is about 6 sample pixels against 1, too few to trust.
- Separately, the yellow path behind the pad matched the old colour at tolerance 90, and the rule pressed keys on the
  start screen. In one run that left the game frozen, with ▼ lit for 70 s and no key, swipe or S doing anything
  (`temple-run-2-stuck-after-start-screen-presses.mp4`).
- The rule now in use is `temple-run-2-pad-rule.js`:
  - the arrow's own yellow (240, 200, 80) at tolerance 45, which the path (L1 distance 108) does not match;
  - the largest count, at least twice the next one;
  - the same key seen on 3 pictures in a row.

**Built, test first: `screen_width` for a rule set** (`retinat_reflex`, `Reflexes.install`). The test is a 4x4 px dot.
At 240 px the rules never see it; at 960 px they find it within 3 px of where it is. Measured live on Temple Run 2:

| Width | Pictures (p50 age) | Pictures in the first ~18 s |
|---|---|---|
| 240 | 72-117 ms | (earlier runs) |
| 480 | 160 ms | 50 |
| 960 | 510 ms | 20 |

**With 480 the rules got furthest so far** (`temple-run-2-pad-rule-480px-zipline-and-on.mp4`).
- The jump, then the zipline under the rope arch (which I had taken for a failure), twice.
- Both times the runner fell into the clouds at a gap after landing: ▲ lit once it was already falling.
- On the third pass it cleared the gap, crossed a broken bridge and reached a second zipline.
- No score read yet: the score panel is at the top right and was not read.

**Not done:**
- Temple Run's turns at corners outside the tutorial.
- Reading the score.
- A car racing game.
- Subway Surfers' Left/Right step.
- A feed that sends only the regions rules read, at full resolution. It would avoid the cost table above.
