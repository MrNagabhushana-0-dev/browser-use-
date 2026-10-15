**Verdict:** the runtime design mostly holds up, but the test plan doesn't. Three of the 13 tests cannot pass or pass without testing anything. One cancellation hazard would make the explore tests nondeterministic and the explore reports wrong. One import change would break the base MCP server for anyone without numpy. About 135 s of serial wall-clock tests is the wrong shape for this file's flake history. Nothing below was run; the arithmetic and code paths come from reading the code.

## A. Tests that fail, or pass without testing anything

**1. Test 7 (browse out of time) explores zero items.**
- Your seeds are final_reserve 3 + next_est 3.0 + overhead_est 1.5 = 7.5 s.
- At `Deadline.after(8)`, item 1 gets cap = min(4, 8 − 7.5) = 0.5. That is below `min_seconds + 0.5` = 1.5, so the browse stops before any item, and "1–2 items" fails.
- One item plus a flick needs about 12 s of budget; two items need about 17 s.
- **Fix:** make the planner a pure function, `browse_cap(left, more, max_s, min_s, final_reserve, next_est, overhead_est) -> float | None`, with an instant table-driven test. Derive the e2e test's budget from the same named constants, so changing a seed can't silently break it.

**2. Test 12 (MCP explore, `call_budget_s=6`) passes while the report is garbage.**
- The MCP handler builds the explorer with its defaults (`server.py:781`, `explore/service.py:83-93`): `settle_s=2.0`, plus mobile, scroll, dom_state and hud.
- After `deadline.minus(1.5)` there are 4.5 s. Setup alone takes about 2.5–3 s: navigate, settle 2 s, the environment eval that creates a WebGL context, then robots.txt and sitemap.xml.
- A page needs at least about 4 s: navigate, 2 s settle, walls, look, DOM summary, scroll, 0.5 s, probe.
- So every page is hard-cut and marked `timed_out`, fast ones included. The test still terminates and says "over N calls".
- **Two related gaps in the design:**
  - The premise that the first page of a call "had the whole budget" is false on call 1, where setup eats it.
  - The design doesn't say whether the soft check applies to that first page. If it does, the explorer livelocks whenever `2*settle_s + 2.5` (6.5 s at defaults) is more than the budget minus setup.
- **Fix:**
  - Mark a page `timed_out` only if it started with at least about 70% of the call's budget; otherwise put it back at the head of `pending`.
  - Skip the soft check for the first page of a call.
  - Leave timed-out pages out of the `mean page.seconds` estimate.
  - Let the server take explorer options (`settle_s`, `mobile`, `dom_state`) as a constructor argument, so the MCP test runs in a realistic regime.
  - Make test 12 assert that fast pages are not `timed_out`.

**3. Test 6 (slow page) accepts `'end'` and names no page.**
- The busy-rAF script was used on `/recorder` (`test_human_input.py:157`), which is a short page. On a one-screen page the scan returns `'end'` at once and the test proves nothing.
- **Fix:** use a page of at least 20 bands and require `stop_reason == 'time'`.
- A busy loop that draws nothing produces no screencast frames, so the first collect falls back to `capture()`. `capture_screenshot` (`capture.py:45`) has no timeout, and the design doesn't clamp it.

**4. Test 13 (static schema check) depends on the environment.**
- `_tools()` (`server.py:104-105`) lists the desktop tools only when desktop eyes are turned on.
- `retinat_desktop_watch` has `seconds` up to 45 (`server.py:247`) and isn't in `LONG_TOOLS`. So the test passes in CI and fails on a dev machine with desktop eyes on.
- **Fix:** build the list from `_browser_tools() + _desktop_tools() + _control_tools()` and keep an explicit `EXEMPT` dict with a reason for each tool.
- Also feed every resume dict through `effects.check_arguments(schema, resume)`. The schema minimums would otherwise make the server refuse its own advice: `watch.seconds >= 1`, `browse.max_seconds >= 2`, `scan.max_screens >= 1`. A watch cut with 0.4 s left would say `seconds=0.4`.

**5. Test 8 (watch out of time):** library `watch` defaults to `hold=False` (`service.py:299`), so "the video is held" fails unless the test passes `hold=True` or the design holds on every time cut.

## B. Determinism hazards

**6. The explorer's hard cut leaves a zombie navigation running.**
- `navigate_to` dispatches an event on the bubus bus and awaits it (`session.py:2689-2691`). Cancelling the waiter doesn't cancel the handler.
- That handler holds bubus's global lock (`bubus/service.py:951`) and sits in `Page.navigate` (`session.py:1235`) for up to `event_timeout` = 30 s (`events.py:122`).
- **Effects:**
  - The next call's navigation queues behind it, so a fast page gets cut and falsely marked `timed_out`.
  - Evals during that window hit the old document, so a page can be credited with the previous page's probe.
  - Tests 10 and 12 can still pass while this happens.
- **Fix:** on a cut, send `Page.stopLoading` directly over CDP, then `await session.event_bus.wait_until_idle(timeout=dl.clamp(2))`.
- **Test:** put `/slow` before `/fast` in the sitemap. Assert `/fast` is not `timed_out` and its probe title is "Fast".

