---
name: retinat
description: See and operate a real browser through the Retinat MCP server (browser_use.retinat) - watch videos/reels with their sound, scan canvas/WebGL pages as drawn, browse short-video feeds with touch, explore a whole site for bugs. Use whenever a task needs to look at a web page, watch or listen to web media, scroll a feed, or audit a site, and never write Playwright/Puppeteer/Selenium code instead.
---

# Retinat: browser eyes for the model

Retinat's MCP tools are prefixed `mcp__retinat__retinat_*`. If they aren't connected, the repository's
`.mcp.json` registers them. You can also start the server by hand with
`uv run python -m browser_use.retinat`, adding `--cdp-url http://127.0.0.1:9222` to attach to the
user's own Chrome.

## Workflow

1. **Open the page:** `retinat_open(url)`. If the reply starts with `BLOCKED:`, stop and tell the
   user what blocked you and what the reply advises. Never try to get past a bot wall or CAPTCHA.
2. **Look before you act:**
   - `retinat_look` shows the current screen.
   - `retinat_scan` shows the whole page scrolled, which suits canvas, WebGL and scroll-driven
     pages.
   - For a video, call `retinat_watch(until='bored')`. It returns keyframes and sound labels, and
     a transcript when the speech extra is installed. It pauses the video afterwards, so nothing
     plays unseen while you think.
3. **Act like a person:** `retinat_tap` / `retinat_click` at coordinates read off the image,
   `retinat_type` into the focused field, `retinat_key` for Enter, Tab or Escape, and
   `retinat_swipe`. For feeds, use `retinat_next` or `retinat_browse(items=N)`.
4. **Check the result by looking again**, not by assuming it worked.
5. **Find bugs across a site:** `retinat_explore(url)` returns a report plus a sheet of every
   page. Present the findings with their evidence.

## Rules

- **No Playwright, Puppeteer or Selenium code.** Use the MCP tools, or `browser_use`'s own Python
  API (`BrowserSession`, `Eyes`, `Explorer`, `HumanInput`, `HumanTouch`).
- **No screenshot loops.** The eyes already send only what changed.
- **Never automate a login.** The user signs in themselves, in the visible browser or their own
  Chrome.
- **Submit, send or buy only when the user explicitly asked** for that exact action.
- **Label estimates as estimates.** Token counts are estimates.
- **Say "blocked" when blocked**, and "no audio" when there was none.

See `AI.md` at the repository root for setup in other clients, the Python API, and the known
limits.
