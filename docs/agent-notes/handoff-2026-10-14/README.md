# Handoff, 2026-10-14 ~15:45 UTC

**Status, 2026-10-19:** PRs #11 and #12 are merged. Of the pending work below:
- item 1 is done (`2925247`, in PR #12);
- item 2 ran on `2925247` and was red on the screen-recorder intermittent;
- item 3's browse and watch half is done (`75a7dc1`);
- the scan sheet's bottom bias is fixed (`e0ed5e3`);
- item 5's cold-start `retinat_open` failure is fixed.

What is still open is in the findings log, Round 57, "Next, in order".

The owner hit their usage limit and asked, verbatim:

> Do u mind urjent raising a new pr with everything on ur vm? Including my prompt & pending work

This folder is that handoff. It sits on branch `claude/laughing-hawking-2cj3a6-handoff`, which is
`claude/laughing-hawking-2cj3a6` (PR #11) plus the commits below. Read this first, then
`docs/agent-notes/findings-log.md` (Round 56 onward) and `AI.md`.

## The owner's prompts

**The governing request for the games (verbatim):**

> For testing I need u to play pokey games car race temple run, subwaysurf / U need to score highest... This proves ur
> capablities of realtime vision working! / So this is tests!!! / Not zooming in a louzy website...

**The hourly routine** (trigger `trig_01SRdqLV1puf2iUXoGsvcmP9`, "Hands-on eyes loop (hourly): use it, find bugs, fix,
record"), its stored prompt verbatim:

> Unattended hourly iteration. The owner asked: be a hungry beast acquiring knowledge, make the eyes (browser_use/eyes +
> Retinat) the world's number one, keep digging. Test by USING it hands-on, not by running test suites for their own
> sake.
>
> Each run:
> (0) Check `git status` and that branch claude/laughing-hawking-2cj3a6 matches origin. If its PR was merged,
> fast-forward the branch to origin/main (git merge --ff-only) before new work and open a new PR for new work.
> (1) Hands-on: drive a real browser only through the Retinat and browser-use MCP tools (retinat_open, retinat_look,
> retinat_watch, retinat_find, retinat_click, retinat_browse, retinat_recall, retinat_search, retinat_changes,
> browser_* ...) on real public websites and media you may view (Wikipedia, news front pages, open-licence video such
> as Blender open movies or Wikimedia Commons, docs sites, public web apps). No Playwright, Puppeteer, Selenium or
> ad-hoc automation scripts. Look for anything the eyes get wrong, miss, or make slow or expensive.
> (2) Record each hands-on session as a short video (screen recording of the browser the tools drive) and commit it
> under docs/hands-on/<date>/ with a short notes.md: what was tried, what the eyes saw, bugs found. Keep each video
> small (a few MB; trim or lower frame rate).
> (3) Each bug found: research it (primary sources 2024-2026 when relevant), fix it test-first per CLAUDE.md (real
> browser, pytest-httpserver, no mocks), and run only the affected test files. Run the full tests/ci (with
> BROWSER_USE_PROXY_CA_CERT=/root/.ccr/agent-proxy-ca.crt) only before updating a PR that changed library code, not
> every hour.
> (4) Between bugs, research and build what would make the eyes the best: compare against the strongest agent-vision
> and browser agents of 2025-2026 and close the gaps, one tested slice at a time.
> (5) Commit and push each working slice; keep the PR description and docs/agent-notes/findings-log.md current
> (measured vs not, and a "Next, in order" list).
>
> Boundaries: no bot-detection or CAPTCHA bypass, no human-disguise features, no automated logins, no Playwright, no
> downloading media the owner has no rights to. Never push red without saying so.
>
> End with a short report: what was used hands-on, bugs found and fixed, videos added, what is unverified, what is
> next.

The 13:42, 14:42 and 15:42 firings of this routine were not worked: the session was waiting on test runs, and then
the usage limit was hit.

**Earlier in the session** the owner sent four repository links (HeyPuter/everything, ImHex, Awesome-Hacking,
morluto/rea). Their exact wording is not in this folder. Cloning untrusted repos was refused by the environment; the
ideas taken from their READMEs are in the findings log ("Ideas from the owner's links").

Ultracode was on for the session (use the Workflow tool for substantive tasks; token cost not a constraint).

## State of the code

| Commit | What | Verified |
|---|---|---|
| `b5b1180` (pushed to PR #11) | MR Racer notes, two clips, `mr-racer-rule-v2.js` | docs only |
| `bdd1d87` (this PR) | **A per-call budget for Retinat.** `Deadline` (`browser_use/eyes/views.py`), 50 s per call (`RetinatServer(call_budget_s=...)` or `BROWSER_USE_MCP_CALL_BUDGET_S`), a backstop 6 s past it for `LONG_TOOLS`, and a deadline-aware `retinat_scan` with `from_screen` and "call again with ..." | `tests/ci/test_eyes.py` + `tests/ci/test_retinat.py`: **127 passed, 0 failed** (10m05s). Full `tests/ci` **not run** on it. |
| handoff commit | this folder, the findings-log entry, the workflow scripts | docs only |

The last full `tests/ci` that ran was on `077c38d`: 1,738 passed, 31 skipped, 0 failed.

## Pending work, in order

1. **Apply and test `pending-patch-slice-a2.py`** (written, not applied, not run). From the repo root:
   `python3 docs/agent-notes/handoff-2026-10-14/pending-patch-slice-a2.py && uv run ruff format browser_use tests/ci`
   (checked at handoff: it applies cleanly to `bdd1d87`, ruff check and pyright pass). It:
   - raises `Eyes._watching` around a scan so the archiver keeps out of its way;
   - captures a picture in `collect()` when no frame came from the current scroll position (the stream can stop under
     a scan: a recording taking it over, the explorer stopping its view on the same tab), so the covered range is what
     was seen;
   - gives the final scroll-to-top at least 0.5 s;
   - drops the backstop message's guess about the effect (`effects.failure` states it);
   - clamps `retinat_wait` to the call's budget (after a cold launch a 50 s wait passed 60 s), threading the deadline
     into `_reflex`;
   - replaces the backstop test with one that shows both: the wait ends with a 1 s budget by itself, and
     `retinat_watch` (not yet deadline-aware) is stopped by the backstop at ~1.5 s and says so.

   Then run, with Retinat's browser parked on about:blank:
   `timeout 900 uv run pytest -q -o addopts="" tests/ci/test_eyes.py tests/ci/test_retinat.py -p no:cacheprovider`.
2. **Full `tests/ci` before pushing library code**, after restoring the original browser config (see below):
   `BROWSER_USE_PROXY_CA_CERT=/root/.ccr/agent-proxy-ca.crt PYTHONPATH=docs/agent-notes/e2e timeout 4200 uv run pytest -p loopwatch -v --maxfail=20 -o addopts="" tests/ci`
   (~39 min).
3. **Deadline slice B**, per `deadline-design.md` section (2), corrected by `deadline-critiques.md`:
   - `retinat_browse` / `retinat_watch`: `_watch` `stop_at`, a per-item cap in `_browse` (as a pure planner function
     with a table test), `next()` checks, the speech pass sized from one shared pool before it starts, `read_keyframes`
     timeout, a `next_first` resume when there was no time to flick;
   - `retinat_explore` resume: split `Explorer.run` into `run`/`resume`/`_go`; `ExploreReport.pending: list | None`
     (None = not discovered yet), `links_pending`, `link_timeouts`, `stopped`, `calls`; bound navigation itself with
     `NavigateToUrlEvent(timeout_ms=..., event_timeout=...)` and `Page.stopLoading` on a cut (cancelling the wait does
     not cancel a bubus handler);
   - move `Deadline` to a numpy-free module before `browser_use/mcp/server.py` (`eyes_watch`, `eyes_browse`) uses it;
   - shielded, time-limited release of a touch or mouse button after a cut, and `touchCancel` at the next gesture;
   - a per-tab busy guard so parallel long calls on one tab are refused rather than queued;
   - docs: `AI.md`, `.claude/skills/retinat/SKILL.md`, `.claude/agents/retinat-browser.md`.
4. **Games** (the owner's test of real-time vision). Best so far:
   - Subway Surfers 323 (206 with rules only);
   - Temple Run 2: past the zipline with the 480 px pad rule, no score read;
   - MR Racer 159 (challenge), 109 (Endless).

   Next:
   - MR Racer rule v3: look at the target lane before changing lanes (a box beside the roof on that side), and keep
     to a lane by its lines;
   - Temple Run: press as the key lights, not after it; take corners; read the score;
   - Subway Surfers: a rule for the Left/Right tutorial step (read the prompt's arrow), then a run with rules only.

   The tuning scripts are in `game-tuning/` (offline: extract frames from a recording with the imageio_ffmpeg binary,
   then simulate rules in numpy/PIL). Committed rules: `docs/hands-on/2026-10-14/*.js`.
5. The rest of the findings log's "Next, in order" list:
   - a screen feed of only the regions the rules read;
   - the cold-start `retinat_open` failure after a container restart ("BrowserStartEvent ... timed out after 30.0s",
     the retry worked);
   - the intermittents to watch.

## Environment notes for whoever resumes

- **Browser config.** `/root/.config/browseruse/config.json` currently has the game viewport 960x540; the original is
  in the session scratchpad as `config.json.bak`. A fresh container will not have the change. Before a full CI run,
  use the original (the default profile, no viewport override); for games, set `viewport` and `window_size` to
  960x540. Smaller is faster: WebGL renders in software, with no GPU.
- **Stopping Retinat.** Kill it by PID only, never `pkill -f`:
  `ps -eo pid,cmd | grep -E "^ *[0-9]+ /home/user/browser-use-/\.venv/bin/python3? -m browser_use\.retinat" | awk '{print $1}' | while read p; do kill $p; done`.
  Restart it after library changes, before hands-on.
- **Park the browser before tests.** Retinat's browser must sit on about:blank while tests run: a game rendering in it
  pushed the reflex speed test over its limit.
- **Send calls in sequence.** Send `retinat_look` and `retinat_wait` in separate turns: sent in one block, they run in
  parallel.
- **What was left out.** The scratchpad (7.1 GB: test logs, frame dumps, recordings, model caches) is not in this PR.
  Only the small text files that carry pending work were copied here.
- **Workflow scripts.** The scripts for the deadline design and the slice A review are in
  `docs/agent-notes/workflows/`. The review run returned nothing: its three agents failed on the usage limit.
