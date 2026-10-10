# Handoff: what this fork adds and how to use it

This fork of browser-use adds:
- **eyes:** an agent can watch and hear pages and video;
- **Retinat:** an MCP server for those eyes;
- **an extension bridge:** the AI can work in your own Chrome, Edge, Brave, Opera or Vivaldi, with your consent at each step;
- **opt-in Tor routing** and **opt-in desktop control**;
- **clearer MCP errors.**

Everything here was merged in PR #9 (`d17f18d`). The full `tests/ci` suite passed before the merge: 1,655 passed, 30 skipped, 0 failed.

`AI.md` is the detailed manual for agents. `docs/agent-notes/findings-log.md` records every round of work, with what was measured and what was not.

---

## 1. Set up

```bash
uv venv --python 3.11 && source .venv/bin/activate
uv sync
```

Connect the two MCP servers to Claude Code (or any MCP client):

```bash
claude mcp add retinat     -- uv run --directory /path/to/browser-use- python -m browser_use.retinat
claude mcp add browser-use -- uv run --directory /path/to/browser-use- python -m browser_use.mcp
```

- **retinat** is the eyes: it sees pages as drawn, plays and hears media, uses touch, and explores whole sites.
- **browser-use** is the hands on the DOM: element indices, clicks, typing, extraction, tabs, and a page's own tools.

The repo also ships a Claude Code skill (`.claude/skills/retinat`) and an agent (`.claude/agents/retinat-browser.md`), which use Retinat for you.

---

## 2. Features and the tools behind them

### Eyes (`browser_use/eyes`) and Retinat (`browser_use/retinat`)

| Want to... | Tool |
|---|---|
| Open a page | `retinat_open` |
| See the page as drawn (canvas and WebGL included) | `retinat_look`, `retinat_scan` (whole page) |
| Watch a video or reel, with its sound | `retinat_watch`: one sheet of keyframes, the sound timeline, cuts, captions, and a transcript if the speech extra is installed |
| Scroll a short-video feed | `retinat_browse`, `retinat_next`, `retinat_swipe` (touch) |
| Look back at a moment already seen | `retinat_recall` (by media time; frames kept on disk) |
| Find frames by what they show | `retinat_search` (a local CLIP image-text model) |
| Hear what changed since you last asked | `retinat_changes` (from the journal: new page, item, sound, toast) |
| Find and read fine print | `retinat_find`, `retinat_zoom` |
| Act | `retinat_click`, `retinat_tap`, `retinat_type`, `retinat_key` |
| See what the page fetched | `retinat_requests` (secrets in bodies are masked) |
| Read the page's console | `retinat_console` (errors, warnings, logs; masked) |
| Check the state now | `retinat_now` |
| Audit a whole site for bugs | `retinat_explore` (live token meter and time estimate) |
| Choose the network route | `retinat_network`, `retinat_network_status` |

Every tool says what happened when it fails: effect `none` (nothing was done), `unknown`, or `committed`. A call missing a required argument is refused before anything runs.

### The browser-use MCP server (`browser_use/mcp`)

- **Pages:** `browser_navigate`, `browser_click`, `browser_type`, `browser_scroll`, `browser_go_back`, `browser_get_state`, `browser_extract_content`, `browser_get_html`, `browser_screenshot`.
- **Tabs and sessions:** `browser_*_tab` and `browser_*_session`.
- **Scripts and a page's own tools:** `browser_run_script`, `browser_list_page_tools`, `browser_call_page_tool`.
- **Network route:** `browser_network`, `browser_network_status`.
- **Eyes:** `eyes_*`.
- `browser_click` now reports a click that failed instead of claiming success.

### The extension bridge: the AI in your own browser (`browser_use/bridge`)

Set it up:
```bash
python -m browser_use.bridge extension ./ext          # write the extension
# chrome://extensions → Developer mode → Load unpacked → pick ./ext
uv run python -m browser_use.retinat --bridge         # Retinat + relay on 127.0.0.1:9333
python -m browser_use.bridge doctor                   # checks every link when something doesn't connect
```

Then, on any tab, press the extension's button (or Alt+Shift+A) and choose **Share this tab with the AI**.

**You stay in charge:**
- the AI sees and acts only in tabs you share;
- a pill in each shared tab shows that the AI is there;
- **Take the wheel** pauses the AI, and it resumes once you stop;
- **Cancel** on the browser's debugging bar stops everything.

**Sites:**
- A new site is asked about in a small window of the extension's own, which the AI can't reach: **Allow** (until the browser closes), **Always**, or **No**.
- A tab that wanders to a site you haven't allowed stops being shared at once.
- Frames from other sites stay hidden unless their site is allowed.