**7. Tests 3 and 4 race on when a frame arrived.**
- `Frame.at` is stamped when the frame reaches Python (`live.py:314`), not when Chrome drew it. A frame drawn before the `scrollTo` and acked after `last_at` passes `f.at > last_at`.
- In a resumed scan that frame sits at y=0 (where `page.py:295` left the page). It adds the most coverage, so `choose_page_frames` picks it, and both tests flake.
- **Fix:** frames carry `scrollOffsetY` (`live.py:311`); also drop any frame with `scroll_y < start_y - 2`.
- **Second issue, present today:** `window.scrollTo(0, y)` (`page.py:256`, `:295`) obeys CSS `scroll-behavior: smooth`, so the jump animates and leaves frames between 0 and `start_y`. Use `scrollTo({top, behavior: 'instant'})` and add a smooth-scroll variant of the band page (about 3 s).

**8. Test 3's lower bound `1 <= from_screen` is load-sensitive.**
- The scan gets 4 − 1 = 3 s. Setup is the eval, the 0.4 s sleep (`page.py:257`) and the pointer glide (60–700 ms, `motion.py:21-22`), about 0.7–1.2 s in total.
- The seeded step plus tail estimate is about 1.8 s on a quiet machine, so there is one wheel. Under load (an eval at 0.2 s) it rises to about 2.9 s, so there are no wheels, `from_screen` comes out as 0.8, and the test fails.
- The property that actually matters is gap-free continuity, and the plan never asserts it. An off-by-0.8-screen bug in `next_y` would pass test 3.
- **Replace tests 3 and 4 with one test:**
  - Use a 10-band page.
  - Chain `PageWatcher.scan(from_screen=…, deadline=Deadline.after(3))` until the stop reason isn't `'time'` (at most 4 calls, about 12 s).
  - Assert that the union of `[scroll_y, scroll_y + vh]` covers `[0, page_height]` with no gap over 2 px, and that the last stop is `'end'`.

**9. Wall-clock assertions in a file with a flake history.**
- `findings-log.md:3450-3460` and `:3504` record load-induced intermittents in this file: the feed overshoot (1 in 30 under load), the beep count, and the play-arena title screen.
- The design never states how far past the deadline a call may run.
- **Fix:** define that overrun bound per tool, have the result report `spent` from the `Deadline`, and assert `spent <= budget + bound` on the system's own clock, not from the test's start.

**10. Estimates are re-seeded every call.**
- `next_est`, `step_est` and `overhead_est` are locals. `_correct_overshoot` can take about 14 s in the worst case (`service.py:907-922`), so a slow feed overruns once in every call.
- **Fix:** keep the learned estimates on `Eyes`/`PageWatcher`. Tests can then pre-seed them deterministically.

## C. Packaging regression

**11. `browser_use/eyes/views.py` is the wrong home for `Deadline`.**
- Importing it from `mcp/server.py` runs `eyes/__init__.py`, which imports `service`, which needs numpy.
- The base MCP server deliberately treats the eyes as optional (`mcp/server.py:738-742`). A top-level import would break `browser-use --mcp` for anyone without the eyes extra.
- **Fix:** put `Deadline` in a module without numpy, e.g. `browser_use/mcp/deadline.py`.
- **Test (about 1 s, run in a subprocess):** `import browser_use.mcp.server, sys; assert 'browser_use.eyes' not in sys.modules`.

## D. What the plan says can't be tested, but can

**12. The backstop needs no mock.**
- Make `backstop_grace_s` and `long_tools` constructor arguments; that is configuration on real objects.
- Use `RetinatServer(call_budget_s=1, backstop_grace_s=0.5, long_tools=LONG_TOOLS | {'retinat_wait'})`. Install a reflex rule that never fires, then call `retinat_wait {'timeout': 10}`.
- Assert:
  - the result is `isError` and isn't just "Error: ";
  - the text says "past its 1s budget";
  - the effect is reported;
  - the whole call took under 3 s;
  - a following `retinat_look` succeeds.
- About 3 s.

**13. A pure planner layer catches the arithmetic instantly.**
- Pull these out as pure functions with instant tests: `scan_step_allowed`, `browse_cap`, `explore_page_allowed`, `speech_seconds_that_fit`, and a round trip of `stop_line` through parsing and `check_arguments`. `test_explore.py:201` already tests `_same_site` this way.
- Let `Deadline` take an explicit `started` and `at`, so a test can build "46 s into a 50 s call" and check the stop line with production numbers.
- This layer would have caught finding 1.

**14. The speech trimming is untested where it matters.**
- All eyes tests use `speech=False` (`test_eyes.py:293-297`). The MCP server builds `Eyes(self.browser_session)` (`mcp/server.py:857`), whose speech default is on whenever faster-whisper is installed, and CI installs every extra.
- So any MCP-level watch or browse timing test would include the first-call model load.
- **Fix:** test the trimming as a pure function. Build any MCP watch/browse test with speech off, or warm the model in a fixture.

## E. Speed and "red for the right reason"

