# AI.md: how any AI should use this repository

You are an AI (Claude, Codex, Gemini, Cursor, anything else). This repository lets you **see and
operate a real browser**. It drives Chrome directly over the Chrome DevTools Protocol, with no
Playwright, Puppeteer or Selenium. Read this before you act.

## The two MCP servers

| Server | Start it with | Use it for |
|---|---|---|
| **retinat**: eyes | `uv run python -m browser_use.retinat` (or `retinat`) | Seeing: a playing video or reel with its sound, any page as drawn (canvas and WebGL included), whole-page scans, feeds, touch, and site-wide bug hunts. |
| **browser-use**: hands on the DOM | `uv run python -m browser_use.mcp` | Structure: element indices, clicking by index, typing into fields, extracting text, a page's own tools, and tabs. |

Use both together. Retinat tells you what the page *looks and sounds like*, and browser-use tells
you what it *is made of*. Use whichever answers the question for fewer tokens:
- **Text, lists or forms:** browser-use is usually cheaper.
- **Video, canvas, animation, or "does this look right":** only Retinat can answer.

### Register them

**Claude Code.** This repository ships `.mcp.json`, so opening the repository in Claude Code offers both servers. Or add them by hand:
```bash
claude mcp add retinat -- uv run --directory /path/to/browser-use- python -m browser_use.retinat
claude mcp add browser-use -- uv run --directory /path/to/browser-use- python -m browser_use.mcp
```

**Codex:** add this to `~/.codex/config.toml`:
```toml
[mcp_servers.retinat]
command = "uv"
args = ["run", "--directory", "/path/to/browser-use-", "python", "-m", "browser_use.retinat"]
```

**Gemini CLI, Cursor, Claude Desktop and anything else that takes JSON:**
```json
{"mcpServers": {"retinat": {"command": "uv", "args": ["run", "--directory", "/path/to/browser-use-", "python", "-m", "browser_use.retinat"]}}}
```

**Attaching to your own Chrome.** Start Chrome yourself, then pass its debugging address. This gives you the person's profile, logins and home connection:
```bash
google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.config/chrome-retinat
uv run python -m browser_use.retinat --cdp-url http://127.0.0.1:9222
```

Install the extras for full hearing, which adds local speech detection and transcription:
```bash
uv sync --all-extras
```
Without the extras, you get sight plus heuristic sound labels, and the percepts say so.

## Memory outside the context: the journal

The eyes keep watching between your turns. They write only what *changed* (a page opened, a new
item, a sound change, a pause) to `journal.jsonl` next to `now.json` in `~/.config/browseruse/eyes/`.
Each entry carries the item and the media time, so `retinat_recall(t0, t1, item=...)` can fetch the
frames for any of them. With the Claude Code hook (`python -m browser_use.eyes.hook`), each turn opens
with the entries you have not seen yet, newest 8, plus the current one-line reading. That keeps the
continuous stream on disk rather than in your context.

## Which Retinat tool to call

| You want to... | Call | Typical cost |
|---|---|---|
| Open a page, and know if it refused you | `retinat_open` | text only |
| See what's on screen now | `retinat_look` | 1 image (about 400–900 tokens) |
| Understand a whole canvas or scroll-driven page | `retinat_scan` | 1 sheet (about 1–1.5k tokens) |
| Watch a video or reel, including its sound | `retinat_watch` with `until=bored` | 1 sheet plus a timeline (about 300–900 tokens per item) |
| Follow an animation drawn on a canvas (a game, a chart, a physics demo): count bounces, swings, sweeps | `retinat_watch` on a page with no video (it attends the largest canvas; the percept has a `motion:` line with turning points and times) | about 200-500 tokens |
| Scroll Reels or Shorts like a person | `retinat_browse` with `items=N` | 1 sheet, one row per item |
| Go to the next or previous feed item | `retinat_next` | text only |
| Tap, swipe, click, type, press a key | `retinat_tap`, `retinat_swipe`, `retinat_click`, `retinat_type`, `retinat_key` | text only |
| Find a moment by what it looked like, across everything archived ("a slide full of code") | `retinat_search` with `query` | 1 strip of up to 8 frames; first use downloads a ~300 MB open model |
| Look again at one moment of a video you already watched (by media time) | `retinat_recall` with `t0`, `t1` | 1 strip of up to 8 frames (about 300-900 tokens) |
| Know what's playing without an image | `retinat_now` | about 30 tokens |
| What appeared, played or flashed since you last asked (toasts, sounds, cuts), for clients without the hook | `retinat_changes` | text only, a line per change |
| Find everything broken on a site | `retinat_explore` | a report plus 1 sheet |

