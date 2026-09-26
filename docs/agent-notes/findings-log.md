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