**15. Only tests 1 and 2 fail on an assertion today; the rest fail with a `TypeError`, which proves nothing.**
- `test_mcp_lifecycle.py:38-50` already runs the real server over stdio, and `ClientSession.call_tool(read_timeout_seconds=…)` (`mcp/client/session.py:1004`) can play the client that gives up.
- **The test:**
  - Start the server with `BROWSER_USE_MCP_CALL_BUDGET_S=5`.
  - `retinat_open` a 30-band page.
  - Call `retinat_scan {'max_screens': 80}` with `read_timeout_seconds=8`.
- Today it fails with a client timeout, which is the real symptom. After the change it passes.
- It is also the only test that covers the client's clock, request queuing and the base64 transfer of the sheet, none of which the `_call` helper (`test_retinat.py:100-105`) reaches.
- Chain the explore resume in the same server process to share the start-up cost (about 15 s total). Make that chain assert that fast pages are not `timed_out` (finding 2); with the default explorer options that needs a larger budget or the constructor knob.

**16. Test 5 is the slowest (about 18 s) and may not fail today.**
- Add a `buffer` argument to `PageWatcher` and pass it to `LiveView(buffer=…)` (`live.py:135`).
- With `buffer=8` and a rAF canvas, `seen` saturates at 8 (`page.py:274-278`), the bottom band is never taken in, and the test fails deterministically today. About 4 s.

**17. Test the stuck CDP call deterministically with the existing STALL page.**
- The `STALL` fixture (`test_eyes.py:213`, main thread busy for 10 s) on a tall page reproduces "a CDP call that never returns" without depending on load. `PageWatcher._eval` (`page.py:197-202`) has no timeout today.
- With `Deadline.after(4)`, assert the scan returns in under 5.5 s with `'time'`.
- Make `EVAL_TIMEOUT_S` a `PageWatcher` attribute so the `'unresponsive'` path can be tested in 2 s instead of 10.

**18. Check the boundary between `'end'` and `'max_screens'`.**
- The final collect's `at_end` is discarded today (`page.py:294`). If the last allowed wheel reaches the bottom, the scan would say "call again" for nothing.
- Add to test 2: a 3-screen page needs three wheels; with `max_screens=3` it must report `'end'`.

**19. Make the resume refusal instant.**
- `_ensure_session` (`server.py:768`) runs before the explore branch, so "`resume=true` on a fresh server" launches Chrome just to refuse.
- Check the resume precondition before `_ensure_session`; the refusal test then needs no browser.

**20. Listener restore under a cut.**
- Extend test 9 to assert the `Network.responseReceived` handler is the same object after a timed-out call, as `test_explore.py:147` does for a full run.

**Budget.** Your own estimates for the 13 tests come to about 135 s. That time is serial: `test_eyes.py`'s browser fixture is module-scoped, and `--dist=loadscope` keeps a module on one worker. CI also gives each file a 4-minute job (`test.yaml:84`). With the changes above, about 12 instant pure tests plus 7–8 browser tests (tests 1, 2 with its boundary case, continuity, buffer, STALL, a fixed test 7, merged tests 9 and 10, stdio, backstop) should come to roughly 70–80 s, with all the expensive tests failing on assertions first.

## Minor
- `deadline=None` does not keep today's behaviour. The frame-intake fix, skipping the final collect, the 10 s `_eval` timeout, the new stop line and `transcribe=False` per browse item all change output. Say so, and rely on the existing scan tests (`test_eyes.py:1358-1499`) as the regression check.
- `None` and `Deadline.unbounded()` are two ways of saying "no limit". Normalise once, where the call enters.
- Put all the reserves (1.0 for the sheet, 1.5 for explore, 3.0 watch tail, 0.5 hard cut, 6.0 grace) next to `Deadline` as named constants. Add a pure test that their sum on each path stays under `BACKSTOP_GRACE_S`.
- `HTTPServer(threaded=True)` is supported by pytest-httpserver 1.1.3 (`httpserver.py:637`, `:955`).

The files this concerns:
- /home/user/browser-use-/browser_use/eyes/page.py
- /home/user/browser-use-/browser_use/eyes/service.py
- /home/user/browser-use-/browser_use/explore/service.py
- /home/user/browser-use-/browser_use/retinat/server.py
- /home/user/browser-use-/browser_use/mcp/server.py
- /home/user/browser-use-/browser_use/vision/live.py
- /home/user/browser-use-/tests/ci/test_eyes.py
- /home/user/browser-use-/tests/ci/test_explore.py
- /home/user/browser-use-/tests/ci/test_retinat.py
- /home/user/browser-use-/tests/ci/test_mcp_lifecycle.py

=====

**Verdict:** the overall shape holds up: one deadline per call, passed down explicitly, loops that stop on their own, resume arguments, and a backstop. Most of the design's factual claims check out against the code: `send_raw` waits forever (`cdp_use/client.py:383-389`), the fallback at `page.py:271` would label a timeout "end", and `str(TimeoutError())` is empty. pytest-httpserver 1.1.3 does accept `threaded=True` (`httpserver.py:637`). The 300-frame bug is worse than described: once the deque is full before a scan starts, the first collect takes all 300 stale frames, `seen` becomes 300, and every later `frames[300:]` is empty. So on an animated page, every scan after the first ~30 s sees only frames from before it began. Part of this is already in the working tree (`eyes/views.py`, `LiveView.clock()`, three new scan tests and two retinat tests), and finding 4 breaks one of those new tests.

