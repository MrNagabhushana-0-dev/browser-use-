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
