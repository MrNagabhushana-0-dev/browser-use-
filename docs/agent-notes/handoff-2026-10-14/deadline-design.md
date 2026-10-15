# Design: a per-call deadline for retinat_scan, retinat_browse, retinat_watch and retinat_explore

Repo: /home/user/browser-use-. I verified the reader maps against the current code and they hold. Nothing was edited or run.

## (1) Design

**One clock per call.**
- The MCP `tools/call` handler creates a `Deadline` on entry, before `_ensure_session`, `_eyes()` and the bridge wait. So a browser launch and opening the retina count against the same budget the client is timing.
- The deadline is passed down as an explicit argument, `deadline: Deadline | None = None`, to `Eyes.scan`, `Eyes.watch`, `Eyes.browse`, `Eyes.next` and `Explorer.run`/`resume`.
- `None` means no limit, which keeps today's behaviour for library callers: `examples/features/eyes_feed.py:88`, `examples/features/explore_site.py:48`, `eyes/bench.py`, and the tests.

**Why not a ContextVar.** `Eyes.open` starts its archiver with `asyncio.create_task` (`service.py:170`). That task copies the context it was created in, so the first call's deadline would follow the archiver forever, already expired. Passing the deadline explicitly is also easier to test.

**Stop on time from inside the loops (the main mechanism).** Each loop checks before its next step whether there is time for one more step plus the work that must come after it (the tail). It stops cleanly with whatever it has and fills in a resume value: the arguments of the next call, using the same names as the Python parameters and the MCP schema.
- **Scan:** `from_screen`, a number of screens down the page. No server state is needed.
- **Browse and watch:** the remaining `items` or `seconds`. No state is needed; the feed position and the held video live in the browser, and `_reported` lives in `Eyes`.
- **Explore:** `resume=true`, using an `Explorer` kept on `RetinatServer`. Its work queue (pending pages, links still to check) and its accumulated report can't fit into an argument.

**Bound every CDP wait by the time left.** Checks between steps can't save a single CDP call that never returns: `cdp_use` `send_raw` waits forever. Every `Runtime.evaluate` and keyframe read on these paths gets `timeout=deadline.clamp(cap)`.

**Server safety net.** For the long tools only, `asyncio.timeout(deadline.left() + BACKSTOP_GRACE_S)` wraps the call, so the hard stop lands at 50 + 6 = 56 s, under the client's 60 s. When it fires, the result is a clear error with effect `unknown`, instead of the client giving up while the server keeps scrolling the page. It is defence in depth only, because it loses the partial work. `retinat_record` and `retinat_desktop_request_access` are exempt.

**One way of saying it.** A single helper writes the stop line for all tools:

`⏳ out of time: stopped at 48.3 s of this call's 50 s (MCP clients drop a call at 60 s) after 12 of 25 screens, 0.0–9.6 of 31.2 screens down. To go on, call again with from_screen=10.4, max_screens=13, keyframes=6.`

- The line is appended, never first, so the `TEXT_FAILURES` check at `server.py:647` never sees it.
- Scan also writes the "to go on" part when it stops at `max_screens`. Today a 40-screen page scanned with `max_screens=25` never says 15 screens were not seen.

**Put no `structured_content` on successful results.** It is unverified whether a client that receives it still shows the image block. The text is the contract, and `Percept.resume` / `ExploreReport.resume` carry the structure for Python callers.

**New module `browser_use/eyes/views.py`** (`explore` already imports from `eyes`):
```python
MCP_CLIENT_LIMIT_S = 60.0
CALL_BUDGET_S = 50.0
class Deadline(BaseModel):  # frozen, extra='forbid'; time.monotonic() clock
	started: float = Field(default_factory=time.monotonic); at: float = math.inf
	@classmethod after(seconds) / unbounded(); bounded; budget_s; left(); spent()
	def allows(self, cost_s: float) -> bool       # left() >= cost_s
	def clamp(self, timeout: float, reserve: float = 0.0) -> float  # max(0, min(timeout, left()-reserve))
	def minus(self, reserve_s: float) -> 'Deadline'  # model_copy(at - reserve_s)
def stop_line(deadline, why, covered: str, resume: dict[str, Any] | None) -> str  # json.dumps each value: resume=true, url="..."
```