The weak points are cancellation that never reaches the work, cleanup that has no time limit, and state that can't be told apart between calls. In order of severity:

### High

**1. Cutting an explore page mid-load cancels the wait, not the navigation.**
- `Explorer._navigate` calls `session.navigate_to`, which dispatches on the bubus event bus and then awaits it (`session.py:2689-2691`).
- Awaiting from outside a handler only waits on a signal (`bubus/models.py:281`). Cancelling that wait leaves the handler running, holding the bus's global lock (`bubus/service.py:957`), for up to 30 s (`events.py:122`).
- Results:
  - The next call's first page queues behind it and gets wrongly marked `timed_out`.
  - Every other bus event, including other tools' navigation, stalls.
  - The tab can land on the cut page after explore has already answered.
- `_dom_state_tokens` (`explore/service.py:391`, a bus event) has the same problem.
- Fix: limit the navigation itself. Dispatch `NavigateToUrlEvent(url, timeout_ms=..., event_timeout=dl.clamp(30, reserve=1))`; both fields exist (`events.py:115,122`). Keep the outer cut only for work that is not on the bus, and send `Page.stopLoading` after a cut.

**2. Several CDP waits on these paths still have no time limit.**
- Unbounded today:
  - `_meter` → `Overlay.show` (`overlay.py:90-93`) and `overlay.install` (`:84-88`)
  - `LiveView.start`
  - `touch.viewport` (`Page.getLayoutMetrics`, `touch.py:107-111`)
  - `PageWatcher.capture` (`page.py:216`)
  - mouse and touch input dispatch. `input.py:207-209` itself notes that a mouse move is answered only after the page takes it in.
- On a hung main thread, which is exactly the page just cut, `_meter(i+1, …, 'done')` runs straight after the cut (`explore/service.py:473`) and blocks. The backstop then fires and the whole call's work is lost.
- Fix: one `dl.bounded(awaitable, cap)` helper used for every CDP await on these paths. `_meter` becomes best effort with about 1 s.
- This also gives the backstop a test without mocks, which the design says is impossible. Load a page that busy-loops its main thread for 60 s after load, then run `retinat_scan` with `call_budget_s=6`. Expect `isError`, effect `unknown`, in under about 13 s.

**3. Cleanup after the backstop has no time limit, and under client cancellation it can leave a finger down.**
- `asyncio.timeout` cancels once, then the `finally` blocks await unbounded sends:
  - `touchEnd` (`touch.py:164-170`)
  - `mouseReleased` (`input.py:173-177`)
  - `clearDeviceMetricsOverride` (`explore/service.py:381-385`)
- On a hung renderer, the "hard stop at 56 s" becomes "whenever touchEnd returns".
- The MCP SDK cancels in "interrupt" mode by default (`mcp/shared/jsonrpc_dispatcher.py:272,623-628`). anyio keeps cancelling every await in a cancelled scope, so the release's own await is cancelled and the touch may never be released.
- The next flick then uses a new touch id (`touch.py:153`), so the page sees a second finger, i.e. a pinch.
- Fix:
  - Send each release in its own task and wait for it shielded with a limit: `await asyncio.wait_for(asyncio.shield(task), 2)`.
  - Track `_touch_down` / `_buttons_down` on the input objects.
  - At the start of the next gesture, send `touchCancel` or `mouseReleased` if a release is still outstanding.

**4. Filtering scan frames by `f.at > last_at` still lets stale frames in.**
- `Frame.at` is when the frame arrived (`live.py:314`), not when it was painted.
- Scan 1 restores `scrollTo(0,0)` (`page.py:295`), then runs a synchronous signature loop (`page.py:298-300`) and builds the sheet, blocking the event loop.
- The restore's frame waits in cdp_use's message loop and is stamped when scan 2 first yields, which is after `last_at`.
- That frame has `scroll_y=0` and adds the most coverage, so `choose_page_frames` picks it.
- This breaks the new test `test_a_scan_out_of_time_says_how_far_it_got_and_where_to_go_on`, which asserts `min(scroll_y) >= frm*vh - 2`, and the design's tests 3 and 4.
- Fixes:
  - Do not restore to the top when the scan stops for `time` or `max_screens`; say where the page was left. This also saves the 2 s restore.
  - Drop frames with `scroll_y < start_y - 2`.
  - Stamp frames with an absolute monotonic time and with the screencast's `metadata.timestamp`.
  - Use `scrollTo({top, behavior: 'instant'})` for both the jump and the restore. Under `scroll-behavior: smooth` both animate, and their in-between frames end up in the scan.

### Medium

**5. Coverage can be claimed for screens that were never seen.**
- The stream can die mid-scan in three ways:
  - `retinat_record start` takes over the frame handler without chaining (`recording_watchdog.py:102`). `in_step()` is only re-checked in `PageWatcher.start` (`page.py:184`).
  - A recording stops while the scan shares its stream.
  - The explorer's own `LiveView` sends `Page.stopScreencast` on the same session (`explore/service.py:447-449, 477-479` → `live.py:281`). The `PageWatcher` view stays `_running`, and `in_step()` still says True because the handler slot was handed back. From then on every scan is blind. This already happens today, and resumable explore makes it common.
