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