## (2) Files and functions to change

**/home/user/browser-use-/browser_use/mcp/server.py (`BrowserUseServer`)**
- `__init__`: add `call_budget_s: float | None = None`, falling back to env `BROWSER_USE_MCP_CALL_BUDGET_S`, then `CALL_BUDGET_S`. Assert > 0.
- `handle_call_tool` (:582): create the `Deadline`.
- `_execute_tool(name, args, deadline=None)` → `_call_eyes_tool(..., deadline)` → pass `deadline=` into `eyes.watch` / `eyes.browse` (:875-890).
- Add the backstop for `{'eyes_watch', 'eyes_browse'}` only. This server also runs agent tools that legitimately take minutes.
- Update the `eyes_watch` / `eyes_browse` descriptions.

**/home/user/browser-use-/browser_use/retinat/server.py**
- `RetinatServer.__init__` (:606): add `call_budget_s` and forward it.
- New constants: `LONG_TOOLS = frozenset({'retinat_scan','retinat_browse','retinat_watch','retinat_explore'})` and `BACKSTOP_GRACE_S = 6.0`.
- `call_tool` (:634): set `deadline = Deadline.after(self.call_budget_s)` as the first statement. Then:
  ```python
  async with asyncio.timeout(deadline.left() + BACKSTOP_GRACE_S if name in LONG_TOOLS else None) as cm:
  	result = await self._call_retinat(name, args, deadline)
  ```
  Add `except TimeoutError` before the generic `except Exception`, and only when `cm.expired()`. Return `effects.failure(name, RuntimeError(f'{name} ran {deadline.spent():.0f}s, past its {budget}s budget, and was stopped before the client would give up; it may have scrolled or flicked: look before calling again'), ...)`. This matters because `str(TimeoutError())` is `''`, which would otherwise produce the message "Error: ".
- `_call_retinat(name, args, deadline: Deadline | None = None)` (:743).
- Scan handler (:828): `eyes.scan(..., from_screen=float(args.get('from_screen', 0)), deadline=deadline)`. Schema (:294): add `from_screen` (number, minimum 0, default 0, "where an earlier scan that ran out of time said to go on").
- Watch (:817) and browse (:832): pass `deadline=deadline`.
- Explore (:778):
  - Keep `self._explorer: Explorer | None` and a `self._exploring` flag.
  - `resume=true` requires a stored explorer with the same `start_url`, `explorer.session is self.browser_session`, and `report.stopped`. Otherwise raise `Refused(...)`; it is raised before `act()`, so the effect is `none`.
  - A second explore while one runs gets `Refused`.
  - Without `resume`, build a new `Explorer`.
  - Run `act(explorer.run(url, deadline.minus(1.5)))` or `act(explorer.resume(deadline.minus(1.5)))`.
  - Render the sheet from `explorer.looks[before:]` only, so this call's tiles aren't paid for again.
  - Keep the explorer only if `report.stopped`.
  - Schema (:589): add `resume: boolean`. I avoid `continue` because it is a Python keyword.
- Update the descriptions of all four long tools (see section 4).

**/home/user/browser-use-/browser_use/vision/live.py**
- Add `LiveView.clock() -> float`, which returns `time.monotonic() - self._started_at`, and/or `frames_after(at) -> list[Frame]`.

**/home/user/browser-use-/browser_use/eyes/page.py**

`PageScan` (:52) gains:
- `stopped: Literal['end','stuck','max_screens','time','unresponsive']`
- `start_y: float`
- `reached_y: float`, the scroll position of the last collect
- `next_y: float | None`, equal to `reached_y + 0.8*h`, or `None` for `end`, `stuck` and `unresponsive`