- The design's "0.0–9.6 of 31.2 screens" line is computed from `reached_y`, so it would claim screens it never saw.
- Fix:
  - In every collect (not just the first), if no frame arrived with `scroll_y` near `y`, call `capture()`.
  - Compute the covered range from the captured frames.
  - The explorer should not stop a screencast when the handler it chained onto is another `LiveView`.

**6. Explore's `pending == []` means both "not discovered yet" and "all explored".**
- If a call stops during the link check and is resumed, `_go` sees an empty `pending` and runs discovery again. Every page is explored twice.
- `report.skipped` (`:324, :329`) is appended to again, and so is the sitemap finding (`:310`, appended rather than merged).
- Fix: `pending: list[str] | None = None`, or a `discovered` flag. The rule "status 0 only after two timed-out calls" also needs a `link_timeouts: dict[str, int]` field, which is missing from the views changes.

**7. Scan can misreport why it stopped.**
- If the 0.3 s re-read (`page.py:289-292`) is skipped when no reserve is left, a wheel that is still settling is reported as `stuck`. That tells the agent "inner scroller, no from_screen", which is wrong. Report it as `time` with `from_screen = reached_y / vh`.
- A `TimeoutError` from the first eval (`page.py:256`) or from `move_to` has to return `PageScan(stopped='unresponsive')`. Otherwise it is an error from inside `act()`, with effect `unknown`.

**8. `clamp()` can return 0, and `wait_for(x, 0)` behaves differently on Python 3.11 and 3.12.**
- On 3.12 the coroutine runs until it first suspends. So `PageWatcher._eval`'s `Runtime.evaluate` (a `scrollTo`, or the `_status` fetch) is sent and its answer dropped.
- On 3.11 (`pyproject.toml:7` allows it) the task is cancelled before it runs.
- Fix: a `Deadline.bound(..., floor=0.2)` that raises `TimeoutError` without sending anything when below the floor.
- Cleanups such as `_hold` and the scroll restore should get a minimum time, not a clamp to zero. A clamped `_hold` lets the video play unseen.

**9. On Python 3.11, `wait_for` can swallow an outer cancellation** when the inner future finishes in the same tick (gh-86296, fixed in 3.12). Every eval on these paths is a `wait_for` (`retina.py:240`, `explore/service.py:117`). The backstop or the per-page cut can then exit without raising. After each `asyncio.timeout` block, check `cm.expired()` explicitly and don't rely on the exception.

**10. The speech budget is per item, not per call.**
- `perceive` starts one `to_thread(_speech)` per item (`service.py:396-406`). If each is sized against `dl.left()`, N items can each use the whole remainder. Size them from one shared pool.
- A thread left running by a cut call keeps the single Whisper model (`asr._model`) busy, and the next call's speech pass waits behind it, eating its budget. Add a module-level "busy" flag and skip with an `untranscribed` note.
- Measure the speed with a running average instead of assuming 6×. Model load (`asr.py:90-99`) is not counted at all.

**11. Browse can stop in states its resume doesn't describe.**
- **Overshoot:** breaking out of `_correct_overshoot` leaves the feed past the next item, and resuming with `items=k` silently skips reels. Either reserve its worst case, about 2 + 3×(2+2.6) + 2 s, or have the stop line name where the feed landed.
- **No time for `next()`:** the text says "call retinat_next first", but the resume dict only says `browse(items=…)`. The design's test 7 (`browse(**p.resume)`, each reel exactly once) then fails whenever two items were watched and the flick didn't fit. Add `next_first: true` to the resume and handle it in `_browse`.
- **Wasted keyframe reads:** each per-item `_watch` still calls `perceive` → `read_keyframes` (`service.py:333, 422`) for a percept that is thrown away. Add a path that only computes the stop reason and `_reported`.
- **Speech reserve:** a per-item watch should reserve speech time based on `transcribe`, not `self.speech`, or it cuts itself short for a speech pass it won't run.

**12. The archiver competes with scan and explore.** `_archive_loop` only holds back while `_watching` is non-zero (`service.py:715`). Scan and explore don't raise it, so every 2 s up to 40 data-URL JPEGs are pulled through the same page thread (`retina.py:260`, 20 s timeout). That inflates the scan's measured step times. Increment `_watching` around scan and explore.

### Low

**13. The signature cost estimate is off when recording.**
- `signature()` decodes each JPEG in full (`page.py:37`).
- With a recording running, frames are full-viewport JPEG at quality 85 (`recording_watchdog.py:104-110`), several times costlier, so the fixed `0.002*len(captured)` term is wrong. Use `img.draft` as `live.py:56` does (check the existing scan tests still pass) and measure the per-frame cost instead.
- CPU work on the event loop also delays the backstop's own timer, so 56 s is not a hard limit.

**14. The backstop's text can contradict its effect.** `effects.failure` takes the effect from the context variable (`effects.py:73-81`). If the cut lands in `_ensure_session` or `_eyes()`, before `act()`, the effect is `none`, while the proposed text says "it may have scrolled or flicked". Make the text depend on the effect. `BrowserUseServer.handle_call_tool` (`mcp/server.py:598-603`) has the same empty-message problem.