Token figures are estimates. Images are costed at about one token per 28×28 px patch.

Coordinates for `retinat_tap` and `retinat_click` are viewport CSS pixels. Read them off the
`retinat_look` image, or take them from browser-use's `browser_get_state`.

## Hard rules

1. **Never write Playwright, Puppeteer or Selenium code**, not even "just to test". Use the MCP
   tools, or this library in Python:
   - `browser_use.browser.BrowserSession` for the browser.
   - `browser_use.human.HumanInput` and `browser_use.human.touch.HumanTouch` for real input.
   - `browser_use.eyes.Eyes` to watch, look, scan, browse and move to the next item.
   - `browser_use.explore.Explorer` to audit a site.

   For anything CDP-level, call the typed client directly:
   `cdp.cdp_client.send.Domain.method(params=..., session_id=...)`.

   The library's only Playwright touchpoint is an optional fallback that *downloads a Chromium
   binary* when no Chrome is installed. No automation goes through it.
2. **Don't loop on screenshots.** `retinat_look`, `retinat_scan` and `retinat_watch` already send
   only the frames that differ. The frames come from the compositor or the video element; they
   are not screenshot calls.
3. **Never try to get past a bot wall or CAPTCHA.** Retinat reports walls such as Google's
   "unusual traffic" page, YouTube's "confirm you're not a bot", and Cloudflare challenges as
   `BLOCKED: ...`. Tell the person. The way through is their own browser and connection
   (`--cdp-url`, or `python -m browser_use.cobrowse`), with them completing any challenge
   themselves.
4. **Never automate a login.** The person signs in, once, in the visible browser, and the profile
   keeps the session.
5. **Submit forms, send messages or buy things only when the person explicitly asked** for that
   exact action.
6. **Report honestly.** Say what was measured and what was estimated. Say "blocked" when you
   were blocked, and "couldn't hear it" when there was no audio track. Never describe a
   challenge page as the site's content.

## Python in 20 lines

```python
import asyncio
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.eyes import Eyes
from browser_use.explore import Explorer, render_markdown

async def main():
    session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None))
    await session.start()
    await session.navigate_to('https://example.com/')
    eyes = Eyes(session)
    await eyes.open()
    page = await eyes.scan()             # the whole page as drawn, a few keyframes
    print(page.text)                     # page.image is a JPEG sheet
    report = await Explorer(session).run('https://example.com/')
    print(render_markdown(report))
    await session.kill()

asyncio.run(main())
```

## Networks that re-sign TLS

Corporate proxies and agent sandboxes present their own CA. If every page fails with
`ERR_CERT_AUTHORITY_INVALID`, set `BROWSER_USE_PROXY_CA_CERT=/path/to/proxy-ca.pem`. Only that
key is trusted, and verification stays on for everything else.

The navigation error names this setting when it detects such an environment. There is no
automatic detection: telling private CAs from public ones automatically was tried, and it
wrongly trusted public CAs.

## Choosing the route: direct, or Tor with an exit country

Some public pages load from one country and not another: a network censors them, or the site
geo-fences them. For research and education you can route the browser through Tor and pick the exit
country. Both MCP servers have two tools for it, and a person's UI toggle calls the same method:

| Tool | Does |
|---|---|
| `retinat_network` / `browser_network` | `mode` = `off` (direct), `auto` (direct, then Tor after a network failure or a "not available in your country" page), `always` (Tor). Optional `exit_country` (`de`, `jp`, ...) and `reason`. |
| `retinat_network_status` / `browser_network_status` | The route, the exit address and country **as Tor reports them**, and recent route events. |

Defaults: the library and `browser-use --mcp` start at `off`. Retinat starts at `auto`, which only
acts on a clear network or geo failure, never on a bot wall, and never when attached to a Chrome you
run (`--cdp-url` keeps its own connection). Change it with `--network off|auto|always`,
`--exit-country de`, or the `BROWSER_USE_NETWORK` / `BROWSER_USE_EXIT_COUNTRY` environment variables.
Changing the route restarts the browser and closes its tabs, because a proxy belongs to the browser.

