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
- Checked on the way: a start stopped at its limit already left no Chrome behind (the test asserts it).

Not verified: a real cold boot with the fix in place. That needs a fresh container.

Seen on the way, not fixed: with `executable_path` set and headless on, `BrowserProfile.get_args()` runs
`<chrome> --version` with `subprocess.run` on the event loop for the user-agent string, which blocks the loop for as
long as Chrome takes to answer (seconds on a cold disk). Retinat's default profile sets no `executable_path`, so it
does not reach this.

## 2. The scan sheet now spreads over the page from the top (fix of 2026-10-14, checked live)

`retinat_scan max_screens=80 keyframes=6` on the same page: 11.8 screens tall, 14 scrolls in 18.4 s, 64 frames seen.

- Sheet: 6 frames at **0.0, 2.6, 5.3, 8.1, 9.5, 10.8** screens down (51% of the page height shown).
- Before the fix the sheet held 6 frames at 4.9, 6.1, 7.2, 8.3, 9.7, 10.9: nothing from the top half.

Video: `scan-sheet-spreads-from-the-top.mp4` (0.3 MB, 14.5 s at 2x speed).