**15. Other tools left uncovered.**
- `retinat_wait` (timeout up to 50 s, `server.py:578`) after a cold launch passes 60 s. Clamping it to `deadline.left()` is a one-line fix.
- `retinat_desktop_watch` (`seconds` up to 45, `:247`) is only listed when desktop eyes are on. So the new LONG_TOOLS test passes in CI but fails on a machine with `BROWSER_USE_DESKTOP_EYES=1`.

**16. Parallel calls on one tab are unguarded.** Claude Code can send tool calls in parallel, and a scan and a browse can run on the same tab at once. Add a per-tab busy guard for long and acting tools that refuses the second call (effect `none`) rather than queueing it, since a queued call burns its own budget waiting.

**17. The MCP tests bypass the real dispatcher.** They call `handler.handler` directly (`test_retinat.py:_call`), so neither the backstop running under anyio nor a client cancel is ever exercised. Add one round-trip test over the SDK's in-memory transport (`mcp/shared/memory.py`) that asserts the backstop's result arrives and no touch is left down after `notifications/cancelled`.

**Smaller points:**
- Every timed-out CDP call makes cdp_use log a "duplicate response" warning (`client.py:324-326`).
- `stop_line` given the shortened deadline from `minus(1.0)` would print "49 s"; pass the outer deadline instead.

=====

**Verdict.** The mechanism is sound: one clock per call, checks between steps, clamped CDP waits and a backstop at 56 s. The honesty contract is weaker than the design claims. In several places the partial result reports coverage that was never seen, blames the page for a limit the tool set, or gives a resume line that cannot be followed as one call. Line numbers are from HEAD, which is what the design cites. "(wt)" marks the uncommitted working tree, where an implementation of this design is already under way and already shows some of these bugs.

## High: the result states something false

**1. Scan's "covered" range and `next_y` come from the scroll position, not from frames actually seen.**
- `collect()` only takes frames drawn before its eval (page.py:264-279).
- The design shortens the last dwell "to what the budget allows", possibly to 0. The last wheel position may then have no frame at all, yet the text says the scan covered up to `(reached_y+vh)/vh`, and the resume `next_y = reached_y + 0.8h` skips about 0.6 screen nobody saw.
- (wt) page.py:403 and service.py:846 do exactly this.
- (wt) There is also a zero-collect case: a cold start eats the budget, nothing is collected, and the scan says "after 0 screens, 0.0–1.0 of 1.0 screens down" with no resume. The page height of 1.0 is a placeholder from `max(height, int(h))`.
- A page that jumps more than 0.8 vh per wheel (snap or hijacked scroll) also leaves gaps the range hides.
- **Fix:** compute coverage from the union of `[f.scroll_y, f.scroll_y+vh]` over the frames captured (`choose_page_frames` already builds these spans, page.py:104-129) and name any gaps. Set `next_y = max(f.scroll_y) + vh - 0.2vh`, or `start_y` if nothing was seen. If no frame was taken, raise `Refused` (effect none) with "no time left to start: session start took N s; call again with the same arguments". Don't return a success that says "0 screens".

**2. Browse's resume never shows the glimpsed reel's opening, and an existing "earlier percept" line is false.**
- The design says the next call's `_unreported_since` starts from the glimpsed item's first frame. That only feeds the per-item watch percept, which `_browse` throws away (service.py:1021).
- What browse returns is `perceive(since=start)` (service.py:1036). The 0.6–2.6 s glimpse (settle plus confirm) falls before the next call's `start`, so call 2 says "the first 0:02 was not held". The hook of that reel is lost in both calls.
- Existing bug in the same code: the inner `_watch` marks `_reported` (service.py:341-342). The final perceive then sets `earlier=True` (service.py:436) and prints "before 0:05: in an earlier percept, not repeated here" (percept.py:159-160) for a reel that was already playing when browse began. The agent never received that percept.
- **Fix:**
  - Use `since = self._unreported_since(start, attended_vid)` for the browse percept.
  - Stop the inner watches from writing `_reported`, and mark items only after browse's own perceive.
  - Add a test: browse, stop for time, browse again, and assert the glimpsed reel's `t0 < 1.0`.

**3. Trimming audio before `_speech` makes the VAD relabel the untranscribed span as "sound".**
- The design trims the hops passed to `_speech` (service.py:403-405), which runs `speech_regions` and then Whisper.
- `apply_speech_regions` (hearing.py:462-490) turns every heuristic `speech` segment outside the regions into `sound` and sets `speech_by='vad'`.
- So the trimmed-off span is stated, with the model's authority, to contain no speech. That is the opposite of "not transcribed".
- **Fix:** trim only `asr.transcribe`. The VAD is a few ms per 2 s of audio (service.py:262) and should run on everything. Failing that, apply regions only within the transcribed span and leave `speech_by` heuristic outside it.

**4. Explore: skipping the `_navigate` retry for time creates false high-severity findings.**
- Skipping the retry when `not allows(RETRY_AFTER_S+3)` makes a first-try 502 or 503 into "Link to /x returns 503 on both of two tries 2 s apart" (service.py:586-591 keys off `page.first_try`), or a plain high `broken-link`. Only one try happened.
- **Fix:** when the retry is skipped for time, put the URL back in `pending`, pop `_first_tries`, and don't append the page. This is the same rule the design already applies to `_status` (`None`, stays pending).

