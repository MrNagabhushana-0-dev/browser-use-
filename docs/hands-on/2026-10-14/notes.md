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