`_eval(expression, timeout=EVAL_TIMEOUT_S=10.0)`: wrap in `asyncio.wait_for` and raise `TimeoutError`. Do not return `None`, because the `[0, True, 0, []]` fallback at :271 would label the scan "end".

`scan(max_screens, dwell_s, keyframes, from_screen: float = 0.0, deadline: Deadline | None = None)`:
- **Start position.** Take `last_at = live.clock()` before the first eval, then run `window.scrollTo(0, {from_screen}*innerHeight), [innerWidth, innerHeight]` in the same single eval.
- **First-step estimate.** Time that first eval as `probe`. Seed `step_est = dwell_s + 3*probe + 0.3`, then grow it to the longest iteration actually measured.
- **Frames and signatures in `collect()`.** Take frames with `f.at > last_at`, not `frames[seen:]`. This fixes two things: frames left over from before the scan (earlier looks, an earlier URL), and the 300-frame deque filling so the scan silently takes no new frames. If the first collect has no fresh frame (a still page), use `await self.capture()`. Compute `signature()` for each frame and append it to `samples` inside `collect()`, so the tail cost doesn't grow with the number of frames.
- **Checks.** At the top of each loop iteration: `if not dl.allows(step_est + tail_est): stopped='time'`. After `collect()` and before `hand.wheel`, check again for the cost of the wheel plus the read. Here `tail_est = min(dwell_s, 0.3) + 2*probe + 0.5 + 0.002*len(captured)`.
- **Stuck check.** Skip the 0.3 s re-read when there is no reserve left.
- **Tail.** Shorten the dwell to what the budget allows. Do the final collect only if a wheel happened since the last one. The `scrollTo(0,0)` restore uses `dl.clamp(2)`, best effort.
- **Errors.** Catch `TimeoutError` from `collect()` and set `stopped='time'` if the deadline has passed, otherwise `'unresponsive'`.
- Assert `from_screen >= 0`.

**/home/user/browser-use-/browser_use/eyes/percept.py**
- `Percept`: add `resume: dict[str, Any] | None = None`.

**/home/user/browser-use-/browser_use/eyes/service.py**

`scan(..., from_screen=0.0, deadline=None)` (:828):
- Call `watcher.scan(..., deadline=dl.minus(1.0))`; the 1 s is reserved for the sheet.
- Set `stop_reason = result.stopped`.
- Line 1 says the range covered: `{start_y/vh:.1f}–{(reached_y+vh)/vh:.1f} of {height/vh:.1f} screens`.
- Add `stop_line(...)`.
- Resume: `time` gives `{'from_screen': floor1(next_y/vh), 'max_screens': max_screens - screens, 'keyframes': keyframes}`; `max_screens` gives the same without `max_screens`.
- `stuck` says the page stopped scrolling (an inner scroller, scroll lock or overlay) and offers no `from_screen`.
- `unresponsive` says the page stopped answering.

`_watch(..., deadline=None)` (:292):
- `stop_at = min(start + seconds, dl.at - WATCH_TAIL_S (3.0) - asr_est)`, where `asr_est = min(8, audio_s/6)` when speech is on.
- Ticks use `wait_for_data(min(0.5, stop_at - now))`.
- When it stops early, `reason = f'out of time: watched {e:.0f}s of the asked {seconds:.0f}s'` and `resume = {'seconds': ..., 'until': ..., 'detail': ...}`.
- `_hold` and `_resume_held` (:860-870) take `timeout=dl.clamp(10)`.
- `_show_page` uses `wait_for(dl.clamp(5))`.
- Pass `deadline` to `perceive`.

`perceive(..., deadline=None, exclude: set[int] = frozenset())`:
- Before `to_thread(_speech, h)`, trim the input to the newest hops that fit in `(dl.left() - 1.5) * 6` seconds of audio, and set `untranscribed='the first N s were not transcribed: no time left in this call'`. A thread started with `to_thread` can't be cancelled, so it must be sized before it starts.
- Call `read_keyframes(seqs, timeout=dl.clamp(keyframes_timeout, reserve=1.0))`.
- Drop the items in `exclude`.