**Clicks that matter wait for you:** placing an order, paying, deleting an account, granting access. Accepting a page's confirm or prompt is yours to do too.

**The Sites list** (the pill's **Sites** button, or the toolbar popup):
- **Remove** takes a site back.
- **Every time** makes a site ask on every visit, with no Always; use it for your bank.
- **Stop asking** undoes that, and only you can.

**Setting things in advance:**
- `--always-allow SITE` or `--ask-every-time SITE` when you write the extension;
- from code: `relay.forget(site)` and `relay.ask_every_time(site)`, which only ever take access away.

**Always refused through the bridge:**
- disguising the browser (user agent, location, time zone);
- rewriting traffic;
- writing cookies directly;
- turning off protections;
- handing a page files from disk;
- reaching past the shared tabs.

It was tested on Chromium 141, Chrome 155, Edge 154, Brave 1.97 and Vivaldi 8.2.

### Network route (`browser_use/net`)

- **The setting:** `off` (direct), `auto` (direct, then one retry through Tor after a network failure or a "not available in your country" page), or `always` (Tor). An exit country can be set too.
- **Defaults:** off for the library and `browser-use --mcp`; auto for Retinat.
- **Where to set it:** `--network off|auto|always`, `--exit-country de`, or `BROWSER_USE_NETWORK` / `BROWSER_USE_EXIT_COUNTRY`.
- **Needs a `tor` binary.** A bot wall is reported as `BLOCKED`, never solved or bypassed.

### Desktop (opt-in, `browser_use/desktop`)

- **Desktop eyes:** start Retinat with `BROWSER_USE_DESKTOP_EYES=1` to get `retinat_desktop_look` and `retinat_desktop_watch`.
- **Desktop control:** set `BROWSER_USE_DESKTOP_CONTROL=1 BROWSER_USE_DESKTOP_APPS="gedit,xterm:click"`. The AI then acts only in the apps you list, and others need your approval first.

### Library fixes

- The about:blank loading screen no longer loads a logo from the internet at every start.
- Temporary profiles are removed when the browser is killed.
- A cross-process launch race is fixed.
- `BROWSER_USE_PROXY_CA_CERT` is for networks that intercept TLS.

---

## 3. Testing

```bash
uv run pytest -vxs tests/ci                                   # everything (about 31-41 min)
uv run pytest tests/ci/test_bridge.py                          # the bridge (36 tests, about 3 min)
BRIDGE_TEST_BROWSER=/path/to/msedge uv run pytest tests/ci/test_bridge.py   # on a branded browser
BROWSER_USE_PROXY_CA_CERT=/path/ca.crt uv run pytest tests/ci  # behind a TLS-intercepting proxy
```

- The tests use real browsers and local pytest-httpserver pages, with no mocks. In the bridge tests, the "person" clicks with real X input.
- The 30 skips each have a named reason: no API keys for live model tests, no `tor` binary.

---

## 4. Known gaps, honestly

1. **Bridge, open security item:** the bridge doesn't yet check CDP calls that name a specific script execution context. In a probe, such a call reached the extension's own content-script world. Until it's fixed, use the bridge only with an AI client you trust. It's the top item in the findings log. The fix would:
   - refuse calls whose context isn't the page's own or one the client created;
   - refuse the Debugger and HeapProfiler domains through the bridge;
   - restrict the extension's local storage to its own pages.
2. **Edge:** one run had 7 failures that two reruns didn't repeat. The log wasn't kept.
3. **Every-time sites:** the one-minute windows are untested, and a restart of the extension's worker forgets visits (it errs toward asking).
4. **Tor** is untested here: there was no `tor` binary.
5. **Not measured:** whether recall, the journal and search help agents more than contact sheets do; and search quality on real video.
6. **During tests,** Chromium still contacts `mtalk.google.com` and `www.google.com`, and the source of those connections isn't known yet.

---

## 5. What's next, in order

1. The bridge security item above.
2. A blind-agent A/B test of the contact sheet, recall and journal on feed pages: answer accuracy and tokens. The tools to build on are in `docs/agent-notes/e2e/` (`host*.py`, `score.py`).
3. Colour-aware cuts, measured on a real, rights-cleared clip.
4. A blind fine-print run where `retinat_scan` can't read the page text, so `retinat_find` alone is measured.

**Ground rules this work kept:**
- no bot-detection or CAPTCHA bypass;
- no disguising the browser as a person;
- no automated logins;
- no Playwright, Puppeteer or Selenium code;
- no media without the rights to use it.