```python
from browser_use.net import NetworkMode, NetworkRouter

router = NetworkRouter(NetworkMode.AUTO, exit_country='jp')
await router.set_network('always', 'kr', reason='compare Korean listings')   # starts Tor now
profile = BrowserProfile(**await router.session_kwargs())                     # SOCKS5 + leak guards + throwaway profile
print(await router.status())                                                  # exit ip/country from Tor itself
```

**Needs Tor installed** (`apt install tor`, `brew install tor`) or one you already run. Without it,
`auto` says "Tor fallback unavailable" and continues to fail normally, and `always` refuses up front.
On a network that blocks Tor, `TorConfig(bridges=[...])` takes obfs4/webtunnel lines.

How a country is chosen, and why it is built this way:
- Chromium's SOCKS5 has no authentication, and Tor's `ExitNodes` is per process. So there is **one
  Tor process per country**, each on its own local port (`TorPool`, at most 3, least recently used
  stopped first), and the browser is launched against the port it wants.
- Tor's country data is approximate, and `StrictNodes` is no guarantee. `status` reports the country
  Tor itself says the exit is in and warns on a mismatch. Do not assume the country.
- Through Tor the browser gets `--disable-quic`, `--disable-ipv6` and
  `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` (SOCKS5 carries TCP only, so QUIC and
  WebRTC would otherwise go around it), proxy-side DNS, no extensions and a throwaway profile (no
  cookies or logins carried in). Plain `http://` is refused, because the exit relay can read and alter
  it; `allow_http=True` is the opt-out.

**What this does not do, and what to do instead**
- **It does not get past bot detection, and it will not try.** Tor exit addresses are on public
  block lists, so Google, YouTube and Cloudflare-fronted sites challenge them *more*. A wall is
  classified `walled`, reported as `BLOCKED`, never retried through Tor, and never solved.
  For YouTube, use the person's own Chrome (`--cdp-url`) or an alternative front end (Invidious, Piped).
- **Never log in or enter credentials over Tor.** Exits can read and tamper with traffic, and a
  signed-in session defeats the point. The route tools say so to the model; it is not enforced in code yet.
- **Be a good guest.** Tor is run by volunteers. Don't use it for bulk downloads, video or scraping
  at volume; read pages, don't crawl them. Tor speed is a few Mbit/s with 1-3 s to first byte: expect
  60-90 s navigation timeouts to be reasonable. Using it to get around a geo-restriction can breach a
  site's terms; that is the person's call, not something this library decides.

Verified here with real browsers: the routing rules, the agent tools, and Chromium sending traffic
through a real SOCKS5 proxy with the hostname resolved by the proxy and a dead proxy meaning failure
rather than a direct connection. **Not verified: a real Tor bootstrap, exit verification against a
live circuit, and the country actually taking effect.** Those need a host with `tor` installed; the
tests for them skip elsewhere.

## Working on this code

- **Setup:** use `uv`, never `pip`. Use tabs, modern typing, and pydantic v2. See `CLAUDE.md`.
- **Tests:** `uv run pytest -vxs tests/ci`. Tests use a real browser and `pytest-httpserver`.
  Nothing is mocked except the LLM, and there are no real remote URLs in tests.
- **Where things live:**
  - Vision, video and audio: `browser_use/eyes/`.
  - Site exploration: `browser_use/explore/`.
  - Retinat server: `browser_use/retinat/`.
  - browser-use MCP server: `browser_use/mcp/server.py`.
  - Real input: `browser_use/human/`.
  - Optional Tor transport: `browser_use/net/`.
- **Measured results and limits:** these are in `docs/agent-notes/findings-log.md`. Read them
  before claiming anything works on a site.

## Known limits

- **Blocked networks:** YouTube and Google refuse datacenter IP ranges before any browser logic
  runs. Use the person's machine.
- **Protected video:** DRM video yields no pixels. Cross-origin video without CORS yields no
  pixels either; this is detected and said.
- **Missing codecs:** Chromium builds without H.264 can't play Instagram. Use Google Chrome.
- **Sound labels:** heuristic speech/music labels are unreliable on music. The optional voice
  model decides speech.
- **Wheel and arrow-key fallbacks** don't move CSS scroll-snap feeds.