`next(..., deadline=None)` (:948):
- Before each method, and inside the `_correct_overshoot` tries, `break` if `not dl.allows(2.7)`.
- Call `page_state` under `dl.clamp(10)`.

`_browse(..., deadline=None)` (:1004):
- Before each item: `cap = min(max_seconds, dl.left() - final_reserve - (next_est if more else 0) - overhead_est)`. Stop if `cap < min_seconds + 0.5`. Seeds are `final_reserve = 3` plus `asr_est`, `next_est = 3.0` grown to the longest `moved.seconds + 0.5` seen, and `overhead_est = 1.5` grown to the measured value.
- Run each item with `_watch(seconds=cap, ..., transcribe=False, deadline=dl)`. The per-item percept is thrown away except `stop_reason`, so its Whisper pass is pure waste today.
- Log an item that hit `cap < max_seconds` as `cut short by this call's time limit`.
- When there is no time to watch another item but there is time for `next()`, flick and then stop. The feed then rests on an unwatched item, and `_hold` pauses it. Pass that item's vid as `exclude` to the final `perceive`, and don't mark it in `_reported`. The next call's `_unreported_since` then starts from its first frame.
- With no time even for `next()`, stop without flicking. The text says to call `retinat_next` first.
- Count items with an explicit `covered` counter. Today the header count at :1037 is derived from log lines, so adding a log line would miscount.
- Resume: `{'items': items - covered, 'max_seconds': ..., 'min_seconds': ..., 'detail': ...}`.
- Fix the stale comment at :83 ("90 s at most" should be 45).

**/home/user/browser-use-/browser_use/eyes/retina.py**
- `read_keyframes(seqs, timeout: float | None = None)` (:251).
- Give `_why_no_answer` the clamped probe time.

