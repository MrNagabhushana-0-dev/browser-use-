# Hands-on, 2026-10-19

Driven only through the Retinat MCP tools, on a container that had booted at 18:25:01 UTC.

## 1. The first `retinat_open` after the machine booted failed at 30 s (bug, fixed)

`retinat_open https://en.wikipedia.org/wiki/List_of_largest_cities` at 18:30:43, the first browser launch since
boot:

```
Event handler ... on_BrowserStartEvent ... timed out after 30.0s and interrupted any processing of 1 child events
effect: none: nothing was done, so it is safe to try again once the cause is fixed.
```

The same call again worked (19 s, Wikipedia's load included). The handoff of 2026-10-14 had the same failure after a
container restart.

What was measured:

- The disk under this VM reads files untouched since boot at **16-21 MB/s** (node 102 MB in 6.5 s, Chrome's
  headless shell 306 MB in 15.1 s). Chrome's binary alone is 463 MB.
- A launch on a warm disk takes 3.5-6 s, on a fresh or the real (296 MB) profile alike.
- Dropping Chrome, its extensions and a copy of the profile from the guest's page cache (`posix_fadvise
  DONTNEED`), then launching: **failed at 31.5 s** with the same error. That launch had paged in only 246 MB
  (`mincore`), about 2 s of sequential reading, so the cost was Chrome's scattered page faults, not bandwidth.
- Later evictions no longer reproduced it (3.5 s): whatever lies under the guest kept the blocks by then. A first
  start after boot is the case that stays slow, and it cannot be made cold again inside one container.
- Reading the files in order before launching took the launch to 3.5 s.

Fix, test-first:

- `BrowserSession.start(timeout=...)`: the time a start may take, the launch inside it and Chrome's wait for its
  port included (they had their own 30 s each).
- Retinat keeps one browser start in flight across calls, with 120 s in all. A call waits for it as long as its own
  budget allows; if it cannot, it says `The browser is still starting (Ns so far ...). Nothing was done; call
  again.`, and the next call joins the same start instead of launching another.
- Tests: `test_a_slow_launch_starts_when_given_the_time_and_leaves_nothing_behind_when_not` (a real Chrome begun 3 s
  late through a shell wrapper, the 30 s defaults lowered to 2 s through their env vars) and
  `test_a_browser_slow_to_start_is_waited_for_across_calls_and_started_once` (a Retinat call with a 3 s budget on a
  Chrome begun 5 s late: the first call answers within its budget, a later one opens the page, one launch).
- A start stopped at its limit **left its Chrome running**, holding the profile. My first note here said it did not:
  that check was vacuous. The wrapper was named `slow-chrome.sh`, and a path containing "chrome" makes the profile be
  copied to a temp dir, so the check looked for the wrong directory. A review workflow caught it. With the wrapper
  renamed the test failed (Chrome left running), and `_launch_browser` now kills the Chrome it began whenever the wait
  for its port ends without one. A cancelled `BrowserSession.start()` kills it too, so a Retinat start stopped by a
  route change or at shutdown ends at once rather than at its 120 s limit.

**Checked on a real cold boot (23:42 UTC, after the container restarted):** the first `retinat_open`
(`https://en.wikipedia.org/wiki/Big_Buck_Bunny`), the first Chrome launch since boot, **opened on the first call in
49 s**. Before the fix this case failed at 30 s. The Retinat MCP server itself took 88 s to connect after the boot
(cold Python imports), against the client's 120 s connect limit: not fixed, worth watching.

Seen on the way, not fixed: with `executable_path` set and headless on, `BrowserProfile.get_args()` runs
`<chrome> --version` with `subprocess.run` on the event loop for the user-agent string, which blocks the loop for as
long as Chrome takes to answer (seconds on a cold disk). Retinat's default profile sets no `executable_path`, so it
does not reach this.

## 2. The scan sheet now spreads over the page from the top (fix of 2026-10-14, checked live)

`retinat_scan max_screens=80 keyframes=6` on the same page: 11.8 screens tall, 14 scrolls in 18.4 s, 64 frames seen.

- Sheet: 6 frames at **0.0, 2.6, 5.3, 8.1, 9.5, 10.8** screens down (51% of the page height shown).
- Before the fix the sheet held 6 frames at 4.9, 6.1, 7.2, 8.3, 9.7, 10.9: nothing from the top half.

Video: `scan-sheet-spreads-from-the-top.mp4` (0.3 MB, 14.5 s at 2x speed).

Later the same day the sheet changed again (see 3): spread by distance alone, it split gaps, and a 6-screen page
scrolled in 100 px notches showed 88% where frames 800 px apart show it all. The sheet now takes its first frames at
even places from the top frame to the bottom one, then fills by height added; the 12-screen case still begins at the
top and spreads over the page.

## 3. A review of this round's commits (workflow, 5 reviewers and a skeptic per serious finding)

10 findings confirmed, none refuted. All fixed test-first except where noted:

- A launch stopped at its limit left Chrome running (above).
- Retinat's start in flight: a route change (`retinat_network`) during it left a started Chrome referenced by nothing
  and the next call failed with an `AttributeError`; a start that finished with no call waiting, its browser dropped
  since, made the next call fail on `assert self.browser_session`. The start is now stopped on a drop or at close
  (its Chrome killed), a finished start is let go, a cancelled one is told as such, and a start that ends as its
  wait does reports its own outcome.
- A launch inherited the start's time even when `start()` was given none, overriding `TIMEOUT_BrowserLaunchEvent`.
- `browse_cap` refused every item when `max_seconds < min_seconds + 0.5` (e.g. `max_seconds: 3`), so the browse
  watched nothing and its "call again" repeated forever.
- With speech on, a watch of 38-45 s, and a browse's longest or last item, were left no time for the speech model
  (`watch_span`, and `browse_cap(speech_rate=...)`).
- The scan sheet's coverage (above).
- Also: a browse that stopped before watching anything said "(no video was on screen)"; a keyframe read cut by the
  call's time blamed the page ("did not answer in 20s") and probed 2 s past the deadline; a browse that had no time
  to flick on is now skipped past by the next browse instead of re-watched; the 1 s-budget test's backstop grace
  is 2 s so a busy machine does not fail it.

Not fixed (in the findings log's Next list): the flick inside a browse is not bounded by the deadline; a start
cancelled before Chrome is spawned still waits for the spawn.
