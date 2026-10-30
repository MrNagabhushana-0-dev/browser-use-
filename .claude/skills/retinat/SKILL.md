---
name: retinat
description: See and operate a real browser through the Retinat MCP server (browser_use.retinat) - watch videos/reels with their sound, scan canvas/WebGL pages as drawn, browse short-video feeds with touch, explore a whole site for bugs. Use whenever a task needs to look at a web page, watch or listen to web media, scroll a feed, or audit a site, and never write Playwright/Puppeteer/Selenium code instead.
---

# Retinat: browser eyes for the model

Retinat's MCP tools are prefixed `mcp__eye-mcp-unlimited__eye_*`. If they aren't connected, the repository's
`.mcp.json` registers them. You can also start the server by hand with
`uv run python -m browser_use.retinat`. Add `--bridge` to use the user's own browser, with their profile and
logins, in the tabs they share through the bridge extension (setup in `AI.md`).

For a native desktop app rather than a web page, a server started with `BROWSER_USE_DESKTOP_CONTROL=1` adds
`eye_desktop_*` tools: look at the screen, then click, type and press keys in the apps the person granted. Call
`eye_desktop_status` first. Read AI.md's "Computer use" section before using them.

## Workflow

1. **Open the page:** `eye_open(url)`. If the reply starts with `BLOCKED:`, stop and tell the
   user what blocked you and what the reply advises. Never try to get past a bot wall or CAPTCHA.
   If it says `Retinat is still loading` or `The browser is still starting`, call it again: the first
   call after a machine boots can take a minute, and the loading goes on between calls.
2. **Look before you act:**
   - `eye_look` shows the current screen.
   - `eye_scan` shows the whole page scrolled, which suits canvas, WebGL and scroll-driven
     pages. A page too long for one call (about 50 s) ends with `call again with from_screen=...`:
     call it so to see the rest.
   - For a video, call `eye_watch(until='bored')`. It returns keyframes and sound labels, and
     a transcript when the speech extra is installed. It pauses the video afterwards, so nothing
     plays unseen while you think.
   - To look again at one moment of a video you already watched, call `eye_recall(t0, t1)`:
     frames from that window, from what the eyes kept. It never replays the video.
   - To find a moment by what it looked like, across everything archived (hours, earlier sessions),
     call `eye_search(query)`, then `eye_recall` around the time it returns.
   - To read small print or fine detail, `eye_zoom(x, y, width, height)` recaptures that region at up to 4x
     (a look frame is ~640 px wide). To locate text, `eye_find(text)` returns where each match is, whether it
     is in view or how far to scroll, and a magnified crop. `eye_scroll(to=text)` turns the wheel until it is in
     view and says where; `eye_scroll(screens=n)` pages by screens (negative is up).
3. **Act like a person:** `eye_tap` / `eye_click` at CSS px: the image's pixels times the factor the look's
   text gives (or straight from `eye_find`),
   `eye_type` into the focused field, `eye_key` for Enter, Tab or Escape, and
   `eye_swipe`. For feeds, use `eye_next` or `eye_browse(items=N)`. Give `eye_click` an `expect`
   (the target's words, like "Save") when a wrong click would cost something; it refuses to land elsewhere.
4. **Check the result by looking again**, not by assuming it worked.
   - `eye_requests(only="failed")` when something did nothing: the failing request and, with `body=#n`, what
     the server said (tokens and passwords masked).
   - `eye_console(level="error")` when the page breaks or goes blank: what it logged and threw.
5. **Play a real-time game yourself:** `eye_step(ms, tap=[...], hold=[...])` stops the page's clock while you
   think and plays only the game time each step asks, then shows the frame it ended on. Say a score made this way is
   turn-based; `charge_thinking=true` plays your thinking time first, to see how the play would fare live.
6. **Find bugs across a site:** `eye_explore(url)` returns a report plus a sheet of every
   page. Present the findings with their evidence.

## Geo-blocked or censored pages

If `eye_open` fails with a network error or the page says it isn't available in your country,
Retinat (default `auto`) retries once through Tor when Tor is installed, and says so. To choose:
`eye_network(mode='always', exit_country='de')`, then `eye_network_status` to see the exit Tor
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