**5. Explore: checks skipped or cut short for time disappear without a word.**
- `page.mobile={'skipped': ...}` produces no finding (`mobile.get('overflow')` is falsy, service.py:625-631), and `render_markdown` never prints `mobile`.
- A `_scroll_through` stopped by the deadline means lazy content never loaded. Its broken-image, LCP and overflow results then read as complete.
- A skipped `_dom_state_tokens` counts as 0 in `ExploreReport.dom_state_tokens` (views.py:63-65), so "a DOM-dump step per page would have been ~N" (service.py:680) is understated against a token figure that counts every page.
- **Fix:**
  - Add `PageReport.unchecked: list[str]` (for example `'phone width: no time'`, `'scrolled 3 of 12 screens'`).
  - Render it as a column or a "Checked only partly" section.
  - Compute the DOM comparison over the measured pages only, and say "on 9 of 12 pages".

**6. A timed-out page is reported as a site defect, and "had the whole budget" is not true in call 1.**
- In call 1 the first page runs after start navigation, settle, the environment eval and the robots and sitemap fetches (service.py:455-467). It can be cut having had only about 35 s.
- A medium finding "took longer than one call allows" describes the tool's limit, not the site, and the page's probe checks were never run.
- **Fix:**
  - Mark `timed_out` only if the page started within a few seconds of the call's start; otherwise leave it pending.
  - List such pages under "Not checked (slower than one call)" with the seconds they actually got, not under `## Findings`.
  - Don't count them in "Explored N pages".

**7. Browse's two-step resume can't be written as one call and contradicts the rule the design adds.**
- When there is no time even for `next()`, the text says to call `retinat_next` first. `stop_line(..., resume)` can only express one call, and the AI.md rule the design adds says "call exactly that". An agent that obeys calls browse and re-watches the current reel.
- The Eyes layer also doesn't know tool names: under `BrowserUseServer` the tool is `eyes_next` (mcp/server.py:891), so "retinat_next" is wrong there.
- **Fix:** add a browse argument `next_first: bool` so the resume stays a single call (`items=2, next_first=true`). Have the server, not Eyes, render the sentence naming the tool (see item 15).

**8. A keyframe read cut by the call's clock is blamed on the page, with the wrong figure.**
- `_why_no_answer` always says "the page did not answer in {self.keyframes_timeout:.0f}s" (20 s) "and its main thread is busy or hung" (retina.py:498-510).
- With `timeout=dl.clamp(20, reserve=1)` that could be a 0.3 s wait the deadline imposed. Because `trouble` short-circuits the remaining items (service.py:418-422), every row of the browse sheet then loses its images under a false cause.
- **Fix:**
  - Pass the actual timeout into the message.
  - If the clamp was below the normal timeout, say "no keyframe images: no time left in this call to fetch them".
  - If the clamp is under about 0.3 s, don't ask the page at all.

**9. `next()` breaking for time reports "the feed did not move".**
- The design breaks the `methods` loop when `not dl.allows(2.7)`. The result is `NextResult(moved=False, tries=[...partial])`, which browse logs as "feed did not move (...); stopping" (service.py:1027-1033).
- **Fix:** add `NextResult.out_of_time: bool` plus the methods not tried. Report the same for `_correct_overshoot` when it stops mid-correction: say how many items the feed overshot and which were skipped.

## Medium: true, but incomplete or misleading

**10. Explore's intermediate report reads as final.**
- `## Findings` prints `None.` unconditionally (service.py:691-692), and broken-link findings from the link check only arrive once nothing is pending.
- The findings are cumulative and re-sent on every call, while the sheet holds only this call's pages (`looks[before:]`). Nothing says either.
- **Fix:** write "None so far (12 of 25 pages; links not yet checked)". Mark findings "(new this call)". Label the sheet "pages 13–19 of 25; earlier calls' sheets had 1–12". Print "Links checked: x of y (capped at 80 of z)".

**11. The on-page HUD tells the person watching "finished".**
- After a time stop, `_meter(len(pages), len(pages), ..., 'finished')` (service.py:484) shows "page 12/12 finished" with 13 pages pending.
- **Fix:** show "paused: out of time for this call, 12/25 done", with progress counted over the whole run.

**12. The backstop's text and state are wrong for three of the four tools.**
- "It may have scrolled or flicked" doesn't fit explore or watch.
- The cancelled call leaves the video playing unseen, because `_hold` is not in a `finally` (service.py:331-332, 1034-1035). It can also leave the page scrolled or under the 390 px emulation.
- `self._explorer` is kept only when `act()` returns, so a backstop during explore drops resumable state the server still has.
- **Fix:**
  - Give each tool its own wording: video not paused, page left N screens down, browser on page X possibly at phone width.
  - Set `_explorer` before running, and keep it after an exception if anything is pending.
  - Run `_hold` in a `finally` under a short timeout.
  - Make the grace a constructor argument (`backstop_grace_s`). A test can then set it below zero and check the message and the retained state with real objects; "no direct test" is avoidable.

