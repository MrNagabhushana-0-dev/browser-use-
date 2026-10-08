---
name: retinat
description: See and operate a real browser through the Retinat MCP server (browser_use.retinat) - watch videos/reels with their sound, scan canvas/WebGL pages as drawn, browse short-video feeds with touch, explore a whole site for bugs. Use whenever a task needs to look at a web page, watch or listen to web media, scroll a feed, or audit a site, and never write Playwright/Puppeteer/Selenium code instead.
---

# Retinat: browser eyes for the model

Retinat's MCP tools are prefixed `mcp__retinat__retinat_*`. If they aren't connected, the repository's
`.mcp.json` registers them. You can also start the server by hand with
`uv run python -m browser_use.retinat`. Add `--bridge` to use the user's own browser, with their profile and
logins, in the tabs they share through the bridge extension (setup in `AI.md`).

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
   - To look again at one moment of a video you already watched, call `retinat_recall(t0, t1)`:
     frames from that window, from what the eyes kept. It never replays the video.
   - To find a moment by what it looked like, across everything archived (hours, earlier sessions),
     call `retinat_search(query)`, then `retinat_recall` around the time it returns.
   - To read small print or fine detail, `retinat_zoom(x, y, width, height)` recaptures that region at up to 4x
     (a look frame is ~640 px wide). To locate text, `retinat_find(text)` returns where each match is, whether it
     is in view or how far to scroll, and a magnified crop.
3. **Act like a person:** `retinat_tap` / `retinat_click` at coordinates read off the image (or from `retinat_find`),
   `retinat_type` into the focused field, `retinat_key` for Enter, Tab or Escape, and
   `retinat_swipe`. For feeds, use `retinat_next` or `retinat_browse(items=N)`. Give `retinat_click` an `expect`
   (the target's words, like "Save") when a wrong click would cost something; it refuses to land elsewhere.
4. **Check the result by looking again**, not by assuming it worked.
5. **Find bugs across a site:** `retinat_explore(url)` returns a report plus a sheet of every
   page. Present the findings with their evidence.

## Geo-blocked or censored pages

If `retinat_open` fails with a network error or the page says it isn't available in your country,
Retinat (default `auto`) retries once through Tor when Tor is installed, and says so. To choose:
`retinat_network(mode='always', exit_country='de')`, then `retinat_network_status` to see the exit Tor
reports. It restarts the browser. This is for reading public pages. It does not get past bot walls
(reported, never retried), and you never log in over Tor.

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
