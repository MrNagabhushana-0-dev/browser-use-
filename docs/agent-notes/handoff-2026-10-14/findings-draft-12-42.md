**12:42 round, MR Racer again, and a scan that answers within the call.**
- After a container restart the first `retinat_open` failed ("BrowserStartEvent ... timed out after 30.0s", a cold
  Chrome start); the second worked. Not looked into yet.
- MR Racer Endless: our car drives on the road's solid centre divider. Throttle only scored 101; one Right press at
  9 s into the right lane scored **109** (rear-ended a slower car); rule v2, which changes lane when the dark count
  in a box over our roof passes 15, scored 82 (it moved into a lane with a van in it). The roof box reads 0 while
  the lane is clear and climbs 3 → 80 over ~4 s before a rear-end: a threshold of 15 fires ~3 s ahead.
- **Built (test first): a call budget for Retinat.** MCP clients drop a call at 60 s, and `retinat_scan` was lost
  that way twice. Each call now gets a `Deadline` (50 s, `RetinatServer(call_budget_s=...)` or
  `BROWSER_USE_MCP_CALL_BUDGET_S`). `retinat_scan` stops in time with the frames it has, says how far it got and
  `call again with from_screen=...`; a backstop stops any long tool 6 s past the budget with a clear error.
  - Found while writing it: a scan kept frames from before it began (the stream's earlier frames: a scan of a grey
    page showed the bands of the page before), and frames drawn before its jump to `from_screen` but acked after.
    Both now dropped; the jump is `behavior: 'instant'`, so a page with smooth scrolling does not animate through it.
  - Tests: a scan shows nothing from before it began; one stopped by `max_screens` says where to go on; one out of
    time says how far it got; chained scans cut by a 3 s deadline cover a 10-screen page with no gap (a resume point
    moved to 1.5 screens on fails it: "nothing seen from 2808 to 3348 px down"); through MCP with a 6 s budget a
    30-screen scan answers in under 8 s; a wait held to a 1 s budget is stopped and says so; every tool with a count
    of screens, items, pages or seconds is under the budget.
  - Not yet under the deadline: `retinat_browse`, `retinat_watch`, `retinat_explore` (their maxima are already 45 s,
    so a single call stays inside 60 s, but a browse of many items or an explore of many pages does not).
  - From the design review, still open: `Deadline` lives in `browser_use/eyes/views.py`, which pulls numpy in through
    `browser_use/eyes/__init__.py`; before the base MCP server (`mcp/server.py`) uses it, it moves to a numpy-free
    module.