**/home/user/browser-use-/browser_use/explore/views.py**
- `PageReport`: add `timed_out: bool = False`.
- `ExploreReport`:
  - `pending: list[str] = []` (pages found but not yet explored, in crawl order)
  - `links_pending: list[str] | None = None` (`None` means the link check hasn't started)
  - `stopped: Literal['', 'time'] = ''`
  - `calls: int = 0`
  - a `resume` property returning `{'url': start_url, 'resume': True}` or `None`
- Keep `skipped` for robots.txt and the page cap only. "Call again" and "raise max_pages" are different instructions.

**/home/user/browser-use-/browser_use/explore/service.py**
- Split `run` into:
  - `run(start_url, deadline=None)`, which creates `self.report`;
  - `resume(deadline=None)`, which asserts there is something to resume;
  - `_go(deadline)`, which stores `self._deadline` for the call.
- `_go` setup, every call: get a fresh `_cdp`, run `_listen`, install the overlay once (`_overlay_installed`), and start `LiveView`.
- Discovery (:455-467) runs only when nothing is pending yet; `_fetch_text` uses `clamp(10)`.
- Page loop over `report.pending`:
  - Soft check: `est = max(mean page.seconds, 2*settle_s + 2.5)`; if `not dl.allows(est)`, stop.
  - Hard cut: `async with asyncio.timeout(dl.left() - 0.5)` around `_page`.
  - On `TimeoutError`, don't append the page (an empty probe produces false `unreachable`/`head`/`h1` findings at :570-618) and run `_first_tries.pop(url)`.
  - The one exception is the first page of a call, which had the whole budget. Record it with `timed_out=True` and move on, so each call always makes progress. `_findings_for` turns that into a single medium "took longer than one call allows" finding and no probe findings.
- `_eval` defaults to `self._deadline.clamp(30)`. The probe uses `clamp(45)`.
- Skip `_dom_state_tokens` when time is tight; it only feeds the cost comparison.
- `_scroll_through` checks the deadline on every screen.
- `_mobile_check`: skip it with `page.mobile={'skipped': 'no time left in this call'}` when `not allows(settle_s + 2)`.
- The `_navigate` retry is skipped when `not allows(RETRY_AFTER_S + 3)`.
- `_check_links`:
  - Runs only once `pending` is empty.
  - Fills `links_pending` once and says so when the 80-link cap applies.
  - Checks the deadline before each link.
  - `_status` returns `int | None`, where `None` means it didn't finish (including a skipped retry). Such a link stays pending. Write status 0 only for a link that timed out in two separate calls.
- `finally`: also clear the device-metrics emulation (best effort), then set `report.seconds += now - call_start`, `report.calls += 1`, and `report.stopped`.
- `render_markdown`: put `stop_line` second when stopped. Write "Explored N pages in Xs over K calls" and add a "Not yet explored: N" line. Keep `## Findings`, `tokens read` and the pages table, which `test_explore.py:138` asserts on.
- Update the module docstring example.

## (3) Tests to write first (real browser, pytest-httpserver, no mocks)

Keep them fast: library tests pass a small `Deadline.after(3–8)`, and MCP tests use `RetinatServer(call_budget_s=6)`. Timing asserts use `budget + 1.5–2 s`.

Two tests fail on an assertion today (they also guard the frame-intake fix). The others fail with a `TypeError` until the new arguments exist.

**In /home/user/browser-use-/tests/ci/test_eyes.py** (fixtures `eyes`, `session`, `site`, `_open`):
1. `test_a_scan_shows_nothing_from_before_it_began` (fails today).
   - Open the existing `/bands` page and `_page_watcher().scan(max_screens=3, dwell_s=0.3)`.
   - Navigate the same tab to a new all-grey `/grey` page and scan again.
   - Assert that no frame's centre pixel has a band colour. `Eyes.open` never resets `_pages` (service.py:160-183), so today the first collect takes in the bands frames.
   - About 6 s.
2. `test_a_scan_stopped_by_max_screens_says_what_it_did_not_see` (fails today).
   - A 20-band page, `eyes.scan(max_screens=2)`.
   - Assert `stop_reason == 'max_screens'` and `'call again with from_screen='` in the text.
   - About 4 s.
3. `test_a_scan_out_of_time_says_how_far_it_got_and_where_to_go_on`.
   - 20 bands, `eyes.scan(max_screens=40, deadline=Deadline.after(4))`.
   - Assert: it took less than 5.5 s; `stop_reason == 'time'`; `p.image` is present; `1 <= resume['from_screen'] <= 6`.
   - Then `eyes.scan(**p.resume, deadline=Deadline.after(4))`: every keyframe in the "frames at …" list is at or below `from_screen - 0.05`.
   - About 9 s.
4. `test_a_resumed_scan_holds_no_frame_from_above_where_it_began`.
   - At `PageWatcher` level with `dwell_s=0.3`: `min(f.scroll_y for f in scan2.frames) >= scan2.start_y - 2`.
   - About 6 s.
5. `test_a_long_scan_of_a_page_that_never_stops_drawing_still_reaches_its_bottom`.
   - 14 bands plus a fixed 40 px rAF canvas; it should produce more than 300 screencast frames.
   - `PageWatcher.scan(max_screens=16)`.
   - Assert the largest `scroll_y` is at least `page_height - vh - 2` and the last band's colour is among the keyframes.
   - About 18 s. It catches the deque saturation; whether it fails today depends on the screencast frame rate.
6. `test_a_scan_of_a_page_drawing_few_frames_a_second_still_answers_in_time`.
   - Use the 400 ms busy-rAF script from `test_human_input.py:154`.
   - `eyes.scan(max_screens=40, deadline=Deadline.after(8))`: it took less than 9.5 s, `stop_reason` is `time` or `end`, and there is an image.
   - About 9 s.
7. `test_browse_out_of_time_returns_the_reels_it_watched_and_goes_on_from_the_next`.
   - `/feed` with `browse(items=3, max_seconds=4, min_seconds=1, deadline=Deadline.after(8))`.
   - Assert: it took less than 9.5 s; 1–2 items; the captions are a prefix of `['@first','@second','@third']`; `resume['items'] == 3 - n`.
   - Then `browse(**p.resume)`: together the two calls cover all three reels exactly once.
   - About 18 s.
8. `test_a_watch_out_of_time_is_cut_and_says_so`.
   - A looping player with `watch(seconds=30, until='time', deadline=Deadline.after(4))`.
   - Assert: it took less than 5.5 s; `stop_reason.startswith('out of time')`; `resume['seconds'] >= 20`; the video is held.
   - About 5 s.

**In /home/user/browser-use-/tests/ci/test_explore.py** (an inline `BrowserSession`, like the gateway test at :184; use `HTTPServer(threaded=True)` — I haven't confirmed pytest-httpserver accepts that kwarg — so a sleeping handler doesn't stall the other fetches):

9. `test_an_exploration_out_of_time_keeps_what_it_did_and_resumes`.
   - `/` links to `/p1`…`/p5`, each handler sleeping 0.6 s. Use `Explorer(settle_s=0.3, mobile=False, scroll=False, dom_state=False, hud=False).run(url, Deadline.after(4))`.
   - First call: it took less than 5.5 s; `stopped == 'time'`; `pending` is non-empty; every reported page has a probe and no error; the markdown has `resume=true`.
   - Then `await explorer.resume()`: `stopped == ''`; the pages are exactly the discovered set with no duplicates; `calls == 2`; the link check is done.
   - About 10 s.
10. `test_a_page_slower_than_a_whole_call_is_reported_once_not_retried_forever`.
    - `/slow` sleeps 8 s. Call it repeatedly with `Deadline.after(4)`.
    - It completes within 4 calls, `/slow` appears once with `timed_out`, and there is no `unreachable` finding for it.
    - About 14 s.

**In /home/user/browser-use-/tests/ci/test_retinat.py** (a `retinat_short` fixture, a copy of `retinat` at :91 with `call_budget_s=6`; the `_call`/`_text` helpers):

11. `test_a_long_scan_through_mcp_answers_within_the_budget`.
    - `retinat_open` a 30-band page first, so no cold launch skews the timing. Then `retinat_scan {'max_screens': 80}`.
    - Assert: it took less than 7.5 s; `not isError`; there is an `ImageContent`; `'call again with from_screen='` is in the text.
    - Call again with the parsed arguments and assert `not isError`.
12. `test_retinat_explore_resumes_where_the_last_call_stopped`.
    - Slow-page site. Call with `resume: True` until the text no longer says "call again" (at most 5 calls). Each call takes less than 8 s, and the last text says "over N calls".
    - `resume=true` on a fresh server returns `isError` with effect `none`.
13. `test_every_tool_that_can_run_long_answers_within_the_call_budget` (static, instant).
    - Every listed tool with any of `max_screens`, `items`, `max_pages`, `seconds` or `max_seconds` is in `LONG_TOOLS`.
    - Keep it next to the existing check at :785 and keep that test.

**The backstop has no direct test.** Firing it would need a tool that ignores its deadline, and the no-mock rule rules out faking one. Tests 9, 10 and 12 exercise the same `asyncio.timeout` cancellation and cleanup through the explorer's hard per-page cut. Afterwards, run the full `tests/ci`. The existing scan, browse and explore tests pass `deadline=None` and should be unchanged.

## (4) Docs to update
- **Tool descriptions in `retinat/server.py`:**
  - `retinat_scan` (:292): "answers within about 50 s; a page too long for one call says `from_screen=` to go on".
  - `retinat_browse` (:303): "stops with the items watched and `items=` to go on".
  - `retinat_watch` (:275): "may be cut for time; held, call again".
  - `retinat_explore` (:590): "may stop at about 48 s with what it found; call again with the same url and `resume=true`".
- **Descriptions in `mcp/server.py`** for `eyes_watch` and `eyes_browse`.
- **AI.md:** table rows :194, :197, :207; a rule near :295 ("A long call answers by about 50 s; when it says 'call again with …', call exactly that"); and the explorer library section :328-338 (`deadline=` and `resume()`, with no limit by default).
- **.claude/skills/retinat/SKILL.md:** :23 (scan), :39 (browse), :45-46 ("returns a report plus a sheet of every page" becomes "…of this call's pages; resume=true").
- **.claude/agents/retinat-browser.md:** :13, :15, :36, plus one line on following "call again with" lines.
- **docs/hands-on/2026-10-14/notes.md:284:** replace "Not fixed" with the fix and its commit.
- **Code comments and docstrings:** `eyes/service.py:83` (stale 90 s) and the module docstrings of `eyes/service.py:14` and `explore/service.py:3`.
- **examples/:** no change. The default stays unbounded.

## (5) Risks
1. **How long a step takes varies about 5×**: about 1.1 s per screen normally, 5–8 s on 1–3 fps WebGL, and about 7 s to over 60 s per explore page. A plain "now > deadline" check overruns by a whole step plus the tail. That is why the estimate is measured, the CDP waits are clamped, explore has a hard per-page cut, and the backstop exists.
2. **The frame-intake fix changes scan output.** Leftover frames disappear and long scans keep their lower frames, so the "moves on its own" and coverage numbers can shift. Existing scan tests (:1358–1499) must pass unchanged. Memory grows with frame count (about 1000+ frames of about 40 KB in a 50 s animated scan). If that matters, subsample consecutive frames taken at the same `scroll_y`.
3. **Trimming speech loses words for good.** `_reported` moves past them and recall is frames only. The `untranscribed` line names how many seconds were skipped. Warming the model in `Eyes.open` when speech is on would remove the first-call outlier (model load or download, `asr.py:90-99`).
4. **Browse's flick-and-hold** leaves a glimpse of the next item. `exclude` keeps it off this call's sheet and out of `_reported`. If even `next()` doesn't fit, the agent needs two calls: `retinat_next`, then browse. Items capped below 12 s can never get "bored" on long-form video, and talking heads never do; those are labelled "cut short".
5. **Cancellation semantics.** `asyncio.timeout` cancels once, so `finally` cleanup (touchEnd, mouseReleased, `clearDeviceMetricsOverride`) completes. A client `notifications/cancelled` cancels at the anyio level, so later awaits inside `finally` are lost, and code outside `finally` (`_hold`, `scrollTo(0,0)`) never runs. Hence the emulation clear at the start of each explore call.
6. **The explore resume state is lost on a server restart or a session change.** `resume=true` is then refused with effect `none` and the advice to start again. An overlapping explore is refused. Two concurrent scans or browses still share one `PageWatcher` and interleave wheel input; that is not addressed here.
7. **The budget figure.** 50 s matches the observed 60 s client limit. Clients configured longer (Claude Code appears to read `MCP_TOOL_TIMEOUT`, unverified) can raise it with `BROWSER_USE_MCP_CALL_BUDGET_S`. Don't rely on progress notifications: the TS SDK's `resetTimeoutOnProgress` is opt-in. The client times from when it sent the request and the server from when its handler started; the base64 transfer of a large sheet takes some of the remaining 10 s.
8. **Resuming with `scrollTo(0, from_screen*h)` only works for document scrolling.** Inner scrollers and scroll-jacked pages end as `stuck`. A jump skips scroll-triggered animations above the start point, so 0.2 screen of overlap is kept and `from_screen` is rounded down. On infinite-scroll pages the reported height is a lower bound.
9. **Explore correctness traps:**
   - A page cut mid-way must not be reported.
   - A link check that didn't finish must not be stored as status 0, which becomes a false high finding.
   - A cut attempt must not leave a `_first_tries` entry, which would read as "flaky".
   - The link check must not run while pages are still pending.
   - Uncaught exceptions that already lose a whole run (the walls `json.loads` at :410, the `_scroll_through` evals, `_fetch_text`) now surface more often as clamped `TimeoutError`s. Catch them per page or per phase.
10. **Other tools can still pass 60 s** and are not covered by this change: `retinat_desktop_request_access` (120 s consent, `desktop/consent.py:55`), `retinat_open` with its Tor retry, and `retinat_record` stop.