**13. Any `TimeoutError` gives "Error: " with an empty message, not only the backstop.**
- `effects.failure` uses `str(error)` (effects.py:82-86). Any clamped `wait_for` that leaks out (a walls `json.loads`, `_fetch_text`, `_scroll_through`) hits this.
- **Fix:** in `failure`, use `str(error) or f'{type(error).__name__} (no answer in time)'`.

**14. Resume dicts are hand-picked subsets and may fail the tool's own schema.**
- Watch drops `hold`; browse drops `keyframes` and `hold`.
- A remaining `seconds=0.4` is below the schema minimum of 1 (server.py:283). A capped `max_seconds` could fall below 2 (server.py:308).
- **Fix:** `resume = {**call_args, **progress}`, clamped to the schema. If the remainder is under the minimum, offer no resume and say so. Add a test that every emitted resume validates against the tool's `input_schema`.

**15. Resume format and placement.**
- `k=json.dumps(v)` joined with ", " can't be split safely once a URL contains ", ".
- "Appended, never first" protects against `TEXT_FAILURES`, but that check only runs on `str` results (server.py:647). All four long tools return content lists, so the reason doesn't apply. Browse's header ("browsed N item(s)", service.py:1037) never says the result is partial.
- **Fix:** put "partial: …" in line 1 of all four tools. End with `To go on, call <tool> with {"from_screen": 10.4, ...}`, rendered by the server from `Percept.resume`.

**16. One word, two units.** "after 12 of 25 screens, 0.0–9.6 of 31.2 screens down": `max_screens` counts wheel steps of 0.8 vh, while "screens down" counts viewport heights. Say "12 of 25 scroll steps".

**17. Page height on lazy or infinite pages is given as a fixed total.** "of 31.2 screens" should read "at least 31.2 (it grew from 10.0 while scrolled)" when the height grew during the scan. Risk 8 notes this, but the text never says it.

**18. Hard-coded "(MCP clients drop a call at 60 s)".** This is false when `BROWSER_USE_MCP_CALL_BUDGET_S` raises the budget, and for library callers with no MCP client at all. Derive the line from the budget's source, or leave it out.

**19. `retinat_watch`'s maximum of 45 s can't be reached with speech on.** Under the design's own formula (50 − 3 − 8 − overhead) a watch is cut at about 38 s. Say so up front ("this call can watch at most 37 s; speech needs the rest") instead of reporting "out of time" every time.

**20. A resumed `until=bored` watch is not a continuation.**
- The boredom window restarts from the new call's start (service.py:246-250). A long-form item needs 12 s, so `seconds<12` can never end as bored.
- **Fix:** state this in the line, or resume with `until='time'`.

**21. The ASR budget is spent in the wrong order.** `order` puts backfilled earlier items first (service.py:368-370), so the attended item, the one actually being watched, is the one left untranscribed. Transcribe the attended item first. Also offer recovery, not only disclosure: keep the untranscribed span and transcribe it on the next call while the ring still holds it, instead of moving `_reported` past it.

## Low

**22. `retinat_desktop_watch` is not covered.** It takes `seconds` up to 45 (server.py:239-247), is not in `LONG_TOOLS`, and is not in risk 10. Test 13 lists tools through `tools/list`, which hides desktop tools unless an environment variable is set (server.py:104), so it can't see this one. Build the test from `_browser_tools() + _desktop_tools() + _control_tools()`.

**23. Explore resume ignores arguments without saying so.**
- A new `max_pages` on `resume=true` is ignored.
- A fresh explore without `resume` discards a stopped one.
- `ExploreReport.resume` returns `{'url', 'resume': True}`, which can't be called on the Python API, contradicting "same names as the Python parameters".
- **Fix:** say each of these in the text, and give Python callers `Explorer.resume()`.

**24. A failed scroll-back is not reported.** If the clamped `scrollTo(0,0)` fails ((wt) page.py:375), the page stays part way down and the text doesn't say so. Add "page left at N screens down".

**25. A late call can overlap a retry.** If the backstop misses because CPU-bound PIL or signature work blocks the event loop, a call the client has already given up on can run alongside the agent's retry and scroll the page back to the top mid-scan. Hold a per-`Eyes` lock for scan, browse and watch, and refuse a second call with "another scan running for 52 s" (effect none). It is cheap and honest.

**26. Two minor imprecisions.** The design's "skip the 0.3 s re-read" must label the stop `time`, not `stuck` ((wt) does this correctly; the design text doesn't say it). The stale comment is at service.py:85, not :83.

Files: `/home/user/browser-use-/browser_use/eyes/page.py`, `/home/user/browser-use-/browser_use/eyes/service.py`, `/home/user/browser-use-/browser_use/eyes/hearing.py`, `/home/user/browser-use-/browser_use/eyes/retina.py`, `/home/user/browser-use-/browser_use/eyes/percept.py`, `/home/user/browser-use-/browser_use/explore/service.py`, `/home/user/browser-use-/browser_use/explore/views.py`, `/home/user/browser-use-/browser_use/retinat/server.py`, `/home/user/browser-use-/browser_use/mcp/server.py`, `/home/user/browser-use-/browser_use/mcp/effects.py`, `/home/user/browser-use-/browser_use/eyes/views.py` (wt, new